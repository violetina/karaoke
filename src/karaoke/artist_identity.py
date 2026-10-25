"""Which real artists a track is by: MusicBrainz identities, linked and cached.

A track's artist is whatever string it arrived with, so one act appears under
several spellings ("Bonnie "Prince" Bill", "Bonnie Prince Billy") and a joint
credit ("Gorillaz, Fatoumata Diawara") is an act of its own. This resolves
each distinct artist string to MusicBrainz artists (MBIDs) and caches what
MusicBrainz knows about them. See docs/artist_metadata_plan.md.

**Identity comes through a recording, not a name.** Searching artists by name
filed "Phoenix" (the French band) under a classical artist with the same name.
So each artist string is resolved by finding one of *its own songs* on
MusicBrainz: the title must match, the credited names must match, and a length
matching the track's breaks ties. The recording's artist credit then names
every artist on the song, with join phrases ("feat.") that say who is a guest.
Only when no recording matches is a name search tried, and only an exact,
unambiguous name or alias is accepted -- two "Phoenix"es stay unresolved.

Tables: ``artists`` (one row per MBID, with MusicBrainz genres and their vote
counts), ``artist_aliases`` (every spelling -> MBID), ``track_artists`` (track
-> MBIDs in credit order), ``artist_lookups`` (what was tried per string).
"""
from __future__ import annotations

import re
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from psycopg.types.json import Jsonb

from .logger import log

_SCHEMA = """
CREATE TABLE IF NOT EXISTS artists (
    mbid           TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    sort_name      TEXT NOT NULL DEFAULT '',
    kind           TEXT NOT NULL DEFAULT '',
    country        TEXT NOT NULL DEFAULT '',
    begin_date     TEXT NOT NULL DEFAULT '',
    end_date       TEXT NOT NULL DEFAULT '',
    disambiguation TEXT NOT NULL DEFAULT '',
    wikidata       TEXT NOT NULL DEFAULT '',
    -- MusicBrainz genres, most votes first: [{"name": "folk", "count": 3}, ...]
    genres         JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- The whole response, so later steps need not fetch again.
    raw            JSONB NOT NULL DEFAULT '{}'::jsonb,
    fetched_at     DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS artist_aliases (
    alias_normalized TEXT NOT NULL,
    mbid             TEXT NOT NULL REFERENCES artists(mbid) ON DELETE CASCADE,
    alias            TEXT NOT NULL,
    PRIMARY KEY (alias_normalized, mbid)
);
CREATE INDEX IF NOT EXISTS idx_artist_aliases_mbid ON artist_aliases (mbid);
CREATE TABLE IF NOT EXISTS track_artists (
    track_id       INTEGER NOT NULL REFERENCES tracks(track_id) ON DELETE CASCADE,
    position       SMALLINT NOT NULL,
    mbid           TEXT NOT NULL REFERENCES artists(mbid) ON DELETE CASCADE,
    role           TEXT NOT NULL,
    credited_as    TEXT NOT NULL,
    joinphrase     TEXT NOT NULL DEFAULT '',
    recording_mbid TEXT NOT NULL DEFAULT '',
    method         TEXT NOT NULL,
    resolved_at    DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (track_id, position)
);
CREATE INDEX IF NOT EXISTS idx_track_artists_mbid ON track_artists (mbid);
CREATE TABLE IF NOT EXISTS artist_lookups (
    artist_text TEXT PRIMARY KEY,
    -- resolved | unmatched (nothing convincing) | failed (MusicBrainz unreachable)
    status      TEXT NOT NULL,
    mbids       JSONB NOT NULL DEFAULT '[]'::jsonb,
    method      TEXT NOT NULL DEFAULT '',
    tried_at    DOUBLE PRECISION NOT NULL
);
"""

#: A search hit below this MusicBrainz score is not considered.
MIN_SCORE = 90
#: A recording this close in length is the same edit as the track.
LENGTH_TOLERANCE_S = 5.0
LENGTH_TOLERANCE_RATIO = 0.05
#: Songs of one artist string tried before giving up on recordings.
SAMPLES = 3

#: Separators between credited artists in a free-text artist string. Not "&"
#: or "and" ("Simon & Garfunkel" is one act) and not a bare slash ("AC/DC");
#: when the split is wrong ("Earth, Wind & Fire" -> "Earth") the search still
#: finds the song and :func:`credit_matches` compares the whole credit.
_CREDIT_SPLIT = re.compile(
    r"\s*(?:,|;|\s/\s|\bfeat\.?|\bft\.?|\bfeaturing\b|\bwith\b|\bvs\.?|\sx\s)\s*",
    re.IGNORECASE)
#: MusicBrainz's placeholder for compilations. Its other placeholders --
#: "[unknown]", "[traditional]", "[no artist]" ... -- are bracketed names.
VARIOUS_ARTISTS = "89ad4ac3-39f7-470e-963a-56509c546377"
#: A join phrase that makes the next artist a guest.
_GUEST_JOIN = re.compile(r"\b(?:feat|ft|featuring|with)\b", re.IGNORECASE)


def ensure_schema(conn) -> None:
    conn.execute(_SCHEMA)
    conn.commit()


# -- the pure part -------------------------------------------------------------

def normalize(text: str) -> str:
    """For comparing names and titles: no accents, case, quotes or punctuation.

    "Bonnie “Prince” Billy" and "Bonnie 'Prince' Billy" come out the same;
    so do "Désolé" and "Desole", and "&" and "and".
    """
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).casefold()
    t = t.replace("&", " and ")
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def clean_title(title: str) -> str:
    """A title without version tags: "Désolé (feat. X) - Extended Version" -> "Désolé"."""
    t = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", title or "")
    t = re.split(r"\s+-\s+", t)[0]
    t = re.sub(r"\s+", " ", t).strip()
    return t or (title or "").strip()


def primary_name(artist: str) -> str:
    """The first credited act: "Gorillaz, Fatoumata Diawara" -> "Gorillaz"."""
    first = _CREDIT_SPLIT.split(artist or "", maxsplit=1)[0].strip()
    return first or (artist or "").strip()


@dataclass(frozen=True)
class Credit:
    mbid: str
    credited_as: str
    artist_name: str
    joinphrase: str
    role: str


@dataclass
class Resolution:
    #: resolved | unmatched | failed
    status: str
    credits: list[Credit] = field(default_factory=list)
    method: str = ""
    recording_mbid: str = ""


def is_placeholder(mbid: str, name: str) -> bool:
    """MusicBrainz's special-purpose artists, which are nobody in particular.

    The library's junk artist string "artist" matched "[unknown]" by alias.
    """
    name = (name or "").strip()
    return mbid == VARIOUS_ARTISTS or (name.startswith("[") and name.endswith("]"))


def credits_of(recording: dict[str, Any]) -> list[Credit]:
    """A recording's artist credit, guests marked by the join phrase before them."""
    out: list[Credit] = []
    role = "main"
    for c in recording.get("artist-credit") or []:
        a = c.get("artist") or {}
        if not a.get("id"):
            continue
        out.append(Credit(mbid=a["id"], credited_as=c.get("name") or a.get("name", ""),
                          artist_name=a.get("name", ""), joinphrase=c.get("joinphrase", ""),
                          role=role))
        if _GUEST_JOIN.search(c.get("joinphrase", "")):
            role = "featured"
    return out


def credit_matches(artist_text: str, credits: list[Credit]) -> bool:
    """Whether a recording's credit is the artist string we have.

    The first credited act must be ours (by its credited or canonical name),
    or the whole credit, join phrases and all, must read as our string.
    """
    if not credits or any(is_placeholder(c.mbid, c.artist_name) for c in credits):
        return False
    ours = normalize(primary_name(artist_text))
    first = credits[0]
    if ours and ours in (normalize(first.credited_as), normalize(first.artist_name)):
        return True
    whole = normalize("".join(c.credited_as + c.joinphrase for c in credits))
    return whole == normalize(artist_text)


def same_length(recording_ms: Optional[int], duration_s: Optional[float]) -> bool:
    if not recording_ms or not duration_s:
        return False
    diff = abs(recording_ms / 1000.0 - duration_s)
    return diff <= max(LENGTH_TOLERANCE_S, LENGTH_TOLERANCE_RATIO * duration_s)


def pick_recording(artist_text: str, title: str, duration_s: Optional[float],
                   recordings: list[dict[str, Any]]) -> tuple[Optional[dict], bool]:
    """The best matching recording for one song, and whether its length matched."""
    want = normalize(clean_title(title))
    ok = [r for r in recordings
          if int(r.get("score") or 0) >= MIN_SCORE
          and normalize(clean_title(r.get("title", ""))) == want
          and credit_matches(artist_text, credits_of(r))]
    if not ok:
        return None, False
    timed = [r for r in ok if same_length(r.get("length"), duration_s)]
    return (timed[0], True) if timed else (ok[0], False)


def pick_artist_by_name(artist_text: str, artists: list[dict[str, Any]]) -> Optional[dict]:
    """The one artist whose name or alias is exactly ours, or None if none or several."""
    want = normalize(artist_text)
    exact = {}
    for a in artists:
        if int(a.get("score") or 0) < MIN_SCORE or is_placeholder(a["id"], a.get("name", "")):
            continue
        names = [a.get("name", ""), a.get("sort-name", "")]
        names += [x.get("name", "") for x in a.get("aliases") or []]
        if want and want in {normalize(n) for n in names}:
            exact[a["id"]] = a
    return next(iter(exact.values())) if len(exact) == 1 else None


def resolve(artist_text: str, samples: list[tuple[str, Optional[float]]], mb) -> Resolution:
    """Resolve one artist string from up to ``SAMPLES`` of its songs.

    ``mb`` is the :mod:`karaoke.musicbrainz` module or a stand-in with the same
    ``search_recordings`` and ``search_artists``.
    """
    votes: Counter = Counter()
    first_of: dict[tuple, dict] = {}
    unreachable = False
    for title, duration in samples[:SAMPLES]:
        recs = mb.search_recordings(clean_title(title), primary_name(artist_text))
        if recs is None:
            unreachable = True
            continue
        rec, timed = pick_recording(artist_text, title, duration, recs)
        if rec is None:
            continue
        key = tuple(c.mbid for c in credits_of(rec))
        first_of.setdefault(key, rec)
        if timed:
            return Resolution("resolved", credits_of(rec), "recording", rec.get("id", ""))
        votes[key] += 1
    if votes:
        rec = first_of[votes.most_common(1)[0][0]]
        return Resolution("resolved", credits_of(rec), "recording", rec.get("id", ""))
    found = mb.search_artists(artist_text)
    if found is None:
        return Resolution("failed")
    a = pick_artist_by_name(artist_text, found)
    if a is not None:
        credit = Credit(mbid=a["id"], credited_as=artist_text, artist_name=a.get("name", ""),
                        joinphrase="", role="main")
        return Resolution("resolved", [credit], "name")
    return Resolution("failed" if unreachable else "unmatched")


def parse_artist(data: dict[str, Any]) -> dict[str, Any]:
    """The ``artists`` row and aliases from a MusicBrainz artist lookup."""
    life = data.get("life-span") or {}
    wikidata = next((r["url"]["resource"].rstrip("/").rsplit("/", 1)[-1]
                     for r in data.get("relations") or []
                     if r.get("type") == "wikidata" and (r.get("url") or {}).get("resource")), "")
    genres = sorted(({"name": g["name"], "count": int(g.get("count") or 0)}
                     for g in data.get("genres") or [] if g.get("name")),
                    key=lambda g: (-g["count"], g["name"]))
    aliases = [data.get("name", ""), data.get("sort-name", "")]
    aliases += [a.get("name", "") for a in data.get("aliases") or []]
    return {
        "mbid": data["id"], "name": data.get("name", ""),
        "sort_name": data.get("sort-name", ""), "kind": data.get("type") or "",
        "country": data.get("country") or "", "begin_date": life.get("begin") or "",
        "end_date": life.get("end") or "", "disambiguation": data.get("disambiguation") or "",
        "wikidata": wikidata, "genres": genres,
        "aliases": list(dict.fromkeys(a for a in aliases if a)),
    }


# -- storage -------------------------------------------------------------------

def save_artist(data: dict[str, Any], conn) -> dict[str, Any]:
    """Store (replace) one artist and its aliases from a MusicBrainz lookup."""
    row = parse_artist(data)
    conn.execute("""
        INSERT INTO artists (mbid, name, sort_name, kind, country, begin_date, end_date,
                             disambiguation, wikidata, genres, raw, fetched_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (mbid) DO UPDATE SET
            name = excluded.name, sort_name = excluded.sort_name, kind = excluded.kind,
            country = excluded.country, begin_date = excluded.begin_date,
            end_date = excluded.end_date, disambiguation = excluded.disambiguation,
            wikidata = excluded.wikidata, genres = excluded.genres, raw = excluded.raw,
            fetched_at = excluded.fetched_at""",
        (row["mbid"], row["name"], row["sort_name"], row["kind"], row["country"],
         row["begin_date"], row["end_date"], row["disambiguation"], row["wikidata"],
         Jsonb(row["genres"]), Jsonb(data), time.time()))
    conn.execute("DELETE FROM artist_aliases WHERE mbid = %s", (row["mbid"],))
    for alias in row["aliases"]:
        if normalize(alias):
            conn.execute(
                "INSERT INTO artist_aliases (alias_normalized, mbid, alias) VALUES (%s, %s, %s)"
                " ON CONFLICT DO NOTHING", (normalize(alias), row["mbid"], alias))
    conn.commit()
    return row


def link_tracks(artist_text: str, res: Resolution, conn) -> int:
    """Point every track with this artist string at the resolved artists."""
    ids = [int(r["track_id"]) for r in conn.execute(
        "SELECT track_id FROM tracks WHERE artist = %s", (artist_text,)).fetchall()]
    if not ids:
        return 0
    conn.execute("DELETE FROM track_artists WHERE track_id = ANY(%s)", (ids,))
    now = time.time()
    for tid in ids:
        for pos, c in enumerate(res.credits):
            conn.execute("""
                INSERT INTO track_artists (track_id, position, mbid, role, credited_as,
                                           joinphrase, recording_mbid, method, resolved_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (tid, pos, c.mbid, c.role, c.credited_as, c.joinphrase,
                 res.recording_mbid, res.method, now))
    conn.commit()
    return len(ids)


def record_lookup(artist_text: str, res: Resolution, conn) -> None:
    conn.execute("""
        INSERT INTO artist_lookups (artist_text, status, mbids, method, tried_at)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (artist_text) DO UPDATE SET status = excluded.status,
            mbids = excluded.mbids, method = excluded.method, tried_at = excluded.tried_at""",
        (artist_text, res.status, Jsonb([c.mbid for c in res.credits]), res.method, time.time()))
    conn.commit()


def mbids_for_name(name: str, conn) -> list[str]:
    """Artists known by this spelling, via their aliases."""
    return [r["mbid"] for r in conn.execute(
        "SELECT DISTINCT mbid FROM artist_aliases WHERE alias_normalized = %s",
        (normalize(name),)).fetchall()]


def pending(conn, *, retry_unmatched: bool = False, limit: Optional[int] = None
            ) -> list[dict[str, Any]]:
    """Artist strings still to resolve, most tracks first, with sample songs.

    Skips strings already resolved, and unmatched ones unless asked; strings
    that failed because MusicBrainz was unreachable are always retried.
    """
    skip = ("resolved",) if retry_unmatched else ("resolved", "unmatched")
    rows = conn.execute("""
        SELECT t.artist, count(*) AS n,
               (array_agg(t.title ORDER BY t.duration IS NULL, t.track_id))[1:8] AS titles,
               (array_agg(t.duration ORDER BY t.duration IS NULL, t.track_id))[1:8] AS durations
        FROM tracks t
        LEFT JOIN artist_lookups l ON l.artist_text = t.artist
        WHERE length(trim(t.artist)) > 0 AND (l.status IS NULL OR l.status <> ALL(%s))
        GROUP BY t.artist
        ORDER BY count(*) DESC, t.artist""", (list(skip),)).fetchall()
    out = []
    for r in rows[:limit] if limit else rows:
        samples, seen = [], set()
        for title, dur in zip(r["titles"], r["durations"]):
            key = normalize(clean_title(title))
            if key and key not in seen:
                seen.add(key)
                samples.append((title, dur))
        out.append({"artist": r["artist"], "tracks": int(r["n"]), "samples": samples[:SAMPLES]})
    return out


def run(conn, mb, *, limit: Optional[int] = None, retry_unmatched: bool = False,
        dry_run: bool = False, out: Callable[[str], None] = print) -> Counter:
    """Resolve pending artist strings, fetch new artists, link their tracks."""
    ensure_schema(conn)
    known = {r["mbid"] for r in conn.execute("SELECT mbid FROM artists").fetchall()}
    todo = pending(conn, retry_unmatched=retry_unmatched, limit=limit)
    totals: Counter = Counter()
    for i, item in enumerate(todo, 1):
        text = item["artist"]
        res = resolve(text, item["samples"], mb)
        if res.status == "resolved" and not dry_run:
            for c in res.credits:
                if c.mbid in known:
                    continue
                data = mb.artist(c.mbid)
                if not data:
                    log.warning("artist %s (%s) could not be fetched", c.mbid, c.artist_name)
                    res = Resolution("failed")
                    break
                save_artist(data, conn)
                known.add(c.mbid)
                totals["artists fetched"] += 1
        names = " + ".join(f"{c.artist_name}{' (feat.)' if c.role == 'featured' else ''}"
                           for c in res.credits)
        out(f"[{i}/{len(todo)}] {text[:40]:40} {item['tracks']:>4} tracks  "
            f"{res.status:9} {res.method:9} {names[:60]}")
        totals[res.status] += 1
        if dry_run:
            continue
        if res.status == "resolved":
            totals["tracks linked"] += link_tracks(text, res, conn)
        record_lookup(text, res, conn)
    return totals
