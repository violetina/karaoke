# YouTube-cache ingest → analyse → vectorize runbook

The correct pipeline for getting cached/scanned audio into SQLite and searchable,
plus the make-target naming that trips people up. Full user-facing version lives
at `docs/howto-quickstart-runbook.md` in the repo.

## Pipeline (in order)
1. `make index-youtube-cache` — ingest cached YouTube files (`settings.youtube_dir`
   = `~/.local/share/karaoke/youtube`) into SQLite with a `youtube` source. Skips
   already-indexed files, safe to re-run. (Local folders instead: `make folder-scan
   DIR=~/Music LIMIT=50 DRY_RUN=1`.)
2. Analyse key/BPM/energy + genre — batch via workers: `make postprocess-enqueue-all`
   then drain with `karaoke-postprocess@{1..6}` systemd units or `make postprocess-worker`.
   One-shot convenience that also rebuilds vectors at the end:
   `PYTHONPATH=src .venv/bin/python scripts/fill_analysis_and_vector_gaps.py`.
3. `make vector-index LINES=1` — rebuild OpenSearch `tracks` + `tracks-lines`
   (`LINES=1` adds per-line lyric docs). Preview: `make vector-index-dry-run`.

Shortest path after a backfill already ran, when only new cache files were added:
`make index-youtube-cache && make vector-index LINES=1`.

## Text vs audio coverage (the #1 confusion)
- `tracks` / `tracks-lines` indexes are built from artist/title/lyrics TEXT — they
  cover EVERY track, including YouTube *links* with no downloaded audio.
- Genre (CLAP) and key/BPM/energy (Essentia) need the ACTUAL audio file on disk.
  Link-only tracks silently no-op there. So "missing genre=NNN / missing key=NN"
  staying high after a run is EXPECTED for a link-only library, not a failure —
  don't chase it as a bug; the audio must be downloaded first. (Real example this
  session: gap-fill reported "classified 660" but `track_genre` held only 257 rows.)

## Long re-embed runs past shell timeouts
A full ~30k-line re-embed takes minutes and can exceed a foreground shell timeout.
Do NOT assume failure — verify via the index state instead:
`curl -s 'http://localhost:9200/_cat/indices?v' | grep -E 'tracks |tracks-lines'`
(healthy = `green`, expected doc counts). Only heavy tasks hold the cross-process
lockfile, so a stray second rebuild is skipped rather than clobbering.

## Make-target naming (do NOT guess)
Real targets are UNPREFIXED: `index-youtube-cache`, `folder-scan`, `analyze`
(single-file only: `FILE=/ARTIST=/TITLE=`), `vector-index`, `vector-index-dry-run`,
`postprocess-enqueue-all`, `postprocess-worker`, `db-cleanup`.
- There is NO `find-sources` target — resolving sourceless tracks is ad-hoc
  `youtube.search(f"{artist} - {title}", limit=1)` in a script.
- The `karaoke-` prefix (e.g. `karaoke-folder-scan`, `karaoke-vector-index`) is the
  SYSTEMD UNIT naming style, not make targets.
