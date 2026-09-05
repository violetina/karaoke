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


class _CdpChannel:
    """One CDP connection on one thread, reused by every caller.

    The previous version ran each command in a fresh thread with
    ``asyncio.run`` and then abandoned that thread on timeout::

        thread.start()
        thread.join(timeout=timeout + 1.5)   # gives up
        return out.get("reply")              # ...and walks away

    A thread that outlives the join keeps a whole event loop alive, and
    :func:`browser_playback` is called from a two-second timer, so every slow
    reply from Chromium leaked one. Observed in a live TUI: **46 stranded
    ``asyncio`` threads**, which on a 24-core box cannot even come from one
    executor -- its cap is 28 -- so they were 46 separate loops.

    They cost more than memory. Quitting joins them, and anyio replaces
    CPython's bounded wait with ``thread.join()`` carrying no timeout at all
    (``anyio/_backends/_asyncio.py``), so the 300-second warning fires and then
    shutdown blocks for good. That is issue #39.

    One connection, one thread, so there is nothing left to strand. A caller
    that times out gives up on its *reply*; the thread stays and serves the
    next request. Commands are serialised by an ``asyncio.Lock`` held inside
    the loop rather than a lock held across threads, so a slow reply delays
    other callers only as far as their own timeouts.
    """

    def __init__(self) -> None:
        import itertools
        import threading

        self._start_lock = threading.Lock()
        self._loop = None
        self._thread = None
        self._ws = None
        self._send_lock = None
        self._ids = itertools.count(1)

    # -- the loop thread ---------------------------------------------------

    def _ensure_loop(self):
        """The channel's event loop, started on first use."""
        import asyncio
        import threading

        with self._start_lock:
            if self._loop is not None and self._thread.is_alive():
                return self._loop
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=loop.run_forever, name="cdp", daemon=True)
            thread.start()
            self._loop, self._thread = loop, thread
            self._ws, self._send_lock = None, None
            return loop

    # -- the connection ----------------------------------------------------

    async def _connection(self):
        """The open socket, reconnecting if the browser dropped it."""
        import websockets

        if self._ws is not None:
            # Chromium drops the socket when the page navigates away, and a
            # send on a dead one raises rather than returning.
            if getattr(self._ws, "close_code", None) is not None:
                self._ws = None
            else:
                try:
                    await self._ws.ping()
                    return self._ws
                except Exception:
                    self._ws = None

        url = _cdp_page_socket()
        if not url:
            return None
        self._ws = await websockets.connect(url)
        return self._ws

    async def _request(self, method: str, params: dict, timeout: float):
        import asyncio
        import json

        if self._send_lock is None:
            self._send_lock = asyncio.Lock()

        async with self._send_lock:
            ws = await self._connection()
            if ws is None:
                return None
            request_id = next(self._ids)
            await ws.send(json.dumps(
                {"id": request_id, "method": method, "params": params}))
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return None
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                message = json.loads(raw)
                # CDP interleaves unsolicited events with replies; only a
                # matching id answers this call.
                if message.get("id") == request_id:
                    return message

    def send(self, method: str, params: dict, timeout: float = 2.0):
        """Run one command, or return None."""
        import asyncio

        try:
            import websockets  # noqa: F401
        except ImportError:
            log.debug("websockets not installed; CDP unavailable")
            return None

        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(
            self._request(method, params, timeout), loop)
        try:
            return future.result(timeout=timeout + 1.5)
        except Exception:
            # Cancelling matters: it releases the lock and drops the socket
            # rather than leaving the next caller behind a dead request.
            future.cancel()
            self._drop()
            log.debug("CDP %s failed", method, exc_info=True)
            return None

    def _drop(self) -> None:
        """Forget the socket so the next call reconnects."""
        import asyncio

        ws, self._ws = self._ws, None
        if ws is not None and self._loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(ws.close(), self._loop)
            except Exception:
                pass

    def close(self) -> None:
        """Stop the loop and its thread. Safe to call more than once."""
        with self._start_lock:
            loop, thread = self._loop, self._thread
            self._loop, self._thread, self._ws = None, None, None
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(loop.stop)
        except Exception:
            pass
        if thread is not None:
            thread.join(timeout=2.0)


_CHANNEL = _CdpChannel()


def _cdp_send(method: str, params: dict, *, timeout: float = 2.0):
    """Send one CDP command and return its result, or None."""
    return _CHANNEL.send(method, params, timeout=timeout)


def close_cdp() -> None:
    """Shut the shared CDP connection down, for use when an app exits."""
    _CHANNEL.close()


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
