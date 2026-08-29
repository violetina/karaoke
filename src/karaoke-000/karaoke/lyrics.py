"""LRCLIB synced-lyrics client and LRC parsing.

LRCLIB (https://lrclib.net) serves community synced lyrics for free with no API
key. `GET /api/get` matches on artist+track (+album+duration); on a miss we fall
back to `GET /api/search`.

LRC format lines look like: ``[mm:ss.xx] the lyric line``. A line may carry
several timestamps; each becomes its own (time, text) entry.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

from .config import settings

_TS = re.compile(r"\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]")


@dataclass
class Lyrics:
    plain: str = ""
    synced_raw: str = ""          # original LRC text (cached verbatim)
    source: str = "none"          # lrclib | whisper | none
    lines: list[tuple[float, str]] = field(default_factory=list)  # (seconds, text)

    @property
    def has_synced(self) -> bool:
        return bool(self.lines)


def parse_lrc(lrc: str) -> list[tuple[float, str]]:
    """Parse LRC text into a time-sorted list of (seconds, text).

    Lines with multiple timestamps are expanded. Metadata-only lines (e.g.
    ``[ar: Artist]``) and empty-text stamps are skipped.
    """
    out: list[tuple[float, str]] = []
    for raw in lrc.splitlines():
        stamps = list(_TS.finditer(raw))
        if not stamps:
            continue
        text = _TS.sub("", raw).strip()
        if not text:
            continue
        for m in stamps:
            mm = int(m.group(1))
            ss = int(m.group(2))
            frac = m.group(3) or "0"
            # normalize fractional part to milliseconds
            ms = int(frac.ljust(3, "0")[:3])
            out.append((mm * 60 + ss + ms / 1000.0, text))
    out.sort(key=lambda t: t[0])
    return out


def _to_lyrics(payload: dict) -> Lyrics:
    synced = payload.get("syncedLyrics") or ""
    plain = payload.get("plainLyrics") or ""
    lines = parse_lrc(synced) if synced else []
    return Lyrics(
        plain=plain,
        synced_raw=synced,
        source="lrclib" if (synced or plain) else "none",
        lines=lines,
    )


def fetch_lrclib(
    artist: str,
    title: str,
    album: Optional[str] = None,
    duration: Optional[float] = None,
    *,
    timeout: float = 10.0,
    session: Optional[Any] = None,
) -> Lyrics:
    """Fetch lyrics for a track. Tries /api/get, then /api/search."""
    http: Any = session or requests
    base = settings.lrclib_base.rstrip("/")
    params: dict[str, str] = {"artist_name": artist, "track_name": title}
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = str(int(round(duration)))

    try:
        r = http.get(f"{base}/api/get", params=params, timeout=timeout)
        if r.status_code == 200:
            return _to_lyrics(r.json())
    except requests.RequestException:
        pass

    # Fallback: search and take the best synced hit, else the first hit.
    try:
        r = http.get(
            f"{base}/api/search",
            params={"artist_name": artist, "track_name": title},
            timeout=timeout,
        )
        if r.status_code == 200:
            results = r.json() or []
            if results:
                best = next((x for x in results if x.get("syncedLyrics")), results[0])
                return _to_lyrics(best)
    except requests.RequestException:
        pass

    return Lyrics()


def _cli() -> int:  # pragma: no cover - thin manual smoke test
    import argparse

    ap = argparse.ArgumentParser(description="Fetch LRCLIB lyrics")
    ap.add_argument("--artist", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--album")
    ap.add_argument("--duration", type=float)
    a = ap.parse_args()
    ly = fetch_lrclib(a.artist, a.title, a.album, a.duration)
    print(f"source={ly.source} synced={ly.has_synced} lines={len(ly.lines)}")
    for t, text in ly.lines[:8]:
        print(f"  [{t:7.2f}] {text}")
    if not ly.lines and ly.plain:
        print(ly.plain[:300])
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
