# Plex Recommendations

A scheduled Python service that selects movies and shows from Plex Discover,
filters out titles already in the library or recently recommended, enriches
results with TMDB, and posts recommendation embeds to Discord.

The service can add request links through an Overseerr/Seerr-compatible URL
and optionally summarize descriptions with a local OpenAI-compatible model
endpoint.

## Run

The included Dockerfile builds the service image:

```sh
docker build -t plex-recommendations .
docker run --env-file .env -v plex-recommendations-data:/app/data \
  plex-recommendations
```

Configure these environment variables in a private `.env` file:

- `PLEX_USERNAME`, `PLEX_PASSWORD`, `PLEX_SERVER_URL`, and `PLEX_TOKEN`
- `TMDB_API_KEY` and `DISCORD_WEBHOOK_URL`
- `OVERSEERR_URL` for request links (defaults to `http://localhost:5055`)
- `LOCAL_AI_BASE`, `LOCAL_AI_MODEL`, and optionally `LOCAL_AI_API_KEY`
- `PLEX_CA_BUNDLE` when Plex uses a private certificate authority

The recommendation history and schedule state are stored in `/app/data`.
Plex TLS certificate verification is enabled by default. Keep credentials,
webhook URLs, and runtime history out of version control.

To run a one-off recommendation pass without changing the weekly schedule:

```sh
docker exec plex-recommendations python /app/plex_recommendations.py run
```

## License

MIT. See [LICENSE](LICENSE).
