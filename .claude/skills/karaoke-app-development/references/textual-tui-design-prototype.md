# Textual TUI design/prototype workflow

Use this when adding or redesigning UI for the local `karaoke` terminal app.

## Pattern validated in session

For broad UI changes, design first in an editable drawing, then implement a clean Textual slice:

1. Create an editable `.excalidraw` concept under `docs/design/` plus a short Markdown brief.
   - Excalidraw is easy for the user to review and edit together.
   - Include the major panels before coding: settings/config, mode select, player/now-playing, scrolling lyrics, mood/sentiment square, ASCII/vector visual placeholder.
2. If the existing TUI path is broken or has accumulated hacks, start a new module instead of extending it.
   - Example: keep old `src/karaoke/browse.py` untouched and build a clean `src/karaoke/tui.py` prototype.
3. First implementation slice should be reviewable and low-risk:
   - Static Textual shell matching the approved drawing.
   - SQLite-backed library table.
   - Non-blocking open/play action via `subprocess.Popen`.
   - Lyric preview reusing existing pure renderer helpers (`LyricTimeline`, `render_lines`, `timeline_from_lyrics`).
   - Mood square reusing `sentiment.mood_of`.
   - ASCII/vector panel as placeholder only; keep vector/OpenSearch optional.
4. Wire a separate entrypoint/target so the user can review without relying on broken browse code.
   - Example entrypoint: `karaoke-tui = "karaoke.tui:tui_main"`.
   - Example target: `make tui`.
5. Verify before reporting done:
   - `PYTHONPATH=src .venv/bin/python -m compileall -q src/karaoke/tui.py`
   - a headless Textual `run_test()` smoke that queries stable widget ids such as `#topbar`, `#mood-square`, and `#library-table`
   - focused TUI helper tests plus the full pytest suite.

## Pitfalls

- Do not continue wiring behavior into a broken previous TUI just because it exists. A clean prototype is better when the user approves a new design.
- Do not use emoji or exotic glyphs in the first Textual prototype if tests/rendering may run in varied terminals; simple ASCII mood glyphs are safer.
- In Textual 8.x, `Static` does not expose a `.renderable` attribute in the tested environment. Smoke tests should assert widget presence, ids, and table row counts rather than reading `.renderable`.
- Avoid making vector/OpenSearch required for playback or UI review. SQLite remains the source of truth; vector search is optional for later semantic visuals.

### `textual-serve` Web Frontend Fallback
If the TUI is required to be cast to an external display (like a tablet remote control) rather than run directly in a terminal window, `textual-serve` acts as an instantaneous zero-rewrite option:
- Set up `web_serve.py` running `Server("karaoke-tui", port=..., host="0.0.0.0")`.
- **Limitation:** As it bridges a terminal UI to an HTML canvas via WebSockets, heavy continuous text animations (like high-refresh-rate cartwheels or large pixel blocks) can stutter or fail to render immediately. When adapting the TUI for `textual-serve`, prefer sparse, low-frequency updates or discrete blocks (like PyFiglet text and simple ASCII equalizers) over dense, rapidly redrawing background-color pixel animations.
