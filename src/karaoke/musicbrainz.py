"""A polite MusicBrainz web-service client: one request a second, retries.

MusicBrainz allows one request per second per IP and asks every client to
identify itself with a contact in the User-Agent. It answers 503 when it sheds
load -- often, at night -- so a 503 or 429 is retried with backoff, and only a
request that still fails is reported, as None, so callers can tell "MusicBrainz
was unreachable" from "MusicBrainz has nothing".

The spacing is shared by every process on the machine through a small lock
file holding the time of the last request: the post-processing workers
resolve new artists while ``make artists`` may be running, and two clients
each keeping their own one-per-second pace together get mostly 503s.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

from .logger import log

BASE_URL = "https://musicbrainz.org/ws/2"
USER_AGENT = "KaraokeArtistIdentity/1.0 (https://github.com/violetina/karaoke)"
#: Seconds between requests; a little over the one-per-second limit.
SPACING_S = 1.05
#: Waits before retrying when MusicBrainz sheds load (503) or rate-limits (429).
RETRY_WAITS_S = (2.0, 5.0, 10.0)
TIMEOUT_S = 10.0



def _slot_file() -> Path:
    from .config import settings

    return settings.data_dir / "musicbrainz.slot"


def wait_turn(slot: Optional[Path] = None, *, clock=time.time, sleep=time.sleep) -> None:
    """Block until ``SPACING_S`` after the last request from any process.

    The file is locked while it is read and rewritten, so two processes
    cannot both take the same slot.
    """
    slot = slot or _slot_file()
    slot.parent.mkdir(parents=True, exist_ok=True)
    with open(slot, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.seek(0)
            try:
                last = float(f.read().strip() or 0)
            except ValueError:
                last = 0.0
            gap = clock() - last
            if 0 <= gap < SPACING_S:
                sleep(SPACING_S - gap)
            f.seek(0)
            f.truncate()
            f.write(repr(clock()))
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def get(path: str, params: dict[str, str], *, sleep=time.sleep) -> Optional[dict[str, Any]]:
    """GET ``/ws/2/<path>`` as JSON, or None if MusicBrainz could not answer."""
    query = urllib.parse.urlencode({**params, "fmt": "json"})
    req = urllib.request.Request(f"{BASE_URL}/{path}?{query}", headers={
        "User-Agent": USER_AGENT, "Accept": "application/json"})
    for wait in (*RETRY_WAITS_S, None):
        wait_turn(sleep=sleep)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 503) and wait is not None:
                sleep(wait)
                continue
            if exc.code == 404:
                return {}
            log.warning("MusicBrainz %s failed: %s", path, exc)
            return None
        except Exception as exc:
            log.warning("MusicBrainz %s failed: %s", path, exc)
            return None
    return None


def quote(text: str) -> str:
    """A Lucene phrase for a search query: quoted, with quotes escaped."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def search_recordings(title: str, artist: str, limit: int = 10) -> Optional[list[dict[str, Any]]]:
    """Recordings matching a title by an artist, best first, or None on failure."""
    data = get("recording/", {"query": f"recording:{quote(title)} AND artist:{quote(artist)}",
                              "limit": str(limit)})
    return None if data is None else data.get("recordings") or []


def search_artists(name: str, limit: int = 10) -> Optional[list[dict[str, Any]]]:
    """Artists matching a name (or alias), best first, or None on failure."""
    data = get("artist/", {"query": f"artist:{quote(name)} OR alias:{quote(name)}",
                           "limit": str(limit)})
    return None if data is None else data.get("artists") or []


def artist(mbid: str) -> Optional[dict[str, Any]]:
    """One artist with aliases, genres, tags and links, or None on failure."""
    return get(f"artist/{mbid}", {"inc": "aliases+genres+tags+url-rels"})
