# Plex Recommendations

Plex Recommendations is a scheduled service that selects movies and shows
from Plex Discover, excludes titles already in your library or recently sent,
matches recommendations with TMDB, and posts them to a Discord webhook.
Messages can include request links through Overseerr or Seerr.

## Requirements

- Docker, or Python 3.12 with `plexapi` and `requests`
- Plex account credentials and a Plex server token
- A TMDB API key and a Discord webhook URL

## Configure

Create a private `.env` file:

```dotenv
PLEX_USERNAME=your-plex-username
PLEX_PASSWORD=your-plex-password
PLEX_SERVER_URL=http://plex:32400
PLEX_TOKEN=replace-with-your-plex-token
TMDB_API_KEY=replace-with-your-tmdb-api-key
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...

# Optional Telegram destination (configure both values)
TELEGRAM_BOT_TOKEN=replace-with-a-telegram-bot-token
TELEGRAM_CHAT_IDS=replace-with-chat-id

OVERSEERR_URL=http://overseerr:5055
LOCAL_AI_BASE=http://llama-litellm:4000/v1
LOCAL_AI_MODEL=your-model-name
# LOCAL_AI_API_KEY=optional-key
```

Set `PLEX_SERVER_URL`, `OVERSEERR_URL`, and `LOCAL_AI_BASE` to addresses
reachable from the container (or host process). In Docker, `localhost` refers
to the container itself, not another service. The Overseerr/Seerr URL is used
to build request links. Local AI is used to summarize descriptions; if its
endpoint is unavailable, the service can wait up to two hours for it before
continuing without AI summaries.

Set `DISCORD_WEBHOOK_URL`, or set both `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_IDS`, or configure both destinations to send recommendations to
both platforms. Telegram chat IDs are comma-separated; the Telegram bot must
be started by the recipient or added to the target group before it can send
messages.

Plex TLS certificate verification is enabled by default. For Plex using a
private certificate authority, set `PLEX_CA_BUNDLE` to its CA file path.
Disabling verification with `PLEX_VERIFY_TLS=false` is discouraged.

## Run with Docker

Pull and start the scheduler with persistent state:

```sh
docker pull ghcr.io/epik-win/plex-recommendations:latest
docker run -d --name plex-recommendations \
  --restart unless-stopped \
  --env-file .env \
  -v plex-recommendations-data:/app/data \
  ghcr.io/epik-win/plex-recommendations:latest
```

Each push to `main` publishes a multi-architecture image to GitHub Container
Registry. Images are also tagged with their source commit SHA for immutable
deployments.

The service checks weekly. On its first start, it runs immediately; subsequent
runs are spaced seven days apart. The `/app/data` volume stores recommendation
history and the last-run timestamp. Keep the volume when replacing or updating
the container to avoid losing that state.

To post a one-off run without changing the weekly schedule:

```sh
docker exec plex-recommendations \
  python /app/plex_recommendations.py run
```

A successful one-off run still adds its titles to the recent-recommendation
history, so they are excluded from normal selections for five weeks. View
service output with:

```sh
docker logs -f plex-recommendations
```

Keep credentials, chat IDs, and webhook URLs out of version control.

## License

MIT. See [LICENSE](LICENSE).
