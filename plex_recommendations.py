#!/usr/bin/env python3
import os, sys, logging, requests, time, json, random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexServer

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

HISTORY_FILE = "/app/data/recommendations_history.json"
LAST_RUN_FILE = "/app/data/last_run.json"
INTERVAL_DAYS = 7


# --- Schedule lock ---

def load_last_run() -> datetime | None:
    if Path(LAST_RUN_FILE).exists():
        try:
            with open(LAST_RUN_FILE, 'r') as f:
                data = json.load(f)
                return datetime.fromisoformat(data["sent_at"])
        except Exception as e:
            logger.error(f"Failed to load last run file: {e}")
    return None

def save_last_run():
    Path(LAST_RUN_FILE).parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    with open(LAST_RUN_FILE, 'w') as f:
        json.dump({"sent_at": now.isoformat()}, f, indent=2)
    logger.info(f"Last run saved: {now.isoformat()}")

def seconds_until_next_run() -> float:
    last_run = load_last_run()
    if last_run is None:
        return 0
    next_run = last_run + timedelta(days=INTERVAL_DAYS)
    remaining = (next_run - datetime.now()).total_seconds()
    return max(0, remaining)


# --- Recommendation history ---

class RecommendationHistory:
    def __init__(self):
        self.history = self.load_history()

    def load_history(self):
        if Path(HISTORY_FILE).exists():
            try:
                with open(HISTORY_FILE, 'r') as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"Failed to load history: {e}")
                return []
        return []

    def save_history(self):
        Path(HISTORY_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(HISTORY_FILE, 'w') as f:
            json.dump(self.history, f, indent=2)

    def add_recommendations(self, items):
        cutoff = (datetime.now() - timedelta(weeks=5)).isoformat()
        self.history = [item for item in self.history if item.get('date', '') >= cutoff]
        for item in items:
            self.history.append({**item, 'date': datetime.now().isoformat()})
        self.save_history()

    def is_recommended_recently(self, item_id):
        cutoff = (datetime.now() - timedelta(weeks=5)).isoformat()
        for h in self.history:
            if h.get('date', '') >= cutoff and h.get('id') == item_id:
                return True
        return False


# --- Plex ---

class PlexDiscoverClient:
    def __init__(self, username: str, password: str, server_url: str, server_token: str):
        self.history = RecommendationHistory()
        logger.info("Connecting to Plex account...")
        # Use token auth to avoid username/password rate limiting on plex.tv
        self.account = MyPlexAccount(token=server_token)
        import requests as req
        session = req.Session()
        session.verify = os.getenv("PLEX_CA_BUNDLE") or os.getenv(
            "PLEX_VERIFY_TLS", "true",
        ).lower() not in ("0", "false", "no", "off")
        self.server = PlexServer(server_url, server_token, session=session)
        self._build_library_index()

    def _build_library_index(self):
        """Build a set of titles already in the local library to exclude from recommendations."""
        self.owned_titles = set()
        for section in self.server.library.sections():
            if section.type in ("movie", "show"):
                for item in section.all():
                    self.owned_titles.add(item.title.lower().strip())
        logger.info(f"Library index built: {len(self.owned_titles)} titles owned")

    def _item_id(self, item, media_type: str) -> str:
        # Discover search results don't have a usable ratingKey (comes back as
        # NaN), so use the globally-unique guid instead - falls back to
        # ratingKey for any local-library item that does have one.
        identifier = getattr(item, "guid", None) or item.ratingKey
        return f"{media_type}_{identifier}"

    def get_recommendation_pool(self, media_type: str = "movie", debug: bool = False, pool_size: int = 50) -> list:
        """
        Fetch a pool of Plex Discover candidates using popular search terms,
        excluding anything already in the local library or recently recommended.
        media_type: "movie" or "show"

        Note: searchDiscover is a literal title-text search, not a "trending"
        browse endpoint - a single query (e.g. "history") alone easily returns
        50 raw matches, so if the pool cap were checked only globally, whichever
        query got shuffled first could fill the entire pool by itself, making
        every result share that one word in the title. To get real variety, we
        cap how many candidates each individual query can contribute, so the
        pool ends up drawn from most/all of the queries. Each query's own
        results are already relevance-ranked by Discover, and we take them in
        that order, so a smaller `pool_size` also means a tighter per-query
        cap - i.e. only each query's *most* relevant hits make it into the pool.

        Final selection (including the TMDB-match requirement) happens later
        in `select_with_tmdb_match`, not here.
        """
        search_queries = [
            "popular", "action", "drama", "thriller", "comedy",
            "sci-fi", "crime", "horror", "adventure", "mystery",
            "romance", "fantasy", "documentary", "animation", "history"
        ]
        random.shuffle(search_queries)
        plex_libtype = "movie" if media_type == "movie" else "show"
        per_query_cap = max(1, -(-pool_size // len(search_queries)))  # ceil division

        seen_keys = set()
        pool = []

        for query in search_queries:
            if len(pool) >= pool_size:
                break
            try:
                items = self.account.searchDiscover(query, limit=50, libtype=plex_libtype)
            except Exception as e:
                logger.error(f"searchDiscover failed for '{query}': {e}")
                continue

            added_for_query = 0
            for item in items:
                if len(pool) >= pool_size or added_for_query >= per_query_cap:
                    break
                key = self._item_id(item, media_type)
                if key in seen_keys:
                    continue
                seen_keys.add(key)

                title_lower = item.title.lower().strip()
                if title_lower in self.owned_titles:
                    continue

                if not debug and self.history.is_recommended_recently(key):
                    continue

                pool.append(item)
                added_for_query += 1

        label = "movies" if media_type == "movie" else "shows"
        suffix = " (history filter bypassed)" if debug else ""
        logger.info(f"Built a pool of {len(pool)} candidate {label}{suffix}")
        return pool


def select_with_tmdb_match(pool: list, search_fn, limit: int = 5) -> list:
    """
    Randomly walk the pool and keep the first `limit` items that both (a)
    have a resolvable TMDB match and (b) have a usable description (from
    Plex's own summary or TMDB's overview). Anything missing either is
    skipped and not counted, so it gets backfilled from the rest of the
    pool - every final pick has a working "Request on Overseerr" link and
    an actual description instead of either being dropped in blank at
    render time.
    Returns a list of (item, tmdb_details) tuples.
    """
    shuffled = pool[:]
    random.shuffle(shuffled)

    selected = []
    for item in shuffled:
        if len(selected) >= limit:
            break
        details = search_fn(item.title, getattr(item, "year", None))
        if not details.get("tmdb_id"):
            continue
        overview = (getattr(item, "summary", "") or "").strip() or (details.get("overview") or "").strip()
        if not overview:
            continue
        selected.append((item, details))

    return selected


# --- Local AI ---

LOCAL_AI_BASE = os.getenv("LOCAL_AI_BASE", "http://llama-litellm:4000/v1")
LOCAL_AI_MODEL = os.getenv("LOCAL_AI_MODEL", "Apex")
LOCAL_AI_API_KEY = os.getenv("LOCAL_AI_API_KEY", "").strip()
LOCAL_AI_HEADERS = {"Content-Type": "application/json"}
if LOCAL_AI_API_KEY:
    LOCAL_AI_HEADERS["Authorization"] = f"Bearer {LOCAL_AI_API_KEY}"
LOCAL_AI_TIMEOUT = 7200

OVERSEERR_URL = os.getenv("OVERSEERR_URL", "http://localhost:5055").rstrip("/")


def is_local_ai_ready() -> bool:
    try:
        resp = requests.get(f"{LOCAL_AI_BASE}/health".replace("/v1/health", "/health"), timeout=5)
        return resp.status_code == 200
    except Exception:
        return False


def wait_for_local_ai() -> bool:
    deadline = time.time() + LOCAL_AI_TIMEOUT
    logger.info(f"Waiting for local AI at {LOCAL_AI_BASE} ...")

    while time.time() < deadline:
        try:
            resp = requests.get(f"{LOCAL_AI_BASE}/models", headers=LOCAL_AI_HEADERS, timeout=5)
            if resp.status_code == 200:
                break
        except Exception:
            pass
        logger.info("Local AI server not up yet, retrying in 60s...")
        time.sleep(60)
    else:
        logger.error("Local AI server did not come up in time.")
        return False

    logger.info("Server up, probing model...")
    while time.time() < deadline:
        try:
            resp = requests.post(
                f"{LOCAL_AI_BASE}/chat/completions",
                headers=LOCAL_AI_HEADERS,
                json={
                    "model": LOCAL_AI_MODEL,
                    "messages": [{"role": "user", "content": "Hi"}],
                    "max_tokens": 1,
                },
                timeout=120,
            )
            if resp.status_code == 200:
                logger.info("Local AI model is loaded and ready.")
                return True
        except Exception:
            pass
        logger.info("Model not ready yet, retrying in 60s...")
        time.sleep(60)

    logger.error("Local AI model did not load in time.")
    return False


def summarize_with_local_ai(overview: str) -> str:
    try:
        response = requests.post(
            f"{LOCAL_AI_BASE}/chat/completions",
            headers=LOCAL_AI_HEADERS,
            json={
                "model": LOCAL_AI_MODEL,
                "max_tokens": 120,
                "messages": [
                    {
                        "role": "system",
                        "content": "You are a helpful assistant that writes short, engaging plot summaries. You always respond in English, regardless of what language the source text is in."
                    },
                    {
                        "role": "user",
                        "content": f"Rewrite the following plot summary in English, in exactly 2 concise, engaging sentences. If the source text below is not in English, translate it first. Do not start with the title. Return only the 2 English sentences, nothing else.\n\n{overview}"
                    }
                ]
            },
            timeout=30
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        logger.error(f"Local AI summarization failed: {e}")
        return overview[:200] + "…"


# --- TMDB ---

class TMDBClient:
    BASE_URL = "https://api.themoviedb.org/3"
    IMAGE_BASE = "https://image.tmdb.org/t/p/w500"

    def __init__(self, api_key: str):
        self.api_key = api_key

    def _fetch(self, path: str) -> dict:
        try:
            resp = requests.get(
                f"{self.BASE_URL}{path}",
                params={"api_key": self.api_key},
                timeout=10
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"TMDB fetch failed for {path}: {e}")
            return {}

    def search_movie(self, title: str, year: int = None) -> dict:
        params = {"api_key": self.api_key, "query": title}
        if year:
            params["year"] = year
        try:
            resp = requests.get(f"{self.BASE_URL}/search/movie", params=params, timeout=10)
            resp.raise_for_status()
            results = resp.json().get("results", [])
            if not results:
                return {}
            data = results[0]
            return {
                "tmdb_id": data.get("id"),
                "overview": data.get("overview", ""),
                "poster_url": f"{self.IMAGE_BASE}{data['poster_path']}" if data.get("poster_path") else None
            }
        except Exception as e:
            logger.error(f"TMDB movie search failed for '{title}': {e}")
            return {}

    def search_show(self, title: str, year: int = None) -> dict:
        params = {"api_key": self.api_key, "query": title}
        if year:
            params["first_air_date_year"] = year
        try:
            resp = requests.get(f"{self.BASE_URL}/search/tv", params=params, timeout=10)
            resp.raise_for_status()
            results = resp.json().get("results", [])
            if not results:
                return {}
            data = results[0]
            return {
                "tmdb_id": data.get("id"),
                "overview": data.get("overview", ""),
                "poster_url": f"{self.IMAGE_BASE}{data['poster_path']}" if data.get("poster_path") else None
            }
        except Exception as e:
            logger.error(f"TMDB show search failed for '{title}': {e}")
            return {}


# --- Discord ---

def _safe_poster_url(item) -> Optional[str]:
    """Some Plex Discover results have `thumb` already set to a full external
    URL (e.g. a TMDB image URL) instead of a relative Plex path. `thumbUrl`
    naively prepends the Plex server base URL regardless, producing a
    malformed concatenated URL that Discord's embed validation rejects with
    a 400. Use the raw thumb as-is when it's already absolute.
    """
    thumb = getattr(item, "thumb", None)
    if thumb and thumb.startswith(("http://", "https://")):
        return thumb
    try:
        return getattr(item, "thumbUrl", None)
    except Exception:
        return None


class DiscordNotifier:
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def send_message(self, embeds: list) -> bool:
        try:
            payload = {"embeds": embeds}
            response = requests.post(self.webhook_url, json=payload, timeout=10)
            response.raise_for_status()
            logger.info("Successfully sent Discord message")
            return True
        except requests.exceptions.HTTPError as e:
            logger.error(f"Failed to send Discord message: {e} | response body: {e.response.text if e.response is not None else '<none>'}")
            return False
        except Exception as e:
            logger.error(f"Failed to send Discord message: {e}")
            return False

    def build_show_embeds(self, show_selections: list) -> list:
        embeds = [{
            "title": "📺 Recommended TV Shows For You",
            "description": "Personalised picks from your Plex watch history\n\n> 💡 **Tip:** [Sign into Overseerr](" + OVERSEERR_URL + "/login) before clicking any request links below, otherwise you'll be taken to the home screen instead of the title after logging in.",
            "color": 0x7289da,
        }]
        for idx, (item, details) in enumerate(show_selections[:5], 1):
            try:
                title = item.title
                year = getattr(item, 'year', None)
                overview = getattr(item, 'summary', '') or ''
                tmdb_id = details.get("tmdb_id")
                poster_url = details.get("poster_url") or _safe_poster_url(item)
                if not overview:
                    overview = details.get("overview", "No description available.")
                summary = summarize_with_local_ai(overview) if overview else "No description available."
                embed = {
                    "title": f"{idx}. {title}{f' ({year})' if year else ''}",
                    "description": summary,
                    "color": 0x7289da,
                    "fields": [
                        {
                            "name": "\u200b",
                            "value": f"[📥 Request on Overseerr]({OVERSEERR_URL}/tv/{tmdb_id})",
                            "inline": False
                        }
                    ],
                }
                if poster_url:
                    embed["thumbnail"] = {"url": poster_url}
                embeds.append(embed)
            except Exception as e:
                logger.error(f"Failed to process show '{getattr(item, 'title', '?')}': {e}")
        return embeds

    def build_movie_embeds(self, movie_selections: list) -> list:
        embeds = [{
            "title": "🎬 Recommended Movies For You",
            "description": "Personalised picks from your Plex watch history\n\n> 💡 **Tip:** [Sign into Overseerr](" + OVERSEERR_URL + "/login) before clicking any request links below, otherwise you'll be taken to the home screen instead of the title after logging in.",
            "color": 0xe74c3c,
        }]
        for idx, (item, details) in enumerate(movie_selections[:5], 1):
            try:
                title = item.title
                year = getattr(item, 'year', None)
                overview = getattr(item, 'summary', '') or ''
                tmdb_id = details.get("tmdb_id")
                poster_url = details.get("poster_url") or _safe_poster_url(item)
                if not overview:
                    overview = details.get("overview", "No description available.")
                summary = summarize_with_local_ai(overview) if overview else "No description available."
                embed = {
                    "title": f"{idx}. {title}{f' ({year})' if year else ''}",
                    "description": summary,
                    "color": 0xe74c3c,
                    "fields": [
                        {
                            "name": "\u200b",
                            "value": f"[📥 Request on Overseerr]({OVERSEERR_URL}/movie/{tmdb_id})",
                            "inline": False
                        }
                    ],
                }
                if poster_url:
                    embed["thumbnail"] = {"url": poster_url}
                embeds.append(embed)
            except Exception as e:
                logger.error(f"Failed to process movie '{getattr(item, 'title', '?')}': {e}")
        return embeds


# --- Main ---

def fetch_and_post(debug: bool = False):
    logger.info("=" * 60)
    if debug:
        logger.info("Starting recommendations fetch (DEBUG mode — history bypassed)")
    else:
        logger.info("Starting weekly recommendations fetch")
    logger.info("=" * 60)

    plex_username = os.getenv("PLEX_USERNAME")
    plex_password = os.getenv("PLEX_PASSWORD")
    plex_server_url = os.getenv("PLEX_SERVER_URL", "http://localhost:32400")
    plex_token = os.getenv("PLEX_TOKEN")
    tmdb_api_key = os.getenv("TMDB_API_KEY")
    webhook = os.getenv("DISCORD_WEBHOOK_URL")

    if not all([plex_username, plex_password, plex_token, tmdb_api_key, webhook]):
        logger.error("Missing required environment variables: PLEX_USERNAME, PLEX_PASSWORD, PLEX_TOKEN, TMDB_API_KEY, DISCORD_WEBHOOK_URL")
        return False

    plex = PlexDiscoverClient(plex_username, plex_password, plex_server_url, plex_token)
    tmdb = TMDBClient(tmdb_api_key)
    discord = DiscordNotifier(webhook)

    movie_pool = plex.get_recommendation_pool("movie", debug=debug)
    show_pool = plex.get_recommendation_pool("show", debug=debug)

    # Resolve TMDB matches before picking the final 5, not after - anything
    # TMDB can't find gets skipped and backfilled from the rest of the pool,
    # so every recommendation that goes out has a working Overseerr link.
    movie_selections = select_with_tmdb_match(movie_pool, tmdb.search_movie, limit=5)
    show_selections = select_with_tmdb_match(show_pool, tmdb.search_show, limit=5)

    if not show_selections and not movie_selections:
        logger.warning("No recommendations fetched")
        return False

    all_recommendations = []
    for item, _ in show_selections:
        all_recommendations.append({"id": plex._item_id(item, "show"), "type": "show"})
    for item, _ in movie_selections:
        all_recommendations.append({"id": plex._item_id(item, "movie"), "type": "movie"})

    if not debug:
        plex.history.add_recommendations(all_recommendations)
    else:
        logger.info("DEBUG: Skipping history save")

    if debug:
        logger.info("DEBUG: Skipping local AI wait — summaries will be truncated.")
    elif not wait_for_local_ai():
        logger.warning("Proceeding without local AI — summaries will be truncated.")

    if show_selections:
        discord.send_message(discord.build_show_embeds(show_selections))
        time.sleep(1)
    if movie_selections:
        discord.send_message(discord.build_movie_embeds(movie_selections))

    logger.info("=" * 60)
    logger.info("Weekly recommendations fetch completed successfully")
    logger.info("=" * 60)
    return True


if __name__ == "__main__":
    debug = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")

    if len(sys.argv) > 1 and sys.argv[1] == "run":
        fetch_and_post(debug=debug)
    else:
        logger.info("Plex Recommendations Scheduler Starting...")
        # Brief startup delay to avoid hammering APIs on rapid container restarts
        time.sleep(10)

        while True:
            wait_seconds = seconds_until_next_run()

            if wait_seconds > 0:
                next_run_time = datetime.now() + timedelta(seconds=wait_seconds)
                hours, remainder = divmod(int(wait_seconds), 3600)
                minutes = remainder // 60
                logger.info(f"Last run was recent. Next run in {hours}h {minutes}m (at {next_run_time.strftime('%Y-%m-%d %H:%M:%S')})")
                time.sleep(wait_seconds)

            success = fetch_and_post(debug=debug)

            if success and not debug:
                save_last_run()
                time.sleep(60)
            elif success and debug:
                logger.info("DEBUG: Lock file not saved — remove DEBUG=true to enable weekly scheduling.")
                time.sleep(60)
            else:
                # A failed run doesn't save last_run, so without a longer pause here
                # a persistent failure (bad API, empty results, etc.) retries every
                # 60s indefinitely instead of backing off.
                logger.warning("Run failed; retrying in 15 minutes instead of hammering the API.")
                time.sleep(900)
