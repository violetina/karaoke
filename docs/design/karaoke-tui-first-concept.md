# Karaoke TUI first editable concept

Editable drawing: `docs/design/karaoke-tui-first-concept.excalidraw`

Open it by dragging the `.excalidraw` file into https://excalidraw.com/ or any Excalidraw-compatible editor. This is easier to edit together than trying to hand-edit terminal box art.

## Source idea recovered

The original TUI idea was the `karaoke-browse` Textual app:
- list known songs from SQLite
- move with arrows
- press Enter to open/play a source URL
- use `xdg-open` for web/YouTube and `playerctl open` for Spotify

Current implementation anchor: `src/karaoke/browse.py`.

## Expanded design areas

1. Settings/config panel
   - mode selector: browse, player, radio/listen/output, YouTube URL
   - player target selector: Spotify, VLC, Firefox, local file
   - sync knobs: lead, offset, nudge
   - visual profile selector

2. Player panel
   - now playing metadata
   - source and cache state
   - basic playback controls

3. Scrolling lyrics panel
   - reuse `LyricTimeline` and existing word-level highlight logic from `src/karaoke/player.py`
   - make this a Textual widget later instead of only Rich live rendering

4. Sentiment square
   - reuse `src/karaoke/sentiment.py`
   - map mood to colour and/or glyph animation

5. Future ASCII/vector visuals
   - keep SQLite as required source of truth
   - use OpenSearch/vector store only as optional derived semantic theme lookup
   - later flow: lyric meaning/theme -> visual prompt/motif -> image or generated shape -> text/ASCII renderer

## Suggested first build slice

Implemented as `src/karaoke/tui.py` and launched with:

```bash
make tui
```

Current scope:
- Header / navigation bar
- left settings/config panel
- center now-playing + scrolling lyrics preview
- library table loaded from SQLite
- right mood square + ASCII/vector placeholder
- footer key bindings

Then move one behavior at a time from `player.py`, `playerctl.py`, and the SQLite cache into widgets. Avoid depending on the older broken `browse.py` code path.

## Implemented so far

- `src/karaoke/detect.py` — auto player detection + mode (spotify / scan / browse),
  URL-based lyric resolution, `launch_spotify()`, gap recording.
- `src/karaoke/tui.py` — player-aware TUI: `m` cycles the mode override (auto →
  browse → spotify → scan; forcing spotify launches the app), `space/n/p/[/]`
  controls, `l` cycles console log level, staging-view Enter whitelists a
  candidate (confirm modal), library filter (working / all / staging).
- `src/karaoke/musictheory.py` — pure keys/relatives/Camelot + `reconcile_key`
  (detected Am vs online C major are recognised as relative = same tonality).
- `src/karaoke/analyze.py` — Essentia `edma` multi-window voted key + librosa
  BPM, ffmpeg transcode for webm/opus. Benchmarked 4/4 exact on the vetted
  ground-truth pop set (a smoke test, not a full benchmark).
- `src/karaoke/track_analysis.py` — stores detected/reference/resolved key + BPM;
  `verify_key()` reconciles an online key and persists the outcome.
- `src/karaoke/visuals.py` — sentiment arc/bars + rhythm bar shown in the free
  visual space, alongside detected key / BPM / tempo word.

CLI: `karaoke-tui`, `karaoke-analyze` (`--file`, `--verify --key`, `--list`).
Audio stack is isolated: `make install-audio` (or `pip install -r
requirements-audio.txt`). Console logs: `KARAOKE_LOG=off|err|info|full`.

