# Quickstart runbook: ingest → analyse → vectorize

A step-by-step for getting cached/scanned audio into the database and searchable.
Every command is a real `make` target (verified in the `Makefile`); the systemd
unit names (`karaoke-*`) are separate — see [systemd services](howto-systemd-services.md).

> **Key distinction that trips people up**
>
> - **Text indexes** (`tracks`, `tracks-lines`) are built from artist/title/lyrics —
>   they cover **every** track, including YouTube *links* with no downloaded audio.
> - **Genre (CLAP)** and **key/BPM/energy (Essentia)** need the **actual audio file**
>   on disk. Tracks that are only a YouTube/Spotify URL silently no-op in those steps
>   until the audio is downloaded. So "missing genre / missing key" counts staying
>   high after a run is expected for a link-only library, **not** a failure.

## The pipeline

### 1. Ingest cached YouTube downloads into SQLite

```bash
make index-youtube-cache
```

Scans `settings.youtube_dir` (`~/.local/share/karaoke/youtube`), resolves
artist/title for each cached file, and inserts a `youtube` source per track so it
shows up in browse. Already-indexed files are skipped, so it is safe to re-run.

### 1b. (Alternative) Scan a local music folder

```bash
make folder-scan DIR=~/Music                 # full run
make folder-scan DIR=~/Music LIMIT=50 DRY_RUN=1   # preview 50, write nothing
```

Fingerprints, classifies, resolves YouTube/Spotify sources, and ingests. Use this
for a folder of local audio files rather than the YouTube cache.

### 2. Analyse: key / BPM / energy + genre (needs local audio)

Batch path via the post-processing workers:

```bash
make postprocess-enqueue-all      # enqueue every track missing key/BPM or word-timing
```

Then drain the queue with either:

- the systemd workers: `systemctl --user start karaoke-postprocess@{1..6}` (see
  [systemd services](howto-systemd-services.md)), or
- a foreground worker: `make postprocess-worker`

Single file (ad-hoc):

```bash
make analyze FILE=/path/song.flac ARTIST="Artist" TITLE="Title"
```

**Gap-fill convenience script** — finds tracks missing analysis/genre, classifies
them, and rebuilds the vector index at the end in one shot:

```bash
PYTHONPATH=src .venv/bin/python scripts/fill_analysis_and_vector_gaps.py
```

### 3. Vectorize: rebuild OpenSearch indexes from SQLite

```bash
make vector-index-dry-run         # preview, no writes, no embedding
make vector-index LINES=1         # rebuild; LINES=1 also builds per-line lyric docs
```

This re-embeds `tracks` (per-track) and `tracks-lines` (per-lyric-line) documents.
It is a full rebuild and safe to re-run (documents are written by deterministic id,
so a rerun overwrites rather than duplicates). A full library re-embed of ~30k line
docs can take several minutes and may run past a short shell timeout — check
progress instead of assuming failure:

```bash
curl -s 'http://localhost:9200/_cat/indices?v' | grep -E 'tracks |tracks-lines'
```

Only heavy audio tasks (rebuild, gap-fill) hold a cross-process lockfile, so a
second rebuild started elsewhere is skipped rather than clobbering the first.

## Shortest path after a backfill already ran

If a backfill / gap-fill already handled analysis and you only added new cached
files:

```bash
make index-youtube-cache      # pull any new cache files into SQLite
make vector-index LINES=1     # re-embed into OpenSearch
```

Run `make postprocess-enqueue-all` (step 2) first only if the new tracks still
lack key/BPM/genre **and** have downloaded audio.

## Verify

```bash
make stats                                                   # play + library stats
curl -s 'http://localhost:9200/_cat/indices?v' | grep -E 'tracks|karaoke'
```

Healthy output shows `tracks` and `tracks-lines` as `green` with the expected
doc counts.

## Not make targets

These names look plausible but do **not** exist as targets:

- `find-sources` — resolving missing YouTube/Spotify sources for sourceless tracks
  is done ad-hoc via `karaoke.youtube.search()` in a script, not a make target.
- `karaoke-folder-scan` / `karaoke-analyze` / `karaoke-vector-index` — the
  `karaoke-` prefix is the **systemd unit** naming style. The make targets are
  `folder-scan`, `analyze`, `vector-index` (no prefix).
