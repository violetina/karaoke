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
import unicodedata
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

CREATE TABLE IF NOT EXISTS spotify_lookups (
    track_id   INTEGER PRIMARY KEY,
    -- NULL means Spotify was asked and had no match. That is a real result and
    -- must be remembered: without it, every track Spotify does not carry is
    -- re-searched on every play, and search is the rate-limited endpoint.
    uri        TEXT,
    checked_at REAL NOT NULL,
    attempts   INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(track_id) REFERENCES tracks(track_id)
);

CREATE TABLE IF NOT EXISTS track_sync_offsets (
    track_id    INTEGER PRIMARY KEY,
    offset_s    REAL NOT NULL,
    updated_at  REAL NOT NULL,
    -- Which clock the offset was tuned against: 'radio' (dead-reckoned from
    -- songrec, carries DEFAULT_LEAD_S) or 'player' (MPRIS position, no lead).
    -- An offset is only valid for its own clock; see offset_mode().
    mode        TEXT NOT NULL DEFAULT '',
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


# How many times a track may be looked up on Spotify before we stop asking. A
# transient failure deserves one retry; more than that and we are just spending
# quota on a song Spotify does not have.
SPOTIFY_LOOKUP_ATTEMPTS = 2


def ensure_spotify_lookup_table(conn: sqlite3.Connection) -> None:
    """Create the Spotify lookup cache in databases predating it."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS spotify_lookups (
            track_id   INTEGER PRIMARY KEY,
            uri        TEXT,
            checked_at REAL NOT NULL,
            attempts   INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()


def spotify_lookup_due(track_id: Optional[int], conn: sqlite3.Connection, *,
                       max_attempts: int = SPOTIFY_LOOKUP_ATTEMPTS) -> bool:
    """Whether this track may be looked up on Spotify.

    False once a URI is known (the answer is cached), and false once the track
    has been asked about ``max_attempts`` times without one — a miss is a
    result, not an invitation to keep spending search quota.
    """
    if track_id is None:
        return False
    row = conn.execute(
        "SELECT uri, attempts FROM spotify_lookups WHERE track_id = ?",
        (track_id,),
    ).fetchone()
    if row is None:
        return True
    if row["uri"]:
        return False
    return int(row["attempts"] or 0) < max_attempts


def record_spotify_lookup(track_id: int, uri: Optional[str],
                          conn: sqlite3.Connection) -> None:
    """Record the outcome of a Spotify lookup, hit or miss.

    Call this for a miss too. Never call it for a rate-limit error: a 429 says
    nothing about whether Spotify has the song, and recording it would cache a
    false negative that ``spotify_lookup_due`` would then honour forever.
    """
    conn.execute(
        """
        INSERT INTO spotify_lookups (track_id, uri, checked_at, attempts)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(track_id) DO UPDATE SET
            uri = excluded.uri,
            checked_at = excluded.checked_at,
            attempts = spotify_lookups.attempts + 1
        """,
        (track_id, uri or None, time.time()),
    )
    conn.commit()


def offset_mode(mode: str) -> str:
    """Collapse a detection mode to the clock its sync offset belongs to.

    Radio dead-reckons the playhead from songrec and adds ``DEFAULT_LEAD_S`` to
    cover the listening latency; every other mode reads an MPRIS position with
    no such lead. An offset tuned against one clock is wrong by roughly that
    lead on the other, so the two are stored and matched separately. Modes that
    share the MPRIS position (scan, spotify, listen) share an offset.
    """
    return "radio" if mode == "radio" else "player"


def ensure_sync_offset_columns(conn: sqlite3.Connection) -> None:
    """Add the ``mode`` column to databases created before it existed."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(track_sync_offsets)")}
    if "mode" not in cols:
        conn.execute("ALTER TABLE track_sync_offsets ADD COLUMN "
                     "mode TEXT NOT NULL DEFAULT ''")
        conn.commit()


def get_sync_offset(track_id: Optional[int], conn: sqlite3.Connection,
                    mode: Optional[str] = None) -> Optional[float]:
    """Return the saved lyric sync offset (seconds) for a track, or None.

    When ``mode`` is given, an offset saved against a different clock is
    ignored rather than misapplied — the caller then falls back to its default.
    Rows predating the ``mode`` column store ``''`` and are honoured for any
    mode: they cannot be attributed, and the alternative is silently discarding
    tuning the user did by hand.
    """
    if track_id is None:
        return None
    row = conn.execute(
        "SELECT offset_s, mode FROM track_sync_offsets WHERE track_id = ?",
        (track_id,),
    ).fetchone()
    if row is None:
        return None
    saved = row["mode"] or ""
    if mode is not None and saved and saved != offset_mode(mode):
        return None
    return float(row["offset_s"])


def set_sync_offset(track_id: int, offset_s: float, conn: sqlite3.Connection,
                    mode: str = "") -> None:
    """Persist (upsert) the per-track lyric sync offset in seconds."""
    conn.execute(
        """
        INSERT INTO track_sync_offsets (track_id, offset_s, updated_at, mode)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(track_id) DO UPDATE SET
            offset_s = excluded.offset_s,
            updated_at = excluded.updated_at,
            mode = excluded.mode
        """,
        (track_id, float(offset_s), time.time(),
         offset_mode(mode) if mode else ""),
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
    ensure_sync_offset_columns(conn)
    ensure_spotify_lookup_table(conn)
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


def _match_key(text: str) -> str:
    """Aggressively normalised form for comparing track/artist names.

    Lowercased, accents stripped, everything but letters and digits removed, so
    "(Sittin' on) The Dock of the Bay" and "(Sittin On) The Dock Of The Bay"
    collapse to the same key.
    """
    folded = unicodedata.normalize("NFKD", text or "")
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", folded.casefold())


def _artist_key(artist: str) -> str:
    """Match key for an artist, ignoring a leading "The".

    Sources disagree on it constantly — songrec stores "The Mothers of
    Invention" where a player reports "Mothers of Invention".
    """
    return _match_key(re.sub(r"^\s*the\s+", "", artist or "", flags=re.I))


def _title_keys(title: str) -> set[str]:
    """Match keys for a title, with and without trailing bracketed suffixes.

    Editions arrive in every bracket style — "[2020 Remaster]", "(Live)",
    "- Remastered 2011" — and only some are covered by clean_title's suffix
    list, so the bare stem is compared too.
    """
    from .lyrics import clean_title

    keys = {_match_key(title), _match_key(clean_title(title))}
    stem = re.sub(r"\s*[\(\[][^)\]]*[\)\]]\s*$", "", title or "").strip()
    while stem and stem != title:
        keys.add(_match_key(stem))
        title, stem = stem, re.sub(r"\s*[\(\[][^)\]]*[\)\]]\s*$", "", stem).strip()
    return {k for k in keys if k}


def find_track_id_relaxed(artist: str, title: str,
                          conn: sqlite3.Connection) -> Optional[int]:
    """Find a track when the exact spelling does not match.

    Sources name the same song differently: songrec stores the full credit
    ("James Brown & The Famous Flames") and a decorated title ("[2020
    Remaster]") where a browser reports "James Brown" and a plainer title. An
    exact lookup misses those, so a track the radio already cached looks absent
    to the TUI and its lyrics never appear.

    Tries the exact match, then the cleaned title, then a punctuation- and
    case-insensitive comparison in which the artist need only be a prefix of
    the stored credit, or the reverse.
    """
    exact = find_track_id(artist, title, conn)
    if exact is not None:
        return exact

    from .lyrics import clean_title

    cleaned = clean_title(title)
    if cleaned and cleaned != title:
        got = find_track_id(artist, cleaned, conn)
        if got is not None:
            return got

    want_titles = _title_keys(title) | _title_keys(cleaned or title)
    want_artist = _artist_key(artist)
    if not want_titles:
        return None

    # Prefilter in SQL on a word of the title so this stays cheap on a large
    # library rather than normalising every row on every 1.5s poll.
    words = re.sub(r"[^a-z0-9]+", " ", (cleaned or title).casefold()).split()
    lead = max(words, key=len) if words else ""
    rows = conn.execute(
        "SELECT track_id, artist, title FROM tracks WHERE lower(title) LIKE ?"
        " ORDER BY track_id DESC LIMIT 200",
        (f"%{lead}%" if lead else "%",),
    ).fetchall()

    for row in rows:
        if not (_title_keys(row["title"]) & want_titles):
            continue
        got_artist = _artist_key(row["artist"])
        if not want_artist or not got_artist:
            return int(row["track_id"])
        # "James Brown" should match "James Brown & The Famous Flames".
        if got_artist.startswith(want_artist) or want_artist.startswith(got_artist):
            return int(row["track_id"])
    return None


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
