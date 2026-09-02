"""Launching song URLs in the desktop player/browser.

Split out of :mod:`karaoke.browse` so that callers which only need to open a
URL — the host-side control API, for example — do not pull in Textual and the
whole TUI stack just to spawn ``xdg-open``.
"""
from __future__ import annotations

import subprocess

from .logger import OPEN_STDERR_LOG, OPEN_STDOUT_LOG, log


def open_song_url(url: str, kind: str | None) -> int | None:
    """Open a song URL and return the spawned process id when applicable.

    YouTube/browser URLs are opened asynchronously so the TUI remains responsive.
    stdout/stderr are captured to log files so xdg-open failures are debuggable.
    """
    if kind == "spotify":
        log.debug("Executing: playerctl open %s", url)
        completed = subprocess.run(
            ["playerctl", "open", url],
            check=True,
            capture_output=True,
            text=True,
        )
        if completed.stdout:
            log.debug("playerctl stdout: %s", completed.stdout.strip())
        if completed.stderr:
            log.debug("playerctl stderr: %s", completed.stderr.strip())
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
