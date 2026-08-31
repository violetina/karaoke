"""Cluster-independent local cache + play/discovery stats (SQLite).

The OpenSearch index on the kind cluster is the rich search/index store, but it
is only available when the cluster is running. This module adds a small, always-
available SQLite database (``~/.local/share/karaoke/karaoke.db`` by default) that:

- caches lyrics per (artist, title) so a known song is served offline WITHOUT the
  kind cluster and WITHOUT a fresh LRCLIB request (checked before going online);
- records every play/identification event so ``karaoke-stats`` can report play
  counts, top artists, and radio-discovery stats.

Design notes:

- Pure standard library (``sqlite3``); no cluster, no extra deps.
- Keys are normalized (casefolded, stripped) so "R.E.M." and "r.e.m." collide.
- All writes are best-effort: cache/stats failures must never break playback.
- This is a lyrics/known-song cache, not an audio fingerprint. True offline audio
  identification (skipping songrec/Shazam) would need AcoustID/chromaprint; see
  the module ``README``/docs. What we cache here is the *result* of a recognition
  (artist/title -> lyrics), which is what makes a repeat play offline-capable.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import settings
from .lyrics import Lyrics, parse_lrc

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lyrics_cache (
    key           TEXT PRIMARY KEY,   -- normalized "artist\\ntitle"
    artist        TEXT NOT NULL,
    title         TEXT NOT NULL,
    album         TEXT DEFAULT '',
    duration      REAL,
    lyrics_source TEXT DEFAULT 'none', -- lrclib | whisper | none
    has_synced    INTEGER DEFAULT 0,
    plain_lyrics  TEXT DEFAULT '',
    synced_lyrics TEXT DEFAULT '',
    updated_at    REAL NOT NULL
);

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
    """Normalized cache key for a track (casefolded, whitespace-stripped)."""
    return f"{(artist or '').strip().casefold()}\n{(title or '').strip().casefold()}"


def connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """Open (and lazily initialize) the local SQLite database."""
    path = Path(db_path or settings.local_db)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


# ---------------------------------------------------------------------------
# Lyrics cache
# ---------------------------------------------------------------------------

def get_cached_lyrics(
    artist: str, title: str, *, conn: Optional[sqlite3.Connection] = None
) -> Optional[Lyrics]:
    """Return locally cached lyrics for a track, or None on a miss.

    Only returns a hit when some lyrics text was previously stored (synced or
    plain); rows that recorded a definitive "no lyrics" result return None so the
    caller can retry online later.
    """
    own = conn is None
    c = conn or connect()
    try:
        row = c.execute(
            "SELECT * FROM lyrics_cache WHERE key = ?", (_key(artist, title),)
        ).fetchone()
    finally:
        if own:
            c.close()
    if not row:
        return None
    synced = row["synced_lyrics"] or ""
    plain = row["plain_lyrics"] or ""
    if not synced and not plain:
        return None
    return Lyrics(
        plain=plain,
        synced_raw=synced,
        source=row["lyrics_source"] or "lrclib",
        lines=parse_lrc(synced) if synced else [],
    )


def put_cached_lyrics(
    artist: str,
    title: str,
    lyrics: Lyrics,
    *,
    album: str = "",
    duration: Optional[float] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> None:
    """Upsert lyrics for a track into the local cache (best-effort)."""
    if not (lyrics.synced_raw or lyrics.plain):
        return
    own = conn is None
    c = conn or connect()
    try:
        c.execute(
            """
            INSERT INTO lyrics_cache
                (key, artist, title, album, duration, lyrics_source,
                 has_synced, plain_lyrics, synced_lyrics, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                album=excluded.album,
                duration=excluded.duration,
                lyrics_source=excluded.lyrics_source,
                has_synced=excluded.has_synced,
                plain_lyrics=excluded.plain_lyrics,
                synced_lyrics=excluded.synced_lyrics,
                updated_at=excluded.updated_at
            """,
            (
                _key(artist, title), artist, title, album, duration,
                lyrics.source, int(lyrics.has_synced), lyrics.plain,
                lyrics.synced_raw, time.time(),
            ),
        )
        c.commit()
    finally:
        if own:
            c.close()


# ---------------------------------------------------------------------------
# Play / discovery stats
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
