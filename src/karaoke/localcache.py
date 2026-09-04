"""Cluster-independent local cache + play/discovery stats (SQLite).

The OpenSearch index on the kind cluster is the rich search/index store, but it
is only available when the cluster is running. This module adds a small, always-
available SQLite database (``~/.local/share/karaoke/karaoke.db`` by default) that:

- Caches lyrics and track metadata.
- Records every play/identification event so ``karaoke-stats`` can report play
  counts, top artists, and radio-discovery stats.
"""
from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import settings
from .logger import log
from .lyrics import Lyrics, clean_artist, clean_page_title, parse_lrc

_NEW_SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    track_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    artist      TEXT NOT NULL,
    title       TEXT NOT NULL,
    album       TEXT,
    duration    REAL,
    UNIQUE(artist, title)
);

CREATE TABLE IF NOT EXISTS sources (
    source_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id    INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    url         TEXT UNIQUE,
    player_name TEXT,
    FOREIGN KEY(track_id) REFERENCES tracks(track_id)
);

CREATE TABLE IF NOT EXISTS lyrics (
    lyric_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    track_id        INTEGER NOT NULL,
    kind            TEXT NOT NULL DEFAULT 'approved', -- approved | staged | rejected
    source          TEXT, -- lrclib | youtube_caption | whisper | user_submitted
    synced_lyrics   TEXT,
    plain_lyrics    TEXT,
    FOREIGN KEY(track_id) REFERENCES tracks(track_id)
);

CREATE TABLE IF NOT EXISTS lyric_gaps (
    gap_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    artist          TEXT NOT NULL,
    title           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending', -- pending | processed | failed
    created_at      REAL NOT NULL,
    processed_at    REAL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    UNIQUE(artist, title)
);

CREATE TABLE IF NOT EXISTS track_sync_offsets (
    track_id    INTEGER PRIMARY KEY,
    offset_s    REAL NOT NULL,
    updated_at  REAL NOT NULL,
    FOREIGN KEY(track_id) REFERENCES tracks(track_id)
);
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS play_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL NOT NULL,
    mode       TEXT NOT NULL,          -- radio | spotify | listen | output | file | query | print
    artist     TEXT DEFAULT '',
    title      TEXT DEFAULT '',
    event      TEXT NOT NULL,          -- play | discover | relock | cache_hit | cache_miss | no_lyrics
    source     TEXT DEFAULT '',        -- local | opensearch | lrclib | whisper | none
    has_synced INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_play_events_ts ON play_events (ts);
CREATE INDEX IF NOT EXISTS idx_play_events_track ON play_events (artist, title);
"""


def _key(artist: str, title: str) -> str:
    """Stable case-insensitive artist/title cache key for staging metadata."""
    return f"{artist.strip().casefold()}\0{title.strip().casefold()}"


def ensure_gap_columns(conn: sqlite3.Connection) -> None:
    """Add the gap diagnostics columns to an existing DB (idempotent).

    ``attempts`` and ``last_error`` let the backfill runner distinguish a
    transient throttle from a real miss, and make ``failed`` rows retryable
    instead of terminal.
    """
    have = {r["name"] for r in conn.execute("PRAGMA table_info(lyric_gaps)")}
    if "attempts" not in have:
        conn.execute("ALTER TABLE lyric_gaps ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
    if "last_error" not in have:
        conn.execute("ALTER TABLE lyric_gaps ADD COLUMN last_error TEXT")
    conn.commit()


def normalize_gap_metadata(artist: str, title: str) -> Optional[tuple[str, str]]:
    """Normalize player metadata for the gap queue, or None if unusable.

    Player/YouTube metadata arrives decorated ("Artist - Topic",
    "Song | YouTube Music", "(Official Video)") or degenerate (empty artist,
    a whole-album page title). Such rows can never resolve against LRCLIB, so
    they are rejected here rather than queued and retried forever.
    """
    # Checked against the raw title: clean_page_title strips "(Full Album)" as a
    # decoration, so the reject has to run before it.
    if re.search(r"\b(full\s+album|full\s+set|album\s+completo|mixtape)\b",
                 title or "", re.I):
        return None
    a = clean_artist(clean_page_title(artist or ""))
    t = clean_page_title(title or "")
    if not a or not t:
        return None
    # YouTube titles repeat the artist ("Red Hot Chili Peppers - Suck My Kiss"),
    # which LRCLIB's exact endpoint cannot match. Drop the redundant prefix.
    prefix = re.match(rf"^{re.escape(a)}\s*[-–—:]\s*(?P<rest>.+)$", t, re.I)
    if prefix:
        t = prefix.group("rest").strip()
    if not t:
        return None
    return a, t


def log_lyric_gap(artist: str, title: str, conn: sqlite3.Connection) -> None:
    """Log a song that is missing lyrics.

    Metadata is normalized first; unusable rows are dropped instead of queued.
    """
    cleaned = normalize_gap_metadata(artist, title)
    if cleaned is None:
        log.debug("lyric gap skipped (unusable metadata): %r - %r", artist, title)
        return
    conn.execute(
        "INSERT OR IGNORE INTO lyric_gaps (artist, title, created_at) VALUES (?, ?, ?)",
        (*cleaned, time.time())
    )
    conn.commit()


def get_sync_offset(track_id: Optional[int], conn: sqlite3.Connection) -> Optional[float]:
    """Return the saved lyric sync offset (seconds) for a track, or None."""
    if track_id is None:
        return None
    row = conn.execute(
        "SELECT offset_s FROM track_sync_offsets WHERE track_id = ?",
        (track_id,),
    ).fetchone()
    return float(row["offset_s"]) if row is not None else None


def set_sync_offset(track_id: int, offset_s: float, conn: sqlite3.Connection) -> None:
    """Persist (upsert) the per-track lyric sync offset in seconds."""
    conn.execute(
        """
        INSERT INTO track_sync_offsets (track_id, offset_s, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(track_id) DO UPDATE SET
            offset_s = excluded.offset_s,
            updated_at = excluded.updated_at
        """,
        (track_id, float(offset_s), time.time()),
    )
    conn.commit()


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open (and lazily initialize) the local SQLite database."""
    path = Path(db_path or settings.local_db)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_NEW_SCHEMA)
    conn.executescript(_SCHEMA)
    ensure_gap_columns(conn)
    return conn


def find_track_id(artist: str, title: str, conn: sqlite3.Connection) -> Optional[int]:
    """Find a track by artist and title, returning its ID.

    Player metadata can vary in case, so cache lookup is case-insensitive while
    preserving the originally-stored display spelling.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT track_id FROM tracks
        WHERE lower(artist) = lower(?) AND lower(title) = lower(?)
        ORDER BY track_id DESC
        LIMIT 1
        """,
        (artist, title),
    )
    row = cur.fetchone()
    return row["track_id"] if row else None


def extract_youtube_id(url: str) -> Optional[str]:
    """Extract the 11-character video ID from a YouTube or YouTube Music URL."""
    if not url:
        return None
    import re
    m = re.search(r"(?:v=|/v/|/embed/|youtu\.be/|/watch\?v=)([^&\s\?]+)", url)
    if m:
        val = m.group(1)
        if len(val) == 11:
            return val
    if len(url) == 11 and re.match(r"^[a-zA-Z0-9_-]{11}$", url):
        return url
    return None


def find_track_by_url(url: str, conn: sqlite3.Connection) -> Optional[tuple[int, str, str]]:
    """Find a track by source URL, returning (track_id, artist, title)."""
    cur = conn.cursor()
    # Try exact match first
    cur.execute(
        """
        SELECT t.track_id, t.artist, t.title
        FROM tracks t JOIN sources s ON t.track_id = s.track_id
        WHERE s.url = ?
        """,
        (url,)
    )
    row = cur.fetchone()
    if row:
        return row["track_id"], row["artist"], row["title"]

    # Fallback to matching by YouTube video ID
    vid = extract_youtube_id(url)
    if vid:
        cur.execute(
            """
            SELECT t.track_id, t.artist, t.title
            FROM tracks t JOIN sources s ON t.track_id = s.track_id
            WHERE s.url LIKE ? AND s.kind IN ('youtube', 'youtube_music')
            LIMIT 1
            """,
            (f"%{vid}%",)
        )
        row = cur.fetchone()
        if row:
            return row["track_id"], row["artist"], row["title"]

    return None


def get_lyrics_by_track_id(track_id: int, conn: sqlite3.Connection) -> Optional[Lyrics]:
    """Get approved lyrics for a given track ID."""
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM lyrics WHERE track_id = ? AND kind = 'approved'",
        (track_id,)
    )
    row = cur.fetchone()
    if not row:
        return None
    
    synced = row["synced_lyrics"] or ""
    plain = row["plain_lyrics"] or ""
    if not synced and not plain:
        return None
        
    return Lyrics(
        plain=plain,
        synced_raw=synced,
        source=row["source"] or "lrclib",
        lines=parse_lrc(synced) if synced else [],
    )

def get_cached_lyrics(
    artist: str,
    title: str,
    *,
    conn: Optional[sqlite3.Connection] = None,
) -> Optional[Lyrics]:
    """Return approved cached lyrics for artist/title, or None on miss.

    Compatibility API kept for older code/tests while the database now stores
    tracks and lyrics in separate tables.
    """
    own = conn is None
    c = conn or connect()
    try:
        track_id = find_track_id(artist, title, c)
        if not track_id:
            return None
        return get_lyrics_by_track_id(track_id, c)
    finally:
        if own:
            c.close()


def put_cached_lyrics(
    artist: str,
    title: str,
    lyrics: Lyrics,
    *,
    album: str = "",
    duration: Optional[float] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Upsert approved lyrics for artist/title.

    Empty lyrics are ignored. Existing approved lyrics for the track are replaced
    so a Whisper/synced result can upgrade an earlier plain LRCLIB/caption entry.
    """
    if not (lyrics.synced_raw or lyrics.plain):
        return
    own = conn is None
    c = conn or connect()
    try:
        cur = c.cursor()
        track_id = find_track_id(artist, title, c)
        if track_id is None:
            cur.execute(
                "INSERT INTO tracks (artist, title, album, duration) VALUES (?, ?, ?, ?)",
                (artist, title, album, duration),
            )
            track_id = cur.lastrowid
            if track_id is None:
                return
        else:
            cur.execute(
                """
                UPDATE tracks
                SET album = COALESCE(NULLIF(?, ''), album),
                    duration = COALESCE(?, duration)
                WHERE track_id = ?
                """,
                (album, duration, track_id),
            )

        cur.execute(
            "DELETE FROM lyrics WHERE track_id = ? AND kind = 'approved'",
            (track_id,),
        )
        cur.execute(
            """
            INSERT INTO lyrics (track_id, kind, source, synced_lyrics, plain_lyrics)
            VALUES (?, 'approved', ?, ?, ?)
            """,
            (track_id, lyrics.source, lyrics.synced_raw, lyrics.plain),
        )
        c.commit()
    finally:
        if own:
            c.close()


def delete_empty_approved_lyrics(conn: Optional[sqlite3.Connection] = None) -> int:
    """Delete placeholder approved lyrics rows that contain no synced or plain text."""
    own = conn is None
    c = conn or connect()
    try:
        cur = c.cursor()
        cur.execute(
            """
            DELETE FROM lyrics
            WHERE kind = 'approved'
              AND COALESCE(synced_lyrics, '') = ''
              AND COALESCE(plain_lyrics, '') = ''
            """
        )
        deleted = cur.rowcount if cur.rowcount is not None else 0
        c.commit()
        return int(deleted)
    finally:
        if own:
            c.close()


def add_track_source(
    artist: str,
    title: str,
    *,
    album: str = "",
    duration: Optional[float] = None,
    url: Optional[str] = None,
    kind: str = "youtube",
    player_name: str = "",
    conn: Optional[sqlite3.Connection] = None,
) -> int:
    """Upsert a track and optional source URL without creating lyrics rows."""
    own = conn is None
    c = conn or connect()
    try:
        cur = c.cursor()
        # Prefer matching an existing track by its source URL / YouTube video ID
        # so callers that pass a URL (transcribe, backfill, write-through) update
        # the canonical track instead of spawning a duplicate when the parsed
        # artist/title differ (e.g. Whisper's filename-derived tags, remix edits).
        track_id: Optional[int] = None
        if url:
            found = find_track_by_url(url, c)
            if found:
                track_id = found[0]
        if track_id is None:
            track_id = find_track_id(artist, title, c)
        if track_id is None:
            cur.execute(
                "INSERT INTO tracks (artist, title, album, duration) VALUES (?, ?, ?, ?)",
                (artist, title, album, duration),
            )
            track_id = cur.lastrowid
            if track_id is None:
                raise RuntimeError("failed to insert track")
        else:
            cur.execute(
                """
                UPDATE tracks
                SET album = COALESCE(NULLIF(?, ''), album),
                    duration = COALESCE(?, duration)
                WHERE track_id = ?
                """,
                (album, duration, track_id),
            )

        if url:
            cur.execute(
                """
                INSERT INTO sources (track_id, url, kind, player_name)
                VALUES (?, ?, ?, NULLIF(?, ''))
                ON CONFLICT(url) DO UPDATE SET
                    track_id = excluded.track_id,
                    kind = excluded.kind,
                    player_name = COALESCE(excluded.player_name, sources.player_name)
                """,
                (track_id, url, kind, player_name),
            )
        c.commit()
        return int(track_id)
    finally:
        if own:
            c.close()


def add_track_and_lyrics(
    artist: str,
    title: str,
    lyrics: Lyrics,
    album: str = "",
    duration: Optional[float] = None,
    url: Optional[str] = None,
    kind: str = "youtube",
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Add or update a track, its optional source URL and approved lyrics."""
    if not (lyrics.synced_raw or lyrics.plain):
        add_track_source(
            artist,
            title,
            album=album,
            duration=duration,
            url=url,
            kind=kind,
            conn=conn,
        )
        return

    own = conn is None
    c = conn or connect()
    try:
        track_id = add_track_source(
            artist,
            title,
            album=album,
            duration=duration,
            url=url,
            kind=kind,
            conn=c,
        )
        cur = c.cursor()
        cur.execute(
            "DELETE FROM lyrics WHERE track_id = ? AND kind = 'approved'",
            (track_id,),
        )
        cur.execute(
            """
            INSERT INTO lyrics (track_id, kind, source, synced_lyrics, plain_lyrics)
            VALUES (?, 'approved', ?, ?, ?)
            """,
            (track_id, lyrics.source, lyrics.synced_raw, lyrics.plain),
        )
        c.commit()
    finally:
        if own:
            c.close()


# ---------------------------------------------------------------------------
# Play / discovery stats (Unchanged from previous implementation)
# ---------------------------------------------------------------------------

def log_event(
    mode: str,
    event: str,
    *,
    artist: str = "",
    title: str = "",
    source: str = "",
    has_synced: bool = False,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Record one play/discovery event (best-effort; never raises to caller)."""
    own = conn is None
    try:
        c = conn or connect()
    except Exception:
        return
    try:
        c.execute(
            "INSERT INTO play_events (ts, mode, artist, title, event, source, has_synced)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (time.time(), mode, artist, title, event, source, int(has_synced)),
        )
        c.commit()
    except Exception:
        pass
    finally:
        if own:
            try:
                c.close()
            except Exception:
                pass


@dataclass
class StatsSummary:
    """Aggregated play/discovery statistics."""

    total_events: int
    plays: int
    discoveries: int
    cache_hits: int
    cache_misses: int
    distinct_tracks: int
    distinct_artists: int
    top_tracks: list[tuple[str, str, int]]     # (artist, title, plays)
    top_artists: list[tuple[str, int]]         # (artist, plays)
    by_mode: list[tuple[str, int]]             # (mode, plays)

    @property
    def cache_hit_rate(self) -> float:
        """Fraction of lyrics lookups served from the local cache (0..1)."""
        total = self.cache_hits + self.cache_misses
        return (self.cache_hits / total) if total else 0.0


def summarize(
    *, limit: int = 10, since: Optional[float] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> StatsSummary:
    """Compute a StatsSummary over play_events (optionally since a UNIX time)."""
    own = conn is None
    c = conn or connect()
    where = "WHERE ts >= ?" if since is not None else ""
    args: tuple = (since,) if since is not None else ()
    try:
        def scalar(sql: str, extra: tuple = ()) -> int:
            row = c.execute(sql, args + extra).fetchone()
            return int(row[0]) if row and row[0] is not None else 0

        total = scalar(f"SELECT COUNT(*) FROM play_events {where}")
        plays = scalar(
            f"SELECT COUNT(*) FROM play_events {where} "
            f"{'AND' if where else 'WHERE'} event = 'play'"
        )
        discoveries = scalar(
            f"SELECT COUNT(*) FROM play_events {where} "
            f"{'AND' if where else 'WHERE'} event = 'discover'"
        )
        hits = scalar(
            f"SELECT COUNT(*) FROM play_events {where} "
            f"{'AND' if where else 'WHERE'} event = 'cache_hit'"
        )
        misses = scalar(
            f"SELECT COUNT(*) FROM play_events {where} "
            f"{'AND' if where else 'WHERE'} event = 'cache_miss'"
        )
        play_where = (
            f"{where} {'AND' if where else 'WHERE'} event IN ('play','discover') "
            "AND title != ''"
        )
        distinct_tracks = scalar(
            f"SELECT COUNT(*) FROM (SELECT 1 FROM play_events {play_where} "
            "GROUP BY artist, title)"
        )
        distinct_artists = scalar(
            f"SELECT COUNT(DISTINCT artist) FROM play_events {play_where} "
            "AND artist != ''"
        )
        top_tracks = [
            (r["artist"], r["title"], int(r["n"]))
            for r in c.execute(
                f"SELECT artist, title, COUNT(*) AS n FROM play_events {play_where} "
                "GROUP BY artist, title ORDER BY n DESC, title ASC LIMIT ?",
                args + (limit,),
            ).fetchall()
        ]
        top_artists = [
            (r["artist"], int(r["n"]))
            for r in c.execute(
                f"SELECT artist, COUNT(*) AS n FROM play_events {play_where} "
                "AND artist != '' GROUP BY artist ORDER BY n DESC, artist ASC LIMIT ?",
                args + (limit,),
            ).fetchall()
        ]
        by_mode = [
            (r["mode"], int(r["n"]))
            for r in c.execute(
                f"SELECT mode, COUNT(*) AS n FROM play_events {play_where} "
                "GROUP BY mode ORDER BY n DESC",
                args,
            ).fetchall()
        ]
    finally:
        if own:
            c.close()
    return StatsSummary(
        total_events=total, plays=plays, discoveries=discoveries,
        cache_hits=hits, cache_misses=misses,
        distinct_tracks=distinct_tracks, distinct_artists=distinct_artists,
        top_tracks=top_tracks, top_artists=top_artists, by_mode=by_mode,
    )
