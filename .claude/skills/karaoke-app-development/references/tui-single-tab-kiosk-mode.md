# Single-Tab Browser Kiosk Mode and CDP Tab Reuse

This reference documents the design, implementation, and debugging workflows established for reusing a single browser tab via the Chrome DevTools Protocol (CDP) and avoiding overlapping media playback in the `karaoke` TUI.

## 1. The Multi-Tab Browser Clutter Problem
In traditional terminal music applications, pressing Enter on a song launches a browser with a standard `xdg-open` subprocess. If the user does this repeatedly:
* Multiple browser tabs are spawned, each running a heavy YouTube/YouTube Music video player.
* Sound from multiple tracks starts overlapping and playing simultaneously, creating a very noisy and frustrating user experience.
* The system is filled with hundreds of orphan browser tabs after several hours of TUI browsing.

## 2. Kiosk-Mode Single-Tab CDP Navigation Fallback
To solve tab clutter and overlapping audio, we implement a **CDP navigation engine** inside `player_open.py`.

### A. The Independent Kiosk Process
Chrome/Chromium exposes a WebSocket-based remote debugging API on port `9222`. However, Chrome has a single-process model; if a main browser is already running, launching a new browser window on port `9222` will be intercepted and the debugging flag will be ignored.
To force a separate process with active port `9222`, Chrome must be launched with a **dedicated user data directory**:
```bash
google-chrome --app="https://music.youtube.com" --remote-debugging-port=9222 --user-data-dir=~/.config/google-chrome-kiosk
```
* `--app` removes the address bar, tabs, and standard browser chrome for a clean frameless media-player look.
* `--user-data-dir` isolates the sandbox, guaranteeing that port `9222` listens successfully.

### B. CDP WebSocket Navigation Code
The python backend queries the local debugging port and uses the `websockets` library (already installed in the project environment) to navigate the active page target in place:

```python
def try_chrome_cdp_navigate(url: str) -> bool:
    import json
    import urllib.request
    import asyncio

    try:
        # Check if the debugging API is responsive
        req = urllib.request.Request("http://localhost:9222/json", method="GET")
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            tabs = json.loads(resp.read().decode())

        # Find the first Page/Tab target
        target = None
        for tab in tabs:
            if tab.get("type") == "page" and "webSocketDebuggerUrl" in tab:
                target = tab
                break
        if not target:
            return False

        ws_url = target["webSocketDebuggerUrl"]

        # Import websockets library to send the CDP navigate command
        import websockets

        async def send_nav():
            async with websockets.connect(ws_url) as ws:
                payload = {
                    "id": 1,
                    "method": "Page.navigate",
                    "params": {"url": url}
                }
                await ws.send(json.dumps(payload))
                await asyncio.wait_for(ws.recv(), timeout=1.0)

        asyncio.run(send_nav())
        return True
    except Exception:
        return False
```

### C. Pausing Overlapping Media Players
Before attempting any new url launch (whether falling back to `xdg-open` or successful CDP navigation), we pause any other active MPRIS media players on the desktop so audio never overlaps:
```python
try:
    proc = subprocess.run(["playerctl", "--list-all"], capture_output=True, text=True, timeout=2)
    if proc.returncode == 0:
        for p in proc.stdout.strip().splitlines():
            p = p.strip()
            if p:
                subprocess.run(["playerctl", "--player", p, "pause"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=1)
except Exception:
    pass
```

---

## 3. Splitting display names from control names in MPRIS
When browsers display YouTube tabs, the tab title is heavily branded (e.g. `Song Title - YouTube`).
To display pretty labels in the TUI, `playerctl.py` rewrites `player` to `"YouTube"` or `"YouTube Music"`.
* **The Pitfall:** Overriding `player` with a friendly label breaks all target selectors for playerctl command wrappers (like `position()`, `status()`, `play_pause()`), because `YouTube` is not a valid MPRIS bus name and results in `"No players found"`.
* **The Fix:** Maintain separate fields in `PlayerMetadata` and `Detection`:
  1. `player`: friendly branded label for the TUI header (`"YouTube Music"`).
  2. `mpris_name` / `mpris`: raw actual MPRIS identifier (`"chromium.instance123"`) used as the `--player` target for playerctl.
