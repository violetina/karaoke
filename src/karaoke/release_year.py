"""Release year per track, looked up once per album.

A MusicBrainz request costs a second, so the year is resolved per *album*
(one release-group search per distinct artist + album), and every track of
that album gets it. Tracks without a usable album fall back to a year written
in the title.

What counts as the year, best first (``SOURCE_RANK``):

``album``           MusicBrainz ``first-release-date`` of the album's release-group
``title``           a bare ``(2004)`` in the title
``title-remaster``  ``(2021 Remaster)``, ``Remix 2009``: the *reissue* year, an upper
                    bound on the original, used only when nothing better exists

Live albums and live-looking titles are skipped: a performance date is not the
song's release. Compilations are skipped too (a "Greatest Hits" date is not the
song's), by name before asking and by MusicBrainz secondary type afterwards.
"""
from __future__ import annotations

import difflib
import re
import time
import unicodedata
from datetime import date
from typing import Any, Optional

from .logger import log

SOURCE_RANK = {"album": 1, "title": 2, "title-remaster": 3}

# album_release.status values
MATCHED, NO_MATCH, LIVE, COMPILATION = "matched", "no-match", "live", "compilation"

_MIN_YEAR = 1900

DDL = """
CREATE TABLE IF NOT EXISTS album_release (
    artist_key   TEXT NOT NULL,
    album_key    TEXT NOT NULL,
    artist       TEXT NOT NULL DEFAULT '',
    album        TEXT NOT NULL DEFAULT '',
    year         INTEGER,
    status       TEXT NOT NULL,
    mb_rgid      TEXT NOT NULL DEFAULT '',
    mb_title     TEXT NOT NULL DEFAULT '',
    looked_up_at DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (artist_key, album_key)
);
CREATE TABLE IF NOT EXISTS track_year (
    track_id INTEGER PRIMARY KEY REFERENCES tracks(track_id) ON DELETE CASCADE,
    year     INTEGER NOT NULL,
    source   TEXT NOT NULL,
    noted_at DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_track_year_year ON track_year (year);
"""

_LIVE_RE = re.compile(
    r"\b(live|unplugged|in concert|concert|bootleg|sessions?)\b|@", re.I)
_COMPILATION_RE = re.compile(
    r"\b(greatest hits|best of|the very best|very best|anthology|collection|essentials?|"
    r"the hits|hits of|number ones|rarities|b-sides|singles|compilation|"
    r"complete (?:studio|recordings|works|albums)|guide to|magazine|\d+ greatest|"
    r"all[- ]time)\b|\bvolume\b|\bvol\.", re.I)
# Qualifiers that name a reissue of the same album, not a different one.
_ALBUM_NOISE_RE = re.compile(
    r"\s*[\(\[][^\)\]]*\b(deluxe|remaster(?:ed)?|expanded|edition|anniversary|bonus|"
    r"reissue|version|mono|stereo|disc|disk|cd)\b[^\)\]]*[\)\]]", re.I)
_DISC_RE = re.compile(r"\s*[-–:,]?\s*\b(?:cd|disc|disk)\s*\d+\b.*$", re.I)
_YEAR = r"(?:19|20)\d{2}"
_BRACKETED_RE = re.compile(r"[\(\[]([^\)\]]*)[\)\]]")
_DASH_SUFFIX_RE = re.compile(r"\s[-–]\s+([^\(\)\[\]]*)$")
_PLAIN_TITLE_YEAR_RE = re.compile(rf"[\(\[]\s*({_YEAR})\s*[\)\]]")
_REMASTER_YEAR_RE = re.compile(
    rf"(?:\b({_YEAR})\b[^\)\]]*\b(?:remaster(?:ed)?|remix(?:ed)?|mix|edit|version|"
    rf"re-?recorded|re-?issue)\b|"
    rf"\b(?:remaster(?:ed)?|remix(?:ed)?|mix|edit|version|re-?recorded|re-?issue)\b"
    rf"[^\)\]]*?\b({_YEAR})\b)", re.I)
_FULL_DATE_RE = re.compile(
    rf"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.? \d{{1,2}},? {_YEAR}\b|"
    rf"\b{_YEAR}[-/.]\d{{1,2}}[-/.]\d{{1,2}}\b|\b\d{{1,2}}[-/.]\d{{1,2}}[-/.]{_YEAR}\b", re.I)


def norm(text: str) -> str:
    """Lowercase, accent-free, punctuation-free key text."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _valid_year(value: Any) -> Optional[int]:
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    return year if _MIN_YEAR <= year <= date.today().year + 1 else None


def is_live(text: str) -> bool:
    return bool(_LIVE_RE.search(text or ""))


def is_compilation_name(album: str) -> bool:
    return bool(_COMPILATION_RE.search(album or ""))


def clean_album(album: str) -> str:
    """The album's name without reissue qualifiers and disc markers."""
    out = _ALBUM_NOISE_RE.sub("", album or "")
    out = _DISC_RE.sub("", out)
    return out.strip(" -–:,")


def title_year(title: str) -> Optional[tuple[int, str]]:
    """``(year, source)`` written in a track title, or None.

    A full date ("October 23, 2026") or a live-looking title says nothing
    about the release, so both give None.
    """
    title = title or ""
    if is_live(title) or _FULL_DATE_RE.search(title):
        return None
    # Only a qualifier counts ("(2021 Remaster)", "- 2017 Remix"), never a year that
    # is part of the song's own name ("Expo 2000 (Virus 13 Mix)").
    for segment in [*_BRACKETED_RE.findall(title), *_DASH_SUFFIX_RE.findall(title)]:
        m = _REMASTER_YEAR_RE.search(segment)
        if m:
            year = _valid_year(m.group(1) or m.group(2))
            if year:
                return year, "title-remaster"
    m = _PLAIN_TITLE_YEAR_RE.search(title)
    if m:
        year = _valid_year(m.group(1))
        return (year, "title") if year else None
    return None


def release_year_of(date_text: str) -> Optional[int]:
    return _valid_year((date_text or "")[:4])


def classify_album(album: str) -> Optional[str]:
    """``live``/``compilation`` when the name alone rules the album out, else None."""
    if is_live(album):
        return LIVE
    if is_compilation_name(album):
        return COMPILATION
    return None


def pick_release_group(groups: list[dict[str, Any]], album: str) -> tuple[Optional[dict], str]:
    """Choose the release-group for an album, as ``(group, status)``.

    ``status`` is ``matched``, ``compilation``, ``live`` or ``no-match``. Groups
    flagged Live/Compilation/Demo and the like are never the song's release;
    when the only good title match is one of those, the status says so.
    """
    want = norm(clean_album(album))
    best: Optional[tuple[tuple, dict]] = None
    ruled_out = None
    for g in groups:
        title = norm(g.get("title", ""))
        score = int(g.get("score") or 0)
        ratio = difflib.SequenceMatcher(None, want, title).ratio()
        if not (title == want or (score >= 95 and ratio >= 0.85)):
            continue
        secondary = {s.lower() for s in g.get("secondary-types") or []}
        if secondary & {"live", "compilation", "dj-mix", "demo", "mixtape/street"}:
            ruled_out = ruled_out or (LIVE if "live" in secondary else COMPILATION)
            continue
        if not release_year_of(g.get("first-release-date", "")):
            continue
        primary = (g.get("primary-type") or "").lower()
        key = (-score, 0 if primary == "album" else 1, g.get("first-release-date", "9999"))
        if best is None or key < best[0]:
            best = (key, g)
    if best:
        return best[1], MATCHED
    return None, ruled_out or NO_MATCH


def ensure_schema(conn: Any) -> None:
    conn.execute(DDL)
    conn.commit()


def set_year(conn: Any, track_id: int, year: int, source: str) -> bool:
    """Store a track's year unless a better-ranked source already holds one."""
    rank = SOURCE_RANK[source]
    row = conn.execute("SELECT source FROM track_year WHERE track_id = %s",
                       (track_id,)).fetchone()
    if row and SOURCE_RANK.get(row["source"], 99) < rank:
        return False
    if row and row["source"] == source:
        conn.execute("UPDATE track_year SET year = %s, noted_at = %s WHERE track_id = %s",
                     (year, time.time(), track_id))
        return True
    conn.execute(
        "INSERT INTO track_year (track_id, year, source, noted_at) VALUES (%s, %s, %s, %s)"
        " ON CONFLICT (track_id) DO UPDATE SET year = EXCLUDED.year,"
        " source = EXCLUDED.source, noted_at = EXCLUDED.noted_at",
        (track_id, year, source, time.time()))
    return True


def apply_title_years(conn: Any) -> dict[str, int]:
    """Write years found in titles for tracks that have no better source. No network."""
    counts = {"title": 0, "title-remaster": 0}
    rows = conn.execute(
        "SELECT t.track_id, t.title FROM tracks t"
        " WHERE t.title ~ '(19|20)[0-9]{2}'").fetchall()
    for r in rows:
        found = title_year(r["title"])
        if found and set_year(conn, r["track_id"], found[0], found[1]):
            counts[found[1]] += 1
    conn.commit()
    return counts


def lookup_album(musicbrainz: Any, album: str, artist: str,
                 arid: str = "") -> tuple[Optional[dict], str]:
    """One release-group search. ``(None, "unreachable")`` when MusicBrainz failed."""
    who = f"arid:{arid}" if arid else f"artist:{musicbrainz.quote(artist)}"
    data = musicbrainz.get("release-group/", {
        "query": f"releasegroup:{musicbrainz.quote(clean_album(album))} AND {who}",
        "limit": "10"})
    if data is None:
        return None, "unreachable"
    return pick_release_group(data.get("release-groups") or [], album)


def record_album(conn: Any, artist: str, album: str, group: Optional[dict], status: str) -> int:
    """Store an album's outcome and give its tracks the year. Returns tracks updated."""
    ak, bk = norm(artist), norm(album)
    year = release_year_of(group.get("first-release-date", "")) if group else None
    conn.execute(
        "INSERT INTO album_release (artist_key, album_key, artist, album, year, status,"
        " mb_rgid, mb_title, looked_up_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)"
        " ON CONFLICT (artist_key, album_key) DO UPDATE SET year = EXCLUDED.year,"
        " status = EXCLUDED.status, mb_rgid = EXCLUDED.mb_rgid, mb_title = EXCLUDED.mb_title,"
        " looked_up_at = EXCLUDED.looked_up_at",
        (ak, bk, artist, album, year, status,
         (group or {}).get("id", ""), (group or {}).get("title", ""), time.time()))
    updated = 0
    if year:
        for r in conn.execute("SELECT track_id, artist, album FROM tracks"
                              " WHERE lower(artist) = lower(%s) AND lower(album) = lower(%s)",
                              (artist, album)).fetchall():
            if norm(r["artist"]) == ak and norm(r["album"]) == bk:
                updated += set_year(conn, r["track_id"], year, "album")
    conn.commit()
    return updated


def pending_albums(conn: Any, limit: Optional[int] = None) -> list[dict[str, Any]]:
    """Distinct artist+album pairs not looked up yet, biggest albums first."""
    done = {(r["artist_key"], r["album_key"])
            for r in conn.execute("SELECT artist_key, album_key FROM album_release").fetchall()}
    rows = conn.execute("""
        SELECT lower(t.artist) AS a, lower(t.album) AS b, min(t.artist) AS artist,
               min(t.album) AS album, count(*) AS n,
               (array_agg(ta.mbid ORDER BY t.track_id) FILTER (WHERE ta.mbid IS NOT NULL))[1] AS arid
        FROM tracks t
        LEFT JOIN track_artists ta ON ta.track_id = t.track_id AND ta.position = 0
        WHERE coalesce(t.album, '') <> ''
        GROUP BY 1, 2
        ORDER BY n DESC, a, b""").fetchall()
    out = [dict(r) for r in rows if (norm(r["artist"]), norm(r["album"])) not in done]
    return out[:limit] if limit else out


def resolve_album(conn: Any, musicbrainz: Any, artist: str, album: str,
                  arid: str = "") -> tuple[str, int]:
    """Look an album up (or rule it out by name) and store the outcome.

    Returns ``(status, tracks updated)``; ``unreachable`` stores nothing so the
    album is tried again next run.
    """
    status = classify_album(album)
    group = None
    if status is None:
        group, status = lookup_album(musicbrainz, album, artist, arid)
        if status == "unreachable":
            return status, 0
    return status, record_album(conn, artist, album, group, status)


def resolve_track(track_id: int, conn: Any, musicbrainz: Any = None) -> str:
    """Chain step: the year for one track (title year now, album year if unknown)."""
    ensure_schema(conn)
    row = conn.execute("SELECT artist, title, album FROM tracks WHERE track_id = %s",
                       (track_id,)).fetchone()
    if not row:
        return "missing"
    found = title_year(row["title"])
    if found:
        set_year(conn, track_id, found[0], found[1])
        conn.commit()
    if not (row["album"] or "").strip():
        return "title" if found else "no-album"
    if conn.execute("SELECT 1 FROM album_release WHERE artist_key = %s AND album_key = %s",
                    (norm(row["artist"]), norm(row["album"]))).fetchone():
        return "known"
    if musicbrainz is None:
        from . import musicbrainz as musicbrainz
    arid_row = conn.execute("SELECT mbid FROM track_artists WHERE track_id = %s AND position = 0",
                            (track_id,)).fetchone()
    try:
        status, _ = resolve_album(conn, musicbrainz, row["artist"], row["album"],
                                  arid_row["mbid"] if arid_row else "")
    except Exception:
        log.warning("release year: lookup failed for track %s", track_id, exc_info=True)
        return "error"
    return status


def needs_year(track_id: int, conn: Any) -> bool:
    """Whether the chain should look this track's album up (bounded: one request per new album)."""
    try:
        ensure_schema(conn)
        row = conn.execute(
            "SELECT t.artist, t.album, (SELECT 1 FROM track_year y WHERE y.track_id = t.track_id) AS has"
            " FROM tracks t WHERE t.track_id = %s", (track_id,)).fetchone()
        if not row or not (row["album"] or "").strip():
            return False
        if conn.execute("SELECT 1 FROM album_release WHERE artist_key = %s AND album_key = %s",
                        (norm(row["artist"]), norm(row["album"]))).fetchone():
            return False
        return True
    except Exception:
        log.debug("could not check release year for track %s", track_id)
        return False
