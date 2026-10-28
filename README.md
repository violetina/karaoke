# Karaoke

A local-first karaoke, music-library and AI DJ platform for Linux. Follow desktop
playback with synced lyrics, explore songs and artist relationships, and manage a
YouTube Music queue from the terminal or the unified web dashboard.

[Latest release](https://github.com/violetina/karaoke/releases/latest) ·
[Changelog](https://github.com/violetina/karaoke/blob/main/CHANGELOG.md) · [Architecture](https://github.com/violetina/karaoke/blob/main/docs/architecture.md) ·
[Service map](https://github.com/violetina/karaoke/blob/main/docs/platform-services.md) · [Makefile commands](https://github.com/violetina/karaoke/blob/main/docs/makefile-targets.md)

## What it does

- **Live karaoke:** synced lyrics with word highlighting, desktop-player following,
  radio/microphone identification and a fullscreen stage view.
- **Unified dashboard:** Vue 3 + FastAPI, animated mood art with live chord overlays,
  mood-over-time timelines, song details, play counts and playlist membership.
- **Artist knowledge:** MusicBrainz identities, Wikidata facts, band members and side
  projects, with ID-based navigation rather than partial-name substitutions.
- **Music discovery:** local title/artist/lyric search plus optional OpenSearch lyric,
  timbre, mood and harmonic similarity graphs.
- **Playlists and controls:** transport, seek, library browsing, saved/smart playlists
  and explicit play/queue actions. Smart playlists hold up to 99 tracks.
- **AI DJ:** an embedded Ollama chat for recommendations and music questions. Chat
  answers do not automatically play or queue songs; queue suggestions require a click.
- **Background enrichment:** source resolution, audio analysis, chords, mood,
  classification, lyric alignment and vector indexing through Celery workers.

## Storage and dependencies

**PostgreSQL is the operational source of truth.** Tracks, sources, approved lyrics,
artist identities, play history, recordings and saved playlists live there. Configure
it with `KARAOKE_PG_URL`; see [Database backend](https://github.com/violetina/karaoke/blob/main/docs/database.md) for schema,
connection settings and migration details. The application no longer uses the old
`karaoke.db` SQLite file for its library.

OpenSearch is a **derived, rebuildable index**, not the playback database. Cached
lyric lookup and local library search can work without it, provided PostgreSQL is
available. Unknown-song recognition, LRCLIB requests and external metadata lookups
still need internet access. Celery's task-result store may separately use
`celery-results.sqlite`; that is not the music library.

| Component | Purpose | Default port |
| --- | --- | --- |
| PostgreSQL | Operational library and event data | 5432 |
| Library API | Read-only tracks, lyrics and stats | 8000 |
| Host control API | Desktop playback, stage state, `/tv` and `/mood` | 8765 |
| MCP / graph server | AI tools, playlists and `/graph` | 8888 |
| Unified dashboard | Browser frontend and aggregation API | 8900 |
| OpenSearch | Derived artist and vector search indexes | 9200 |
| Ollama | Local AI DJ model | 11434 |
| RabbitMQ | Celery task broker | 5672 |
| Flower | Worker/task monitoring | 5555 |

The host control API and kiosk need a Linux desktop session. Playback uses MPRIS
and/or Chrome CDP. Local files are optional audio sources; Spotify supplies metadata
and playback integration, not downloadable audio.

## Install from source

Python 3.11+ is declared in `pyproject.toml`; the local development environment uses
Python 3.14. Install system media tools and provision PostgreSQL before running the
platform. Follow the [service runbook](https://github.com/violetina/karaoke/blob/main/docs/howto-systemd-services.md) and
[database guide](https://github.com/violetina/karaoke/blob/main/docs/database.md) for the complete setup.

```bash
git clone https://github.com/violetina/karaoke.git
cd karaoke
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[dev]"
```

Audio analysis and YouTube support have additional dependencies:

```bash
python -m pip install -e ".[youtube,beats]"
make install-audio   # optional isolated analysis environment
```

Use your own PostgreSQL connection string in `KARAOKE_PG_URL`. Do not use a real
library database for destructive maintenance or test fixtures. Downloaded audio and
artwork normally live under `~/.local/share/karaoke`, configurable with
`KARAOKE_DATA_DIR`.

## Run the unified dashboard

With PostgreSQL available, start these in separate terminals, or use the existing
host service setup if they are already running:

```bash
make ctrl-api     # host playback and stage state, :8765
make mcp          # graph, playlists and MCP tools, :8888
make dashboard    # unified browser dashboard, :8900
```

Open <http://127.0.0.1:8900>. The release wheel includes the built frontend, and
`karaoke-dashboard` is also available after package installation. OpenSearch enables
the derived search/artist features; Ollama powers chat. Full analysis requires the
appropriate Celery workers, RabbitMQ and audio models.

### Dashboard behaviour

- **Left-click a graph node:** inspect the artist without interrupting playback.
- **Right-click a song node:** Play now, Queue next, Add to AI DJ playlist, or process.
- **Song change:** refresh the artist pane and library list; resolve new artist links.
- **Mood square:** the `/mood` canvas animation, with chords overlaid inside it.
- **Mood over time:** stored feeling/arc, swing count and coloured timeline segments.
- **AI DJ replies:** informational by default. Click a queue suggestion to act on it.

Queue next needs a running YouTube Music kiosk and a queueable YouTube source. It
never falls back to Play now. Word highlighting uses exact word timings when
available and estimated word positions for line-only synced lyrics.

### Frontend development

Node.js is needed only to build or develop the Vue frontend, not to serve the built
release. CI builds it with Node.js 22.

```bash
npm ci --prefix src/karaoke/webapp
make dashboard-build
node tests/test_dashboard_chat_safety.mjs
```

Restart `make dashboard` after changing Python code. After a frontend rebuild,
hard-refresh the browser. The dashboard is separate from the library/control APIs;
restarting the gateway or kiosk is not a substitute for restarting it.

### Dashboard configuration and safety

| Variable | Purpose |
| --- | --- |
| `DASHBOARD_HOST`, `DASHBOARD_PORT` | Dashboard bind address and port |
| `KARAOKE_CTRL_API_URL` | Host control API upstream |
| `KARAOKE_MCP_API_URL` | MCP/graph server upstream |
| `OPENSEARCH_URL` | Derived search service |
| `OLLAMA_URL`, `KARAOKE_DJ_MODEL` | Chat server and model |

**The dashboard has no authentication and defaults to binding on all interfaces.**
Keep it on a trusted network, or bind it to loopback:

```bash
DASHBOARD_HOST=127.0.0.1 make dashboard
```

Do not expose playback controls, Chrome CDP or the dashboard directly to the
internet. Kubernetes examples in this project target `kind-karaoke`; always select
that context explicitly and do not run project deployment commands against work or
production clusters.

## Terminal and fullscreen interfaces

```bash
make tui                                    # interactive karaoke TUI
make admin                                  # operations and worker management
make dj                                     # terminal AI DJ chat
karaoke "R.E.M. - Losing My Religion"         # resolve a song and render its lyrics
karaoke --player                            # follow a desktop MPRIS player
karaoke --spotify                           # follow Spotify playback
karaoke --listen                            # identify room audio through songrec
karaoke --output                            # identify audio playing on this machine
karaoke --radio                             # continuous identification/re-locking
karaoke --file song.mp3                      # use local file metadata
karaoke --print "Queen - Bohemian Rhapsody"   # print lyrics without the live UI
```

Fullscreen views: <http://127.0.0.1:8765/tv>,
<http://127.0.0.1:8765/mood> and <http://127.0.0.1:8888/graph>.

## Ingest, enrich and search

```bash
make folder-scan DIR=~/Music LIMIT=50 DRY_RUN=1 # preview a local scan
make index-youtube-cache                       # ingest downloaded audio into PostgreSQL
make artists                                  # resolve artists, facts and genres
make artist-index                             # rebuild the derived artist index
make vector-index-dry-run                      # preview track/lyric indexing
make vector-index LINES=1                       # rebuild derived OpenSearch vectors
make stats                                    # play/discovery statistics
lyricsearch "religion faith losing my belief"   # semantic lyric search
lyricsearch --keyword "teen spirit"             # keyword search
```

See the [ingestion quickstart](https://github.com/violetina/karaoke/blob/main/docs/howto-quickstart-runbook.md),
[post-processing guide](https://github.com/violetina/karaoke/blob/main/docs/howto-post-processing.md),
[search guide](https://github.com/violetina/karaoke/blob/main/docs/search.md), [vector reference](https://github.com/violetina/karaoke/blob/main/docs/vectors.md) and
[artist metadata overview](https://github.com/violetina/karaoke/blob/main/docs/artist_metadata_plan.md).

### Lyrics and YouTube

LRCLIB supplies plain and synced lyrics. Whisper can transcribe missing words or
align known lyrics to audio. Lower-trust captions go through a staging/review queue:

```bash
karaoke-yt URL --download
karaoke-stage youtube URL
karaoke-stage list
karaoke-stage show 12
karaoke-stage approve 12   # review the candidate before approving it
```

Use `make auth-youtube` / `make auth-spotify` for the dedicated playback browser
profile. Download/caption authentication is separate; see
[browser cookies](https://github.com/violetina/karaoke/blob/main/docs/browser-cookies.md). Spotify does not provide downloadable
audio, and YouTube downloads/cookie automation may be restricted by service terms.

## Operations and verification

```bash
make systemd-status          # inspect existing host services
make health                  # check services, database and upstreams
make test-audio               # audio/recognition/lyrics prerequisites
make mic-test                 # microphone meter
make test                     # Python test suite
make lint                     # Python compile checks
make docs-audit               # documentation drift check
.venv/bin/mkdocs build --strict
```

Python tests require a PostgreSQL server. The fixture pins its dedicated test
database; see `tests/conftest.py` before running against a new environment. The
JavaScript chat-safety regression runs separately with the command above.

## Repository layout

```text
src/karaoke/                  Python application and APIs
src/karaoke/webapp/           Vue frontend source and npm lockfile
src/karaoke/webapp_dist/      Prebuilt frontend served by FastAPI
tests/                       Python and JavaScript regression tests
deploy/                      Host services and Kubernetes manifests
scripts/                     Ingestion, maintenance and migration tools
docs/                        MkDocs guides, API reference and historical plans
```
