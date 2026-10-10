# TUI design prototyping notes

Use this when the user wants to co-design a larger karaoke TUI layout before implementation.

## Durable workflow

1. Recover the original/current UI anchor before drawing. For the karaoke app, the first TUI idea is `karaoke-browse`: a Textual app that lists songs from SQLite, supports arrow-key navigation, and opens/plays the selected source with `xdg-open` or `playerctl open`.
2. Inspect current anchors before changing code:
   - `src/karaoke/browse.py` for the existing Textual browser and source-opening flow.
   - `src/karaoke/player.py` for `LyricTimeline`, scrolling lyrics, word highlight, and mood colour maps.
   - `src/karaoke/sentiment.py` for the dependency-free mood classifier.
3. For multi-panel UI work, create an editable design artifact first, not just a prose plan. Prefer `docs/design/<concept>.excalidraw` plus `docs/design/<concept>.md` so the user can edit it in Excalidraw/tldraw-like tools.
4. Represent the intended panels explicitly:
   - settings/config and mode selection
   - player/now-playing state
   - actual scrolling lyrics area
   - mood/sentiment square
   - ASCII/text visual placeholder
   - optional vector-store/semantic-theme lane
5. Build the first implementation slice as a static Textual prototype before wiring real playback. A good starting point is a new module such as `src/karaoke/tui.py` with header, settings/mode panel, now-playing panel, lyric placeholder, visual panel, and footer bindings.
6. Only after layout agreement, move behavior incrementally from `browse.py` and `player.py` into reusable widgets.

## Pitfalls

- Do not skip directly from a broad UI idea to invasive playback rewiring. First stabilize the visual vocabulary and panel layout.
- Keep SQLite as the required source of truth. OpenSearch/vector store features should stay optional derived enhancements for semantic theme lookup and future text/ASCII visuals.
- For user-editable diagrams, Excalidraw `.excalidraw` JSON is a practical local format: no account needed; drag into https://excalidraw.com/ to edit.
