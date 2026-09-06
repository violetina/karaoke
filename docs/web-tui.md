# Web TUI (`make web`)

The karaoke platform includes a **Web TUI** mode that serves the full Textual terminal user interface over HTTP and WebSockets directly into any web browser.

## Running the Web TUI

Start the web server with:

```bash
make web
# or python scripts/web_serve.py
```

It binds to **`http://127.0.0.1:8001`** by default (port configurable via `PORT` or `HOST` environment variables, keeping it distinct from `karaoke-api` on port `8000` and MkDocs on port `8085`).

## Architecture & Features

- **`textual-serve` Backend**: Wraps `python -m karaoke.tui` inside a browser-compatible WebSocket bridge. No separate frontend code is required — the exact same TUI code runs both in your terminal (`make tui`) and in your browser.
- **Full Interactivity**:
  - **Library & Browse**: Browse all library tracks with artist, title, key, BPM, energy, genre, and play counts.
  - **Mood & Energy Slider**: Adjust energy floor (`-` / `+` / `m`) and filter songs in real-time.
  - **Sort Options**: Sort by Artist (A-Z), Priority: Least Played, Most Played, Energy, BPM, or Key.
  - **Playlist Controls**: Enqueue (`a`), Shuffle (`U`), Clear (`C`), and Play-Once auto-advance.
  - **Time-Synced Lyrics**: Live synced lyric rendering and playhead tracking.

## Related

- [The Terminal TUI](tui.md)
- [Control API](control-api.md)
- [Makefile targets](makefile-targets.md)
