"""Weighted search across the library.

One box, ranked results. A query is matched against several fields and the
matches are weighted, so typing a song name finds the song rather than every
track by an artist whose name happens to contain the same letters.

Field priority, highest first:

1. **title** — what you almost always mean
2. **album** — narrower than an artist, so a hit is more deliberate
3. **artist** — broad; matches everything they recorded
4. **lyrics** — the fallback for "I only remember a line of it"

Position counts as well as presence: an exact title beats a title that starts
with the query, which beats one that merely contains it. Without that,
searching "river" ranks *Riverside* and *The River* identically, and the one
you meant is wherever the database happened to put it.

Note that album is sparsely populated in practice — most rows arrive from
player metadata that omits it — so that weight rarely fires today. It costs
nothing to keep, and it starts working the moment albums are filled in.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Optional

# Relative worth of a hit in each field.
W_TITLE = 1.0
W_ALBUM = 0.6
W_ARTIST = 0.4
W_LYRICS = 0.2

# How a hit within a field scores, by how deliberate the match looks.
EXACT = 1.0
PREFIX = 0.75     # starts with the query, at a word boundary
WORD = 0.6        # the query appears as a whole word
PARTIAL = 0.45    # starts with the query mid-word ("Riverside" for "river")
CONTAINS = 0.35   # appears somewhere, inside a word

# Lyrics are long, so a substring hit there says less than one in a title.
# Only whole-word matches count, or "love" matches "glove" in every song.
_WORD_CACHE_LIMIT = 400


@dataclass(frozen=True)
class Hit:
    """One search result and why it matched."""

    track_id: int
    artist: str
    title: str
    album: str
    score: float
    fields: tuple[str, ...]      # which fields contributed, best first

    @property
    def label(self) -> str:
        return f"{self.artist} - {self.title}"


def _normalise(text: str) -> str:
    return " ".join((text or "").casefold().split())


def field_score(value: str, query: str) -> float:
    """How well one field matches, 0..1."""
    haystack = _normalise(value)
    needle = _normalise(query)
    if not haystack or not needle:
        return 0.0
    if haystack == needle:
        return EXACT
    # Word boundaries are checked before a bare prefix, or "Riverside" scores
    # the same as "River Deep" for "river" -- both merely start with it, but
    # only one of them is actually the word you typed.
    if re.match(rf"{re.escape(needle)}\b", haystack):
        return PREFIX
    if re.search(rf"\b{re.escape(needle)}\b", haystack):
        return WORD
    if haystack.startswith(needle):
        return PARTIAL
    if needle in haystack:
        return CONTAINS
    return 0.0


def lyrics_score(text: str, query: str) -> float:
    """Whether the query appears in the lyrics as a whole word.

    Substring matching is deliberately not used here: in a body of text
    "love" would match "glove" and "clover", and the lyric field would then
    match nearly everything at its low weight rather than nothing.
    """
    haystack = _normalise(text)
    needle = _normalise(query)
    if not haystack or not needle:
        return 0.0
    return WORD if re.search(rf"\b{re.escape(needle)}\b", haystack) else 0.0


def score_row(row, query: str, *, search_lyrics: bool = True) -> tuple[float, tuple[str, ...]]:
    """Total weighted score for one track, and the fields that contributed."""
    parts: list[tuple[float, str]] = []

    title = field_score(row["title"] or "", query) * W_TITLE
    if title:
        parts.append((title, "title"))
    album = field_score(row["album"] or "", query) * W_ALBUM
    if album:
        parts.append((album, "album"))
    artist = field_score(row["artist"] or "", query) * W_ARTIST
    if artist:
        parts.append((artist, "artist"))
    if search_lyrics:
        words = lyrics_score(row["words"] if "words" in row.keys() else "", query)
        if words:
            parts.append((words * W_LYRICS, "lyrics"))

    if not parts:
        return (0.0, ())
    parts.sort(reverse=True)
    # Summed, not maxed: a track matching on both title and artist is a better
    # answer than one matching on either alone, and should outrank it.
    return (sum(score for score, _ in parts), tuple(name for _, name in parts))


def search(query: str, conn: sqlite3.Connection, *, limit: int = 25,
           search_lyrics: bool = True) -> list[Hit]:
    """Ranked matches for a query, best first."""
    if not _normalise(query):
        return []

    rows = conn.execute(
        """
        SELECT t.track_id, t.artist, t.title, COALESCE(t.album, '') AS album,
               COALESCE(l.plain_lyrics, l.synced_lyrics, '') AS words
        FROM tracks t
        LEFT JOIN lyrics l ON l.track_id = t.track_id AND l.kind = 'approved'
        """
    ).fetchall()

    hits: list[Hit] = []
    for row in rows:
        score, fields = score_row(row, query, search_lyrics=search_lyrics)
        if score <= 0:
            continue
        hits.append(Hit(track_id=int(row["track_id"]), artist=row["artist"] or "",
                        title=row["title"] or "", album=row["album"] or "",
                        score=score, fields=fields))
    hits.sort(key=lambda h: (-h.score, h.artist.casefold(), h.title.casefold()))
    return hits[:limit]


def playable_url(track_id: int, conn: sqlite3.Connection) -> Optional[str]:
    """Somewhere to play a track from, preferring a browser-openable source."""
    row = conn.execute(
        "SELECT url FROM sources WHERE track_id = ?"
        " ORDER BY CASE WHEN url LIKE '%youtu%' THEN 0"
        "               WHEN url LIKE 'http%' THEN 1 ELSE 2 END, source_id"
        " LIMIT 1", (track_id,)).fetchone()
    return row["url"] if row and row["url"] else None
