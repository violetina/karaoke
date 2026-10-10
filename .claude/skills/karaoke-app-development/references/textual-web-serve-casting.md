# Textual Web Serve Casting (`textual-serve`)

The karaoke TUI can be cast to a web browser using `textual-serve`. This provides an instant web UI without rewriting the Textual application, making it ideal for "remote control" casting.

## Setup
1. Ensure `textual-serve` is installed in the local `.venv` (it is an optional dependency under `[project.optional-dependencies] web` in `pyproject.toml`).
2. Use a dedicated script (e.g., `scripts/web_serve.py`) to launch the server rather than trying to run `textual serve` directly if the CLI is misconfigured.
   ```python
   import argparse
   from textual_serve.server import Server

   def main():
       parser = argparse.ArgumentParser()
       parser.add_argument("--port", type=int, default=8080)
       args = parser.parse_args()
       server = Server("karaoke-tui", port=args.port, host="0.0.0.0")
       server.serve()

   if __name__ == "__main__":
       main()
   ```
3. Expose this via a `make web` target in the `Makefile`.

## Known Limitations and Pitfalls
- **Animations Stutter:** High-frequency text replacement animations (like the beat-synced cartwheel/rhythm ASCII animation in `visuals.py`) can stutter or fail to render smoothly. This is a limitation of bridging rapid terminal frames over a WebSocket into an HTML canvas.
- **Dense Background Colors:** Album art (`coverart.py`), which is rendered using dense blocks of background-colored spaces, might struggle in the web canvas (`xterm.js`) compared to a native terminal emulator.
- **Responsive Layout Hiding:** If the browser window (or mobile device) is narrower than `NARROW_COLS` (110 columns), the `KaraokeTui` responsive design will automatically hide the left sidebar (cover art) and right visuals panel (animations/stats). The user needs to zoom out (Cmd/Ctrl + Minus) or resize the window to increase the column count and bring the panels back.