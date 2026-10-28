# Changelog

## 1.5.0 — Unified dashboard and artist identities

### Unified stage dashboard

- Add a Vue 3 dashboard backed by FastAPI on port 8900, with a prebuilt frontend and a `karaoke-dashboard` entry point.
- Combine highlighted live lyrics, the animated mood canvas with in-square chord overlays, mood-over-time summaries, song metadata, play counts, and saved-playlist membership.
- Embed the Ollama AI DJ chat beside the mood display. Answers cannot automatically queue or play songs; queue suggestions require an explicit button click.
- Provide player transport/seek controls, artwork served over HTTP, artist library tracks, and saved/smart playlist playback.
- Add artist/member/side-project and song-similarity graphs. Left-click inspects without interrupting playback; right-click offers explicit play, queue, DJ-list, and processing actions.
- Navigate artist relations by Wikidata IDs, support MusicBrainz IDs, and reject partial-name substitutions. Protect the artist pane from stale asynchronous responses.
- Refresh artist context on each song change, resolving missing artist links through the existing MusicBrainz/Wikidata pipeline.
- Add dashboard regression tests and frontend build/chat-safety CI checks.

### Artist metadata and search

- Resolve library artist strings to canonical MusicBrainz identities and track credits.
- Cache Wikidata artist facts and relations, derive genres from MusicBrainz votes, and index artist documents in OpenSearch.
- Link newly ingested tracks to known artists and enrich new artists during post-processing.
- Combine similarity facts in the constellation and improve required-fact searches and key/tempo links.

### Playlists and maintenance

- Increase smart-playlist capacity to 99 tracks, with genre-family, mood, harmonic-key, and tempo flow ordering.
- Improve main-genre ranking/filtering, CLAP genre coverage, and library-gap backfill controls.
- Include the opt-in dirty-record maintenance script and updated schema/API/Makefile documentation. The cleanup script is not run automatically.

### Running and upgrading

- Start the host control API (:8765) and MCP/graph server (:8888), then run `make dashboard` or `karaoke-dashboard` and open http://127.0.0.1:8900.
- The built frontend is included in the Python wheel. Rebuild source changes with `npm ci --prefix src/karaoke/webapp` and `make dashboard-build`.
- Restart the dashboard after upgrading Python code and hard-refresh the browser after frontend changes.
- OpenSearch supplies derived artist/search data; Ollama powers chat; existing Celery workers execute full song processing. Queueing requires a running YouTube Music kiosk and a queueable source.
- The dashboard is an unauthenticated host control surface. Use a trusted local network or set `DASHBOARD_HOST=127.0.0.1`; do not expose it directly to the internet.
