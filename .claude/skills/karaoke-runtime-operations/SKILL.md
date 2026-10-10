---
name: karaoke-runtime-operations
description: Use when operating the karaoke runtime.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [karaoke, systemd, textual-serve, sqlite, opensearch, celery, ingestion]
    related_skills: [karaoke-app-development, karaoke-lyrics-platform]
---

# Karaoke Runtime Operations

Use this skill for runtime changes and investigations involving the karaoke Web TUI, kiosk browser, systemd user services, SQLite concurrency, large music-archive ingestion, OpenSearch vector refreshes, and local-versus-YouTube Music queue behavior.

## Runtime boundaries

- SQLite at `~/.local/share/karaoke/karaoke.db` is the operational source of truth.
- OpenSearch indexes are derived and can be rebuilt from SQLite.
- `karaoke-kiosk.service` owns Chrome/CDP on port 9222.
- `karaoke-webtui.service` owns textual-serve on port 8001 and spawns TUI WebSocket children.
- Celery/RabbitMQ handles explicitly published post-processing tasks; a direct folder scan is normally a sequential loop unless separately adapted to publish one task per file.

## Safe reload workflow

1. For Python changes, run `systemctl --user restart karaoke-webtui.service`.
2. For unit-file changes, run `make systemd-install` first, then restart the affected unit.
3. Verify with `systemctl --user status karaoke-webtui.service --no-pager -l` and an HTTP request to `http://127.0.0.1:8001/`.
4. Restart `karaoke-kiosk.service` only when Chrome/CDP state needs resetting; normal Python TUI changes do not require a kiosk restart.
5. Follow logs with `journalctl --user -u karaoke-webtui.service -f`.

The Web TUI unit should include `TimeoutStopSec=10` and `KillMode=control-group`. `textual-serve` can retain a WebSocket child or worker thread after Ctrl+C; bounded process-group cleanup prevents reloads from hanging indefinitely.

## SQLite concurrency

Large archive imports and interactive TUI reads can overlap. Open connections with a useful busy timeout (currently 30 seconds) and catch transient `database is locked` failures in interactive refresh paths. A lock should produce a warning/deferred refresh rather than terminate the TUI screen. Inspect the journal before calling a crash random.

Avoid launching multiple full writers unnecessarily. Vector rebuilds and archive scans should use their cross-process locks. A running importer started before a code change retains the old imported Python code until it is restarted.

## Large-library startup

Never load and render an entire archive on initial TUI mount. Use a bounded random wildcard sample; the current responsive default is 70 rows and startup does not autoplay. Typed searches may still query the full database. Keep SQL sampling and Textual rendering limits separate so the UI remains responsive as the archive grows.

## Queue authority

The local Karaoke queue and YouTube Music's temporary `TL...` playback queue are separate. When a local queue is populated, Follow Queue (`o`) must be enabled and local `next`/end-of-track handling must remain authoritative. Otherwise YouTube Music may advance to unrelated mix/autoplay content. Persistent YouTube Music playlists are useful as a recovery/export copy, but `ytmusicapi` does not directly control the ephemeral live queue; CDP does.

## Archive ingestion

The standard folder scan performs per-file tag extraction, optional fingerprinting, audio analysis, source resolution, SQLite ingestion, and optional vector work in a sequential loop. For unattended jobs:

- write per-file progress to a dedicated logfile;
- record `seen`, `processed`, `classified`, `sourced`, `skipped`, and `errors`;
- retain a checkpoint/manifest if interruption and efficient resume matter;
- do not claim that a full run is complete until the terminal output and database/index state agree;
- retry database-lock failures after the writer-heavy run rather than silently treating them as completed.

A naive rerun is usually idempotent at the database level but may repeat expensive audio analysis and online YouTube/Spotify searches.

## Vector refreshes

A recurring vector refresh should call the project vector-index command with the repository virtualenv and `PYTHONPATH=src`, use the existing vector rebuild lock, apply a bounded timeout, and report non-zero exits. Running it every 40 minutes is reasonable for incremental ingestion, but full re-embedding becomes increasingly expensive as the archive grows; add true missing-document/incremental selection before scaling beyond the current collection size.

## Visual renderer safety

ASCII animations must receive the actual widget width, not a hard-coded width. Keep actors inside their owned regions, drop colliding characters instead of merging sprites, and fall back to one complete compact actor when two figures cannot fit. Test narrow panels explicitly.

## Diagnostics checklist

- `systemctl --user status karaoke-webtui.service --no-pager -l`
- `journalctl --user -u karaoke-webtui.service -n 100 --no-pager`
- `curl -fsS http://127.0.0.1:8001/`
- `systemctl --user is-active karaoke-kiosk.service`
- `ss -lnt | grep -E ':8001|:9222'`
- inspect the dedicated ingestion logfile for progress and lock errors
- query SQLite counts and OpenSearch `_cat/indices` after long jobs

See `references/validated-runtime-lessons.md` for session-derived failure signatures and verified recovery recipes.
