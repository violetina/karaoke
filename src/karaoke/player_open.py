"""Launching song URLs in the desktop player/browser.

Split out of :mod:`karaoke.browse` so that callers which only need to open a
URL — the host-side control API, for example — do not pull in Textual and the
whole TUI stack just to spawn ``xdg-open``.
"""
from __future__ import annotations

import subprocess

from .logger import OPEN_STDERR_LOG, OPEN_STDOUT_LOG, log


def try_chrome_cdp_navigate(url: str) -> bool:
    """Try to navigate an existing Chromium window/tab to the new URL using CDP.

    Returns True on success, False otherwise.
    """
    import json
    import urllib.request
    import asyncio
    import threading

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

        # Run send_nav in a background thread to bypass "running event loop" restriction
        success = [False]
        def thread_target():
            try:
                asyncio.run(send_nav())
                success[0] = True
            except Exception:
                pass

        t = threading.Thread(target=thread_target)
        t.start()
        t.join(timeout=2.0)
        return success[0]
    except Exception as exc:
        log.exception("try_chrome_cdp_navigate failed")
        return False


def open_song_url(url: str, kind: str | None) -> int | None:
    """Open a song URL and return the spawned process id when applicable.

    YouTube/browser URLs are opened asynchronously so the TUI remains responsive.
    stdout/stderr are captured to log files so xdg-open failures are debuggable.
    """
    # Pause any other active players first so audio does not overlap!
    try:
        proc = subprocess.run(
            ["playerctl", "--list-all"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if proc.returncode == 0:
            for p in proc.stdout.strip().splitlines():
                p = p.strip()
                if p:
                    subprocess.run(
                        ["playerctl", "--player", p, "pause"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=1,
                    )
    except Exception:
        pass

    # For Spotify, convert URIs to HTTPS Web Player links and fall through
    # so they play in the dedicated browser window instead of launching/relying
    # on the native Spotify desktop app!
    if kind == "spotify" or "spotify.com" in url.lower() or url.startswith("spotify:"):
        if url.startswith("spotify:track:"):
            track_id = url.split(":")[-1]
            url = f"https://open.spotify.com/track/{track_id}"
        kind = "spotify_web"

    # Automatically upgrade standard YouTube links to YouTube Music links for superior audio
    if kind == "youtube" or "youtube.com" in url.lower() or "youtu.be" in url.lower():
        from .localcache import extract_youtube_id
        vid = extract_youtube_id(url)
        if vid:
            url = f"https://music.youtube.com/watch?v={vid}"
            kind = "youtube_music"
        elif "/results?search_query=" in url:
            # A search URL has no video id to convert, but its query does carry
            # over — so the no-URL fallback lands in the same player as
            # everything else instead of dropping the user into plain YouTube.
            from urllib.parse import parse_qs, quote_plus, urlparse
            query = parse_qs(urlparse(url).query).get("search_query", [""])[0]
            if query:
                url = f"https://music.youtube.com/search?q={quote_plus(query)}"
                kind = "youtube_music_search"

    # Try to navigate an active kiosk/debugging browser first to avoid tab clutter!
    if try_chrome_cdp_navigate(url):
        log.info("Navigated active kiosk-mode Chrome via CDP: %s", url)
        return None

    OPEN_STDOUT_LOG.parent.mkdir(parents=True, exist_ok=True)
    stdout = OPEN_STDOUT_LOG.open("ab")
    stderr = OPEN_STDERR_LOG.open("ab")
    try:
        log.debug("Executing: xdg-open %s", url)
        proc = subprocess.Popen(["xdg-open", url], stdout=stdout, stderr=stderr)
        log.info(
            "xdg-open spawned pid=%s stdout=%s stderr=%s",
            proc.pid,
            OPEN_STDOUT_LOG,
            OPEN_STDERR_LOG,
        )
        return proc.pid
    finally:
        stdout.close()
        stderr.close()
