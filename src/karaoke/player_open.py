"""Launching song URLs in the desktop player/browser.

Split out of :mod:`karaoke.browse` so that callers which only need to open a
URL — the host-side control API, for example — do not pull in Textual and the
whole TUI stack just to spawn ``xdg-open``.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from .logger import OPEN_STDERR_LOG, OPEN_STDOUT_LOG, log


CDP_PORT = 9222
CDP_URL = f"http://localhost:{CDP_PORT}/json"


def _cdp_page_socket(timeout: float = 1.0) -> "str | None":
    """WebSocket URL of the kiosk browser's first page target, or None."""
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(CDP_URL, timeout=timeout) as resp:
            tabs = json.loads(resp.read().decode())
    except Exception:
        return None
    for tab in tabs:
        if tab.get("type") == "page" and "webSocketDebuggerUrl" in tab:
            return tab["webSocketDebuggerUrl"]
    return None


def _cdp_send(method: str, params: dict, *, timeout: float = 2.0):
    """Send one CDP command and return its result, or None.

    Runs the socket work on its own thread with ``asyncio.run``: the callers
    here are Textual workers and timers that may already have a running event
    loop, which ``asyncio.run`` refuses to nest.
    """
    import asyncio
    import json
    import threading

    ws_url = _cdp_page_socket()
    if not ws_url:
        return None
    try:
        import websockets
    except ImportError:
        log.debug("websockets not installed; CDP unavailable")
        return None

    out: dict = {}

    async def send():
        async with websockets.connect(ws_url) as ws:
            await ws.send(json.dumps({"id": 1, "method": method, "params": params}))
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            out["reply"] = json.loads(raw)

    def run() -> None:
        try:
            asyncio.run(send())
        except Exception:
            log.debug("CDP %s failed", method, exc_info=True)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=timeout + 1.5)
    return out.get("reply")


def try_chrome_cdp_navigate(url: str) -> bool:
    """Navigate the existing kiosk window to a URL. True on success."""
    reply = _cdp_send("Page.navigate", {"url": url})
    return reply is not None


# Reads the page's own <video> element. MPRIS reports a position but not
# reliably whether a track *finished*: when YouTube Music moves on to its own
# suggestion the metadata simply changes, which is indistinguishable from the
# user picking something. The video element says plainly that it ended.
_PLAYBACK_JS = """(() => {
  const v = document.querySelector('video');
  if (!v) return JSON.stringify({present: false});
  return JSON.stringify({
    present: true,
    ended: !!v.ended,
    paused: !!v.paused,
    position: v.currentTime || 0,
    duration: (isFinite(v.duration) ? v.duration : 0) || 0,
    readyState: v.readyState || 0,
    url: location.href
  });
})()"""


def browser_playback() -> "dict | None":
    """What the kiosk browser's video element is doing, or None.

    Returns ``present``, ``ended``, ``paused``, ``position``, ``duration``,
    ``readyState`` and ``url``.
    """
    import json

    reply = _cdp_send("Runtime.evaluate",
                      {"expression": _PLAYBACK_JS, "returnByValue": True})
    if not reply:
        return None
    try:
        raw = reply["result"]["result"]["value"]
        return json.loads(raw)
    except (KeyError, TypeError, ValueError):
        return None


def track_finished(state: "dict | None", *, tail: float = 1.5) -> bool:
    """Whether the browser's current track has reached its end.

    ``tail`` catches the case where playback is swapped out a moment before
    ``ended`` is set, which is what happens when the site starts its own next
    track: by the time ``ended`` would be true the element already holds the
    replacement.
    """
    if not state or not state.get("present"):
        return False
    if state.get("ended"):
        return True
    duration = float(state.get("duration") or 0.0)
    position = float(state.get("position") or 0.0)
    return duration > 0 and position >= (duration - tail)


def track_idle(state: "dict | None") -> bool:
    """Whether the element holds nothing that could ever play.

    YouTube Music keeps a ``<video>`` on every page, including artist and
    search pages, so navigating off a watch URL leaves an element that is
    present but empty. That state never satisfies :func:`track_finished` --
    it has no duration and will never set ``ended`` -- so a queue waiting for
    the track to end waits forever. ``readyState`` is what separates the two:
    a live stream also reports no duration, but it has data.

    Idle is not the same as finished: a watch URL reads as idle for a moment
    while it loads. Callers must see it hold before acting on it.
    """
    if not state or not state.get("present"):
        return False
    if state.get("ended"):
        return False
    try:
        ready = int(state.get("readyState") or 0)
    except (TypeError, ValueError):
        return False
    return ready == 0  # HAVE_NOTHING: no source loaded at all


def _kiosk_mpris_names(names: "list[str]") -> "set[str]":
    """Which of ``names`` are the browser we drive over CDP.

    playerctl names Chrome's bus ``chromium.instanceNNN`` after the browser
    process id, so reading that process's cmdline tells us whether it is the
    window we are about to navigate.
    """
    kiosk = set()
    for name in names:
        _, sep, pid = name.partition(".instance")
        if not sep or not pid.isdigit():
            continue
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
        except OSError:
            continue
        if f"--remote-debugging-port={CDP_PORT}" in cmdline:
            kiosk.add(name)
    return kiosk


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
            names = [p.strip() for p in proc.stdout.strip().splitlines() if p.strip()]
            # ...but never the kiosk window itself. On a dedicated box it is
            # the only MPRIS player there is, and pausing the very browser we
            # are about to navigate leaves it parked at 0:00 whenever the site
            # does not autoplay the new URL.
            kiosk = _kiosk_mpris_names(names)
            for p in names:
                if p in kiosk:
                    continue
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
