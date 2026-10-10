# Serving Textual in the Browser (Textual-Serve)

To expose the karaoke TUI as a web frontend on the local network without rewriting the UI, the project uses `textual-serve`.

## Setup
1. **Dependencies:** `textual-serve` is included in the `web` optional dependencies in `pyproject.toml`.
2. **Runner:** Instead of relying on the `textual-serve` CLI binary (which can have PATH issues in virtual environments), the server is invoked programmatically via `scripts/web_serve.py`:
   ```python
   from textual_serve.server import Server
   Server("karaoke-tui", port=8080, host="0.0.0.0").serve()
   ```
3. **Make target:** Run via `make web`.

## Pitfalls: Web Layout Latency
Native terminal sizing is synchronous. In web mode, layout calculations over the WebSocket are asynchronous and slightly delayed.
- **Zero-Size Widgets:** Code that reads `widget.content_size.width` or `height` immediately after a state change (or from a background worker triggered by a state change) will often see `0` in web mode.
- **Symptom:** Dynamic visual elements (like the mood square or cover art pixel renderer) that check `if cols < 4: return` will silently abort because they measure the widget before the browser has completed the layout reflow.
- **Fix:** When querying `content_size` for rendering artwork, either provide a minimum hardcoded fallback size, or retry/delay the measurement until the layout settles.
