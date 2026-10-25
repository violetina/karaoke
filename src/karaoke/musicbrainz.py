"""A polite MusicBrainz web-service client: one request a second, retries.

MusicBrainz allows one request per second per IP and asks every client to
identify itself with a contact in the User-Agent. It answers 503 when it sheds
load -- often, at night -- so a 503 or 429 is retried with backoff, and only a
request that still fails is reported, as None, so callers can tell "MusicBrainz
was unreachable" from "MusicBrainz has nothing".

The spacing is per process. Two processes querying at once (a backfill and a
probe) together exceed the limit and mostly get 503s, which the retries
absorb at the cost of speed.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from .logger import log

BASE_URL = "https://musicbrainz.org/ws/2"
USER_AGENT = "KaraokeArtistIdentity/1.0 (https://github.com/violetina/karaoke)"
#: Seconds between requests; a little over the one-per-second limit.
SPACING_S = 1.05
#: Waits before retrying when MusicBrainz sheds load (503) or rate-limits (429).
RETRY_WAITS_S = (2.0, 5.0, 10.0)
TIMEOUT_S = 10.0

_last_request = 0.0


def get(path: str, params: dict[str, str], *, sleep=time.sleep) -> Optional[dict[str, Any]]:
    """GET ``/ws/2/<path>`` as JSON, or None if MusicBrainz could not answer."""
    global _last_request
    query = urllib.parse.urlencode({**params, "fmt": "json"})
    req = urllib.request.Request(f"{BASE_URL}/{path}?{query}", headers={
        "User-Agent": USER_AGENT, "Accept": "application/json"})
    for wait in (*RETRY_WAITS_S, None):
        gap = time.time() - _last_request
        if gap < SPACING_S:
            sleep(SPACING_S - gap)
        _last_request = time.time()
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
