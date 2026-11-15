"""Playlists that rebuild themselves from what the library knows.

Three kinds, all named so they can be told apart from hand-made lists:

* ``Genre: <name>`` -- the same match as the TUI's ``H`` genre filter: an
  artist's genres (broad or specific) when the artist is classified, the
  track's own CLAP genre otherwise.
* ``Smart: <name>`` -- rules over the analysis: the mood timeline's feelings,
  swings and sound-vs-words contrast, and the CLAP timbre of a playlist you
  already have ("More like Komijn 2025").

* ``Decade: <name>`` -- songs by release year (``track_year``), ranked by play
  count mixed with how many other smart lists picked them.

Every list keeps to songs that can be played (a YouTube source), favours
songs with synced lyrics -- this is a karaoke library -- allows two songs per
act, and is then put in *flow* order: each next song is the one that follows
best, so a list plays like a set rather than a sorted table.

"Follows best" starts from the DJ booth's fit (the sound, plus a little mood
hand-off) and adds what makes a *set* smooth rather than a single pick right:
a heavier mood hand-off so the feeling does not lurch, compatible keys, close
tempo, similar chord motion, and the genre family. Genre does two more jobs.
A family with more than five songs in a list plays as one block instead of
being scattered through it. And a family that turns up only once or twice
among a list's candidates is the odd one out for that feeling ("Party" is
mostly rock in this library; its lone country song is the weird mix), so such
songs only fill a list that would otherwise come up short.

Lists rotate between rebuilds: unplayed songs from the tail of the previous
list lead the new one, and the previous opening third and played songs go to
the back (:func:`rotate_candidates`), so a list does not start the same way twice.

Rebuilding replaces each list's contents in place: the saved playlist row and,
with ``--push`` (the default of ``make smart-playlists``), the YouTube Music
playlist of the same name (its id, and so any kiosk link to it, survive).
Nothing here deletes a playlist.

    python -m karaoke.smart_playlists            # rebuild, local only
    python -m karaoke.smart_playlists --push     # and sync YouTube Music
    python -m karaoke.smart_playlists --only "Smart: Dreamy" --dry-run
"""
from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

import numpy as np

from .logger import log

#: Long enough to be a session, short enough for the YouTube Music API, which
#: slows and fails on much longer playlists (the TUI and collector cap at 50).
MAX_TRACKS = 99
PER_ACT = 2
#: Candidates kept before the odd-one-out genres are pushed to the back.
POOL_TRACKS = 2 * MAX_TRACKS
#: A family with more songs than this in a list plays as one block.
CLUSTER_OVER = 5
#: A family with this many songs or fewer among the candidates is an outlier.
LONELY_AT_MOST = 2

#: Transition weights on top of the booth's fit (timbre + 0.1 mood flow).
#: Mood is raised to 0.5 in all: a set should not lurch from one feeling to
#: another, where the booth only ranks single picks. Measured on the 24 lists:
#: 0.3 left More Komijn and Sing-along with bigger emotional jumps than
#: before; 0.8 gave back most of the tempo gain and cost Electronic 0.08 of
#: sound similarity. Key, tempo and chord motion did not predict what radio
#: DJs played next (see dj_suggest), so they stay small -- they smooth a
#: transition, they do not choose it.
MOOD_WEIGHT = 0.5
KEY_WEIGHT = 0.1
TEMPO_WEIGHT = 0.1
HARMONY_WEIGHT = 0.05
#: Tempo closeness decays over this log-ratio (8%: still mixable).
TEMPO_SCALE = 0.08
#: Subtracted from a transition by how far apart the two genre families are.
GENRE_PENALTY = {"same": 0.0, "related": 0.15, "unrelated": 0.4, "unknown": 0.1}
#: Songs this alike (CLAP cosine) lend a genre-less song their family.
NEIGHBOUR_SIMILARITY = 0.75

#: Broad genre families that sit well next to each other in a set.
RELATED_FAMILIES = {
    "rock": {"punk", "metal", "blues", "pop"},
    "punk": {"rock", "metal"},
    "metal": {"rock", "punk"},
    "blues": {"jazz", "soul", "rock", "country", "folk"},
    "jazz": {"blues", "soul", "latin"},
    "soul": {"blues", "jazz", "pop", "hip hop"},
    "folk": {"country", "blues", "world"},
    "country": {"folk", "blues"},
    "electronic": {"ambient", "pop", "hip hop"},
    "ambient": {"electronic", "classical"},
    "pop": {"rock", "soul", "electronic"},
    "hip hop": {"soul", "electronic", "reggae"},
    "reggae": {"hip hop", "latin"},
    "latin": {"jazz", "reggae", "world"},
    "world": {"latin", "folk"},
    "classical": {"ambient"},
}
SOURCE_KIND = "smart"

#: Fraction of the previous playlist treated as the "head" that already opened a session.
HEAD_FRACTION = 3
#: Transition penalty on tracks from the previous head or already played, so unplayed tail/fresh songs lead.
STALE_PENALTY = 0.25

#: Rules where a high play_count is the point of the list, not a reason to demote a track out of the pool.
PLAY_POSITIVE_RULES = frozenset({"Smart: Sing-along Favourites"})

GENRES = ("blues", "jazz", "folk", "soul", "reggae", "country", "latin",
          "ambient", "electronic", "hip hop", "punk", "metal")

#: (playlist suffix after "Genre: ", matching artist/OpenSearch/CLAP sub-genre tags).
SUBGENRES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Alternative Rock", ("alternative rock", "alt-rock")),
    ("Indie Rock", ("indie rock", "slacker rock")),
    ("Indie Pop", ("indie pop", "jangle pop", "chamber pop", "twee pop")),
    ("Art & Experimental Rock", ("art rock", "experimental rock", "avant-prog", "krautrock")),
    ("Progressive Rock", ("progressive rock", "prog rock", "symphonic prog")),
    ("Psychedelic Rock", ("psychedelic rock", "psych rock", "acid rock", "space rock",
                          "space rock revival", "neo-psychedelia")),
    ("Hard Rock & Glam", ("hard rock", "glam rock", "sleaze rock")),
    ("Blues Rock", ("blues rock", "blues-rock", "british blues", "punk blues")),
    ("Folk Rock & Singer-Songwriter", ("folk rock", "singer-songwriter", "indie folk",
                                       "contemporary folk", "freak folk", "anti-folk")),
    ("Post-Punk", ("post-punk", "post-punk revival", "proto-punk", "art punk", "dance-punk")),
    ("New Wave & Synth-Pop", ("new wave", "synth-pop", "synthpop", "electropop", "dark wave")),
    ("Shoegaze & Dream Pop", ("shoegaze", "dream pop", "post-rock")),
    ("Stoner & Doom", ("stoner rock", "desert rock", "stoner metal", "doom metal", "sludge metal")),
    ("Grunge & Noise Rock", ("grunge", "post-grunge", "noise rock", "post-hardcore")),
    ("Garage Rock", ("garage rock", "garage rock revival", "garage punk", "pub rock")),
    ("Funk & Disco", ("funk", "funk rock", "funk metal", "disco", "boogie", "jazz-funk")),
    ("Trip Hop & Downtempo", ("trip hop", "trip-hop", "downtempo")),
    ("IDM & Breakbeat", ("idm", "glitch", "breakcore", "ambient techno", "drum and bass", "breakbeat")),
    ("Techno & House", ("techno", "house", "electro", "electro house", "acid techno",
                        "deep house", "french house", "tech house")),
    ("Swing & Gypsy Jazz", ("gypsy jazz", "jazz manouche", "swing", "big band", "vocal jazz", "dixieland")),
    ("Jazz Fusion & Bop", ("jazz fusion", "jazz rock", "hard bop", "post-bop",
                           "bebop", "cool jazz", "modal jazz")),
    ("Ska & Dub", ("ska", "dub", "rocksteady", "2 tone", "ska punk")),
    ("Americana & Alt-Country", ("americana", "alt-country", "alternative country",
                                 "country rock", "outlaw country", "rockabilly", "bluegrass")),
    ("Industrial", ("industrial", "industrial rock", "industrial metal", "ebm", "electro-industrial")),
)


@dataclass(frozen=True)
class Rule:
    name: str
    description: str
    #: (conn, os_client) -> candidate track ids, best first.
    select: Callable[[Any, Any], list[int]]


# -- selectors ---------------------------------------------------------------

#: Lyrics first: synced, then plain, then none. Unplayed tracks next (negative play_count).
_KARAOKE_ORDER = """
    CASE WHEN EXISTS (SELECT 1 FROM lyrics l WHERE l.track_id = t.track_id
                       AND COALESCE(l.synced_lyrics, '') != '') THEN 0
         WHEN EXISTS (SELECT 1 FROM lyrics l WHERE l.track_id = t.track_id
                       AND COALESCE(l.plain_lyrics, '') != '') THEN 1
         ELSE 2 END,
    COALESCE(t.play_count, 0) ASC, t.track_id"""


def _opensearch_subgenre_matches(tags: list[str], os_client: Any
                                 ) -> tuple[set[str], set[str], set[str], set[str], set[int]]:
    """Query OpenSearch ``karaoke-artists`` and ``tracks`` for sub-genre tags.

    Returns ``(top_mbids, all_mbids, top_artists, all_artists, track_ids)``.
    Best-effort: returns empty sets if ``os_client`` is None or unreachable.
    """
    top_mbids: set[str] = set()
    all_mbids: set[str] = set()
    top_artists: set[str] = set()
    all_artists: set[str] = set()
    track_ids: set[int] = set()
    if os_client is None or not tags:
        return top_mbids, all_mbids, top_artists, all_artists, track_ids
    try:
        from .artist_index import ARTIST_INDEX
        res = os_client.search(index=ARTIST_INDEX, body={
            "size": 2000,
            "_source": ["mbid", "library_names", "genres"],
            "query": {"terms": {"genres": tags}},
        })
        for h in (res.get("hits", {}).get("hits") or []):
            src = h.get("_source") or {}
            mbid = src.get("mbid")
            g_list = [str(g).strip().lower() for g in (src.get("genres") or []) if g]
            is_top = bool(g_list and g_list[0] in tags)
            if mbid:
                all_mbids.add(str(mbid))
                if is_top:
                    top_mbids.add(str(mbid))
            for ln in (src.get("library_names") or []):
                norm = str(ln).strip().lower()
                if norm:
                    all_artists.add(norm)
                    if is_top:
                        top_artists.add(norm)
    except Exception:
        pass
    try:
        from .config import settings
        res_t = os_client.search(index=settings.index_name, body={
            "size": 3000,
            "_source": ["track_id"],
            "query": {"bool": {"should": [
                {"terms": {"genre.keyword": tags}},
                {"terms": {"audio_genre.keyword": tags}},
                {"terms": {"artist_genres.keyword": tags}},
            ], "minimum_should_match": 1}},
        })
        for h in (res_t.get("hits", {}).get("hits") or []):
            tid = (h.get("_source") or {}).get("track_id")
            if tid is not None:
                track_ids.add(int(tid))
    except Exception:
        pass
    return top_mbids, all_mbids, top_artists, all_artists, track_ids


def subgenre_ids(tags: tuple[str, ...] | list[str] | str, conn, os_client: Any = None) -> list[int]:
    """Tracks matching a sub-genre cluster across OpenSearch and PostgreSQL, best first."""
    from . import localcache

    wanted = [t.strip().lower() for t in (tags if isinstance(tags, (tuple, list)) else (tags,))
              if t and t.strip()]
    if not wanted:
        return []
    top_mbids, all_mbids, top_artists, all_artists, os_tids = _opensearch_subgenre_matches(wanted, os_client)
    mbid_tids: set[int] = set()
    top_mbid_tids: set[int] = set()
    if all_mbids and localcache.table_exists(conn, "track_artists"):
        try:
            for r in conn.execute(
                "SELECT track_id, mbid FROM track_artists WHERE position = 0 AND mbid = ANY(%s)",
                (list(all_mbids),),
            ).fetchall():
                tid = int(r["track_id"])
                mbid_tids.add(tid)
                if r["mbid"] in top_mbids:
                    top_mbid_tids.add(tid)
        except Exception:
            pass

    rows = conn.execute(f"""
        WITH spec AS (
            SELECT artist_normalized,
                   sum(weight) FILTER (WHERE lower(trim(genre)) = ANY(%(tags)s)) AS matched_w,
                   max(weight) AS top_w,
                   count(DISTINCT lower(trim(broad_genre))) AS n_broad
            FROM artist_genres
            GROUP BY artist_normalized
        )
        SELECT t.track_id FROM tracks t
        LEFT JOIN track_genre g ON g.track_id = t.track_id
        LEFT JOIN spec s ON s.artist_normalized = lower(trim(t.artist))
        WHERE COALESCE(s.matched_w, 0) > 0
           OR lower(trim(t.artist)) = ANY(%(os_artists)s)
           OR t.track_id = ANY(%(mbid_tids)s)
           OR (s.n_broad IS NULL
               AND NOT (lower(trim(t.artist)) = ANY(%(os_artists)s))
               AND (lower(trim(COALESCE(g.genre, ''))) = ANY(%(tags)s)
                    OR t.track_id = ANY(%(os_tids)s)))
        ORDER BY CASE WHEN (COALESCE(s.matched_w, 0) > 0 AND 2 * s.matched_w >= s.top_w)
                        OR lower(trim(t.artist)) = ANY(%(os_top_artists)s)
                        OR t.track_id = ANY(%(top_mbid_tids)s) THEN 0
                      WHEN COALESCE(s.matched_w, 0) > 0
                        OR lower(trim(t.artist)) = ANY(%(os_artists)s)
                        OR t.track_id = ANY(%(mbid_tids)s) THEN 1
                      ELSE 2 END,
                 s.n_broad NULLS LAST,
                 {_KARAOKE_ORDER}""", {
        "tags": wanted,
        "os_artists": list(all_artists),
        "os_top_artists": list(top_artists),
        "mbid_tids": list(mbid_tids),
        "top_mbid_tids": list(top_mbid_tids),
        "os_tids": list(os_tids),
    }).fetchall()
    return [int(r["track_id"]) for r in rows]


def genre_ids(genre: str, conn) -> list[int]:
    """Tracks the TUI's ``H`` filter shows for this genre, best first.

    The filter's match, ranked by how much the song *belongs* to the genre.
    "Has the genre" is not enough: The Doors are tagged blues (via "blues
    rock") and opened "Blues"; Zappa and King Crimson are tagged jazz (via
    "jazz fusion") and opened "Jazz". An artist's *main* genre is the broad
    genre with the most weight behind it: MusicBrainz votes for artists
    resolved by :mod:`karaoke.artist_identity` (Zappa: rock 70-odd votes,
    jazz about 20), one per specific tag for the seed list and the
    name-searched classifier (The Doors: acid, blues and psychedelic rock ->
    rock 3, blues 1). A tie keeps both (Rory Gallagher: blues 2, rock 2). An act counts for a genre
    only when it is more than half as strong as its main one: Biohazard (rap
    metal: hip hop 2, metal 3) is in "Hip Hop", System Of A Down (one nu metal
    tag: hip hop 1, metal 4) is not -- so these lists are narrower than the
    TUI's ``H`` filter, which takes any tag. Ranked: acts whose main genre this
    is, then the other strong ones, then tracks known only by the CLAP guess
    on the song itself -- the least reliable ("shoegaze" Billie Holiday).
    Within each, the more dedicated act (fewer broad genres) first.
    """
    rows = conn.execute(f"""
        WITH tags AS (
            SELECT artist_normalized, lower(trim(broad_genre)) AS broad, sum(weight) AS n
            FROM artist_genres GROUP BY artist_normalized, lower(trim(broad_genre))),
        strength AS (
            SELECT artist_normalized, broad, n,
                   max(n) OVER (PARTITION BY artist_normalized) AS top FROM tags),
        breadth AS (
            SELECT artist_normalized, count(*) AS n FROM tags GROUP BY artist_normalized)
        SELECT t.track_id FROM tracks t
        LEFT JOIN track_genre g ON g.track_id = t.track_id
        LEFT JOIN breadth b ON b.artist_normalized = lower(trim(t.artist))
        WHERE EXISTS (SELECT 1 FROM strength s
                       WHERE s.artist_normalized = lower(trim(t.artist))
                         AND s.broad = lower(trim(%(g)s)) AND 2 * s.n > s.top)
           OR EXISTS (SELECT 1 FROM artist_genres ag
                       WHERE lower(trim(ag.genre)) = lower(trim(%(g)s))
                         AND ag.artist_normalized = lower(trim(t.artist)))
           OR (b.n IS NULL
               AND lower(trim(COALESCE(g.genre, ''))) = lower(trim(%(g)s)))
        ORDER BY CASE WHEN b.n IS NULL THEN 2
                      WHEN EXISTS (SELECT 1 FROM strength s
                                    WHERE s.artist_normalized = lower(trim(t.artist))
                                      AND s.broad = lower(trim(%(g)s))
                                      AND s.n = s.top) THEN 0
                      ELSE 1 END,
                 b.n NULLS LAST,
                 {_KARAOKE_ORDER}""", {"g": genre}).fetchall()
    return [int(r["track_id"]) for r in rows]


def mood_ids(conn, where: str, params: tuple = (), order: str = "") -> list[int]:
    """Tracks whose current mood timeline matches ``where`` (over ``m``)."""
    from . import mood_timeline

    rows = conn.execute(f"""
        SELECT t.track_id FROM tracks t
        JOIN track_moods m ON m.track_id = t.track_id AND m.mood_version = %s
        WHERE {where}
        ORDER BY {order + ',' if order else ''} {_KARAOKE_ORDER}""",
        (mood_timeline.MOOD_VERSION, *params)).fetchall()
    return [int(r["track_id"]) for r in rows]


def feeling_ids(conn, *feelings: str) -> list[int]:
    return mood_ids(conn, "m.summary->>'dominant_feeling' = ANY(%s)", (list(feelings),))


def slow_burn_ids(conn) -> list[int]:
    """Quiet start, loud finish: the last section well above the first."""
    return mood_ids(
        conn,
        "jsonb_array_length(m.segments) >= 2"
        " AND (m.segments->0->>'arousal')::float < 0"
        " AND (m.segments->-1->>'arousal')::float"
        "     - (m.segments->0->>'arousal')::float > 0.5",
        order="(m.segments->-1->>'arousal')::float - (m.segments->0->>'arousal')::float DESC")


def playlist_track_ids(name: str, conn) -> list[int]:
    """The library tracks of the biggest saved playlist with this name."""
    row = conn.execute(
        "SELECT playlist_id FROM saved_playlists WHERE name = %s"
        " ORDER BY track_count DESC NULLS LAST LIMIT 1", (name,)).fetchone()
    if row is None:
        return []
    rows = conn.execute(
        "SELECT DISTINCT track_id FROM saved_playlist_tracks"
        " WHERE playlist_id = %s AND track_id IS NOT NULL", (row["playlist_id"],)).fetchall()
    return [int(r["track_id"]) for r in rows]


def sounds_like_playlist(name: str, conn, os_client, k: int = 150) -> list[int]:
    """Songs nearest the CLAP centroid of a playlist, not already on it."""
    from . import clap_vector

    seeds = playlist_track_ids(name, conn)
    if not seeds or os_client is None:
        return []
    docs = os_client.mget(index=clap_vector.CLAP_INDEX, _source=["clap_vector"],
                          body={"ids": [clap_vector.doc_id(t) for t in seeds]})["docs"]
    vectors = [np.asarray(d["_source"]["clap_vector"], dtype=np.float32)
               for d in docs if d.get("found") and d["_source"].get("clap_vector")]
    if not vectors:
        return []
    centroid = np.mean([v / (np.linalg.norm(v) or 1.0) for v in vectors], axis=0)
    res = os_client.search(index=clap_vector.CLAP_INDEX, body={
        "size": k, "_source": False,
        "query": {"knn": {"clap_vector": {"vector": centroid.tolist(), "k": k}}}})
    own = set(seeds)
    out = []
    for hit in res["hits"]["hits"]:
        tid = int(str(hit["_id"]).split(":")[-1])
        if tid not in own:
            out.append(tid)
    return out


def sing_along_ids(conn) -> list[int]:
    """The most played songs with synced lyrics."""
    rows = conn.execute("""
        SELECT t.track_id FROM tracks t
        WHERE COALESCE(t.play_count, 0) > 0
          AND EXISTS (SELECT 1 FROM lyrics l WHERE l.track_id = t.track_id
                       AND COALESCE(l.synced_lyrics, '') != '')
        ORDER BY t.play_count DESC, t.track_id""").fetchall()
    return [int(r["track_id"]) for r in rows]


def discover_ids(conn) -> list[int]:
    """Unplayed tracks, ripe for discovery."""
    rows = conn.execute("""
        SELECT t.track_id FROM tracks t
        WHERE COALESCE(t.play_count, 0) = 0
        ORDER BY RANDOM()""").fetchall()
    return [int(r["track_id"]) for r in rows]


#: (rule name suffix, first year, last year); the oldest list has no lower bound.
DECADES = (("Before 1960", 0, 1959), ("1960s", 1960, 1969), ("1970s", 1970, 1979),
           ("1980s", 1980, 1989), ("1990s", 1990, 1999), ("2000s", 2000, 2009),
           ("2010s", 2010, 2019), ("2020s", 2020, 2029))


def decade_ids(conn, first: int, last: int, exclude_playlist: str = "") -> list[int]:
    """Tracks released in ``first``..``last``, best first.

    Few songs have been played, so play count alone cannot rank a decade: it is
    mixed with how many *other* smart/genre lists already picked the song (a vetted
    signal from the mood and genre rules) and with karaoke-readiness (synced lyrics).
    A song's year is the release year in ``track_year``; a remaster/remix year found in
    a title is a reissue date and does not place a song in a decade.
    """
    rows = conn.execute("""
        SELECT t.track_id,
               2.0 * LEAST(COALESCE(t.play_count, 0), 3)
             + 1.5 * LEAST(COALESCE(o.lists, 0), 3)
             + CASE WHEN EXISTS (SELECT 1 FROM lyrics l WHERE l.track_id = t.track_id
                                  AND COALESCE(l.synced_lyrics, '') != '') THEN 1.0
                    WHEN EXISTS (SELECT 1 FROM lyrics l WHERE l.track_id = t.track_id
                                  AND COALESCE(l.plain_lyrics, '') != '') THEN 0.5
                    ELSE 0 END AS score
        FROM tracks t
        JOIN track_year y ON y.track_id = t.track_id AND y.source <> 'title-remaster'
        LEFT JOIN (
            SELECT spt.track_id, COUNT(DISTINCT sp.playlist_id) AS lists
            FROM saved_playlist_tracks spt
            JOIN saved_playlists sp ON sp.playlist_id = spt.playlist_id
            WHERE sp.source_kind = %s AND sp.name <> %s AND spt.track_id IS NOT NULL
            GROUP BY spt.track_id
        ) o ON o.track_id = t.track_id
        WHERE y.year BETWEEN %s AND %s
        ORDER BY score DESC, t.track_id""",
        (SOURCE_KIND, exclude_playlist, first, last)).fetchall()
    return [int(r["track_id"]) for r in rows]


def _decade_rule(label: str, first: int, last: int) -> Rule:
    name = f"Decade: {label}"
    return Rule(name,
                f"Songs from {label}: most played and most picked by the other lists first, "
                "synced lyrics preferred, in flow order",
                lambda conn, osc: decade_ids(conn, first, last, exclude_playlist=name))


def _genre_rule(genre: str) -> Rule:
    return Rule(f"Genre: {genre.title()}",
                f"Like the TUI's H filter on '{genre}', lyrics first, in flow order",
                lambda conn, osc, g=genre: genre_ids(g, conn))


def _subgenre_rule(label: str, tags: tuple[str, ...]) -> Rule:
    return Rule(f"Genre: {label}",
                f"Sub-genre '{label}' ({', '.join(tags[:3])}) from OpenSearch & library tags, in flow order",
                lambda conn, osc, ts=tags: subgenre_ids(ts, conn, osc))


RULES: tuple[Rule, ...] = (
    tuple(_genre_rule(g) for g in GENRES)
    + tuple(_subgenre_rule(label, tags) for label, tags in SUBGENRES)
    + (
        Rule("Smart: Bittersweet & Ironic",
             "Sound and words pull opposite ways: a warm sound over dark words, or a cynical tone",
             lambda conn, osc: mood_ids(conn, "m.summary->>'contrast' IN ('bittersweet', 'ironic')")),
        Rule("Smart: Slow Burn",
             "Starts quiet, ends loud: the build-ups",
             lambda conn, osc: slow_burn_ids(conn)),
        Rule("Smart: Dreamy",
             "Dreamy and wistful: floating, spacey, a little far away",
             lambda conn, osc: feeling_ids(conn, "dreamy", "wistful")),
        Rule("Smart: Blue & Frustrated",
             "The blues feeling: aching, frustrated, not angry",
             lambda conn, osc: feeling_ids(conn, "frustrated", "blue")),
        Rule("Smart: Longing",
             "Missing someone, something, somewhere",
             lambda conn, osc: feeling_ids(conn, "longing", "melancholic")),
        Rule("Smart: Romantic",
             "Romantic, tender, hopeful",
             lambda conn, osc: feeling_ids(conn, "romantic", "tender", "hopeful")),
        Rule("Smart: Party",
             "Festive, cheerful, energetic, uplifting",
             lambda conn, osc: feeling_ids(conn, "festive", "cheerful", "energetic", "uplifting")),
        Rule("Smart: Mood Swings",
             "Songs that change their mind: three or more mood swings",
             lambda conn, osc: mood_ids(conn, "(m.summary->>'swing_count')::int >= 3",
                                        order="(m.summary->>'swings_per_minute')::float DESC")),
        Rule("Smart: More Sunday",
             "Sounds like your 'sunday' list, without repeating it",
             lambda conn, osc: sounds_like_playlist("sunday", conn, osc)),
        Rule("Smart: More Komijn",
             "Sounds like 'Komijn 2025', without repeating it",
             lambda conn, osc: sounds_like_playlist("Komijn 2025", conn, osc)),
        Rule("Smart: Radio Discoveries Set",
             "Songs radio mode found, sequenced as a set",
             lambda conn, osc: playlist_track_ids("Karaoke: Radio Discoveries", conn)),
        Rule("Smart: Sing-along Favourites",
             "Your most played songs with synced lyrics",
             lambda conn, osc: sing_along_ids(conn)),
        Rule("Smart: Borderline Moods",
             "Songs that shift or sit between two clear moods (rate weighted)",
             lambda conn, osc: mood_ids(conn, "(m.summary->>'borderline_share')::float > 0.3",
                                        order="(m.summary->>'borderline_share')::float DESC")),
        Rule("Smart: To Discover",
             "Tracks you have never played, grouped smoothly by genre",
             lambda conn, osc: discover_ids(conn)),
    )
    + tuple(_decade_rule(*d) for d in DECADES)
)


# -- shaping -------------------------------------------------------------------

def playable(track_ids: list[int], conn) -> list[dict[str, Any]]:
    """Queue rows for the ids that have a YouTube source, in the given order."""
    from . import collection
    from .ytmusic_playlist import extract_video_id

    return [r for r in collection.rows_for(track_ids, conn) if extract_video_id(r["url"])]


def song_key(title: str) -> str:
    """A title without its version tags: "Call Me (12'' Version)" -> "call me"."""
    t = re.sub(r"[\(\[].*?[\)\]]", " ", title.lower())
    t = re.split(r"\s+-\s+", t)[0]
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def act_keys(rows: list[dict[str, Any]], conn) -> dict[int, str]:
    """Who each song is by, for "two songs per act": the first credited artist.

    By MusicBrainz ID when :mod:`karaoke.artist_identity` has resolved the
    track, so "Bonnie "Prince" Billy" and "Bonnie Prince Billy", or "Gorillaz"
    and "Gorillaz feat. Fatoumata Diawara", are one act. Otherwise by name
    (:func:`dj_suggest.artist_key`).
    """
    from . import localcache
    from .dj_suggest import artist_key

    keys = {r["track_id"]: artist_key(r["artist"]) for r in rows}
    if rows and localcache.table_exists(conn, "track_artists"):
        for r in conn.execute(
                "SELECT track_id, mbid FROM track_artists"
                " WHERE position = 0 AND track_id = ANY(%s)",
                ([r["track_id"] for r in rows],)).fetchall():
            keys[int(r["track_id"])] = r["mbid"]
    return keys


def cap_per_act(rows: list[dict[str, Any]], per_act: int = PER_ACT,
                limit: int = MAX_TRACKS,
                acts: Optional[dict[int, str]] = None) -> list[dict[str, Any]]:
    """At most ``per_act`` songs per act, one version of each song, up to ``limit``.

    ``acts`` (from :func:`act_keys`) says who each song is by; without it,
    the artist name does.
    """
    from .dj_suggest import artist_key

    seen: dict[str, int] = {}
    # One version of a song per list, whoever plays it: "Rory Gallagher &
    # Muddy Waters" is another act but the same song, and flow order would
    # put the two side by side because they sound alike.
    songs: set[str] = set()
    out = []
    for r in rows:
        key = (acts or {}).get(r["track_id"]) or artist_key(r["artist"])
        song = song_key(r["title"])
        if seen.get(key, 0) >= per_act or song in songs:
            continue
        songs.add(song)
        seen[key] = seen.get(key, 0) + 1
        out.append(r)
        if len(out) >= limit:
            break
    return out


def genre_options(rows: list[dict[str, Any]], conn) -> dict[int, list[str]]:
    """The broad genre families each song could belong to.

    The artist's genres when the artist is classified -- by full name, then by
    the act ("Tommy Dorsey with Frank Sinatra" -> "tommy dorsey") -- otherwise
    the families of the CLAP label on the song itself, the weaker guess. As in
    :func:`genre_ids`, a family counts only when it has more than half the
    weight of the artist's strongest one: Zappa is rock, not also classical
    because a few voters said "modern classical".
    """
    from .artist_classifier import map_to_broad_genres
    from .dj_suggest import artist_key

    def names(artist: str) -> list[str]:
        full = (artist or "").strip().lower()
        act = artist_key(artist)
        return list(dict.fromkeys(n for n in (full, act, f"the {act}") if n))

    wanted = sorted({n for r in rows for n in names(r["artist"])})
    weights: dict[str, dict[str, float]] = {}
    for g in conn.execute(
            "SELECT artist_normalized, lower(trim(broad_genre)) AS broad, sum(weight) AS w"
            " FROM artist_genres WHERE artist_normalized = ANY(%s)"
            " GROUP BY artist_normalized, lower(trim(broad_genre))"
            " ORDER BY artist_normalized, broad", (wanted,)).fetchall():
        weights.setdefault(g["artist_normalized"], {})[g["broad"]] = float(g["w"] or 0)
    by_artist = {a: [f for f, w in fams.items() if 2 * w > max(fams.values())]
                 for a, fams in weights.items()}
    clap = {int(g["track_id"]): g["genre"] for g in conn.execute(
        "SELECT track_id, genre FROM track_genre WHERE track_id = ANY(%s)",
        ([r["track_id"] for r in rows],)).fetchall()}
    out: dict[int, list[str]] = {}
    for r in rows:
        fams = next((by_artist[n] for n in names(r["artist"]) if n in by_artist), [])
        if not fams and r["track_id"] in clap:
            fams = map_to_broad_genres(clap[r["track_id"]])
        out[r["track_id"]] = list(fams)
    return out


def assign_families(options: dict[int, list[str]], tracks: dict[int, Any]
                    ) -> dict[int, Optional[str]]:
    """One family per song, chosen to suit the list it is in.

    An artist tagged rock, pop and electronic is "rock" in a rock-heavy list
    and "electronic" in an electronic one: each song takes whichever of its
    families the list as a whole leans to most. A song with no genre at all
    borrows the family most of its closest-sounding listmates share.
    """
    from collections import Counter

    from .dj_suggest import cosine

    votes = Counter(f for fams in options.values() for f in fams)
    fam: dict[int, Optional[str]] = {
        tid: (max(fams, key=lambda f: votes[f]) if fams else None)
        for tid, fams in options.items()}
    known = [t for t, f in fam.items() if f]
    for tid in [t for t, f in fam.items() if not f]:
        me = getattr(tracks.get(tid), "clap", None)
        if me is None:
            continue
        near = sorted(((cosine(me, getattr(tracks.get(o), "clap", None)), o) for o in known),
                      reverse=True)[:3]
        near_fams = Counter(fam[o] for sim, o in near if sim >= NEIGHBOUR_SIMILARITY)
        if near_fams:
            fam[tid] = near_fams.most_common(1)[0][0]
    return fam


def prefer_common_families(rows: list[dict[str, Any]], families: dict[int, Optional[str]],
                           limit: int = MAX_TRACKS) -> list[dict[str, Any]]:
    """Up to ``limit`` rows, odd-one-out genres only if the list needs them.

    Keeps the rule's order. A song whose family has at most
    ``LONELY_AT_MOST`` songs among the candidates is used only to fill.
    """
    from collections import Counter

    counts = Counter(families.get(r["track_id"]) for r in rows)

    def lonely(r) -> bool:
        f = families.get(r["track_id"])
        return f is not None and counts[f] <= LONELY_AT_MOST

    keep = {r["track_id"] for r in sorted(rows, key=lonely)[:limit]}
    return [r for r in rows if r["track_id"] in keep]


def genre_penalty(a: Optional[str], b: Optional[str]) -> float:
    if not a or not b:
        return GENRE_PENALTY["unknown"]
    if a == b:
        return GENRE_PENALTY["same"]
    if b in RELATED_FAMILIES.get(a, ()) or a in RELATED_FAMILIES.get(b, ()):
        return GENRE_PENALTY["related"]
    return GENRE_PENALTY["unrelated"]


def transition(cur, cand) -> float:
    """How well ``cand`` follows ``cur`` in a set, before genre and act."""
    import math

    from . import dj_suggest

    s = dj_suggest.score(cur, cand, "flow")
    if s is None:
        return -1.0
    s += (MOOD_WEIGHT - dj_suggest.MOOD_WEIGHT) * dj_suggest.mood_flow(cur, cand)
    s += KEY_WEIGHT * dj_suggest.camelot_score(cur.camelot, cand.camelot)
    ratio = dj_suggest.tempo_ratio(cur.bpm, cand.bpm)
    if ratio is not None:
        s += TEMPO_WEIGHT * math.exp(-ratio / TEMPO_SCALE)
    s += HARMONY_WEIGHT * dj_suggest.cosine(cur.motion, cand.motion)
    return s


def flow_order(rows: list[dict[str, Any]], conn, os_client,
               families: Optional[dict[int, Optional[str]]] = None,
               tracks: Optional[dict[int, Any]] = None,
               acts: Optional[dict[int, str]] = None,
               stale_ids: Optional[set[int]] = None) -> list[dict[str, Any]]:
    """Sequence the rows as a set: each next song follows the last best.

    Starts from the first row (the rule's best pick) and greedily takes the
    candidate that follows the current song best (:func:`transition`, less
    :func:`genre_penalty`). Avoids two songs by one act back to back when
    anything else fits. With ``families``, a family holding more than
    ``CLUSTER_OVER`` songs is played out as one block before the list moves
    on. Smaller families go between blocks; genre-less songs may go anywhere,
    a block included, wherever they fit -- kept out of blocks they all piled
    up at the end of the list. When ``stale_ids`` (tracks from the previous
    list's opening head or already played) is given, they take a small
    :data:`STALE_PENALTY` so unplayed tail and fresh songs sequence first.
    """
    from collections import Counter

    from . import dj_suggest

    if len(rows) < 3:
        return rows
    if tracks is None:
        tracks = dj_suggest._load([r["track_id"] for r in rows], conn, os_client)
    fam = families or {}
    blocks = {f for f, n in Counter(fam.get(r["track_id"]) for r in rows).items()
              if f and n > CLUSTER_OVER}
    by_id = {r["track_id"]: r for r in rows}
    stale = stale_ids or set()

    def act_of(tid: int) -> str:
        return (acts or {}).get(tid) or dj_suggest.artist_key(by_id[tid]["artist"])

    order = [rows[0]["track_id"]]
    left = [r["track_id"] for r in rows[1:]]
    while left:
        cur = tracks.get(order[-1])
        cur_act = act_of(order[-1])
        # The family the list is in: a genre-less song inside a block does
        # not end the block.
        cur_fam = next((fam[t] for t in reversed(order) if fam.get(t)), None)

        def fit(tid: int) -> float:
            cand = tracks.get(tid)
            s = transition(cur, cand) if cur and cand else -1.0
            same_act = act_of(tid) == cur_act
            if families is not None:
                s -= genre_penalty(cur_fam, fam.get(tid))
            if tid in stale:
                s -= STALE_PENALTY
            return s - (1.0 if same_act else 0.0)

        pool = left
        if cur_fam in blocks and any(fam.get(t) == cur_fam for t in left):
            pool = [t for t in left if fam.get(t) in (cur_fam, None)]
        nxt = max(pool, key=fit)
        order.append(nxt)
        left.remove(nxt)
    return [by_id[t] for t in order]


def previous_state(rule_name: str, rows: list[dict[str, Any]], conn
                   ) -> tuple[dict[int, int], dict[int, int]]:
    """Return ``(prev_positions, play_counts)`` for ``rows`` from the database.

    ``prev_positions`` maps ``track_id -> 1-based position`` in the currently
    saved playlist for ``rule_name`` (before it is overwritten). ``play_counts``
    maps ``track_id -> play_count`` from ``tracks``, incremented when the song
    was logged in ``play_events`` since the playlist was last saved.
    """
    from . import localcache

    if conn is None or not rows:
        return {}, {}
    prev_pos: dict[int, int] = {}
    play_counts: dict[int, int] = {}
    pid = slug(rule_name)
    updated_at = 0.0
    try:
        if localcache.table_exists(conn, "saved_playlists"):
            pl = conn.execute(
                "SELECT updated_at FROM saved_playlists WHERE playlist_id = %s",
                (pid,),
            ).fetchone()
            if pl and pl.get("updated_at"):
                updated_at = float(pl["updated_at"])
        if localcache.table_exists(conn, "saved_playlist_tracks"):
            for i, r in enumerate(conn.execute(
                "SELECT track_id, position FROM saved_playlist_tracks"
                " WHERE playlist_id = %s AND track_id IS NOT NULL ORDER BY position",
                (pid,),
            ).fetchall(), start=1):
                prev_pos[int(r["track_id"])] = int(r["position"] or i)
    except Exception:
        pass
    tids = [int(r["track_id"]) for r in rows if r.get("track_id") is not None]
    try:
        if tids and localcache.table_exists(conn, "tracks"):
            for r in conn.execute(
                "SELECT track_id, COALESCE(play_count, 0) AS play_count"
                " FROM tracks WHERE track_id = ANY(%s)",
                (tids,),
            ).fetchall():
                play_counts[int(r["track_id"])] = int(r["play_count"] or 0)
        if updated_at > 0 and localcache.table_exists(conn, "play_events"):
            recent = {
                (str(r["artist"] or "").strip().lower(), song_key(str(r["title"] or "")))
                for r in conn.execute(
                    "SELECT artist, title FROM play_events"
                    " WHERE event = 'play' AND ts >= %s",
                    (updated_at,),
                ).fetchall()
            }
            if recent:
                for r in rows:
                    key = (str(r.get("artist") or "").strip().lower(),
                           song_key(str(r.get("title") or "")))
                    if key in recent:
                        tid = int(r["track_id"])
                        play_counts[tid] = play_counts.get(tid, 0) + 1
    except Exception:
        pass
    return prev_pos, play_counts


def rotate_candidates(rows: list[dict[str, Any]], prev_pos: dict[int, int],
                      play_counts: dict[int, int], *,
                      penalize_plays: bool = True) -> tuple[list[dict[str, Any]], set[int]]:
    """Reorder candidates so unplayed end-of-list songs move up and played/head songs sink.

    Returns ``(reordered_rows, stale_track_ids)``:
    - Tier 0: unplayed songs from the tail of the previous playlist (``pos > head_cut``),
      ordered from the end of the previous list upward (``-pos``) so songs that sat
      at the bottom move to the top.
    - Tier 1: fresh unplayed songs that were not in the previous playlist.
    - Tier 2: unplayed songs from the opening head of the previous playlist
      (``pos <= head_cut``), rotated behind the tail and fresh tracks.
    - Tier 3: played songs (``play_count > 0`` when ``penalize_plays`` is True),
      ordered by fewest plays first.
    """
    has_plays = penalize_plays and any(v > 0 for v in play_counts.values())
    if not prev_pos and not has_plays:
        return list(rows), set()

    head_cut = max(1, len(prev_pos) // HEAD_FRACTION) if prev_pos else 0
    stale: set[int] = set()

    def sort_key(item: tuple[int, dict[str, Any]]) -> tuple[int, int, int]:
        idx, r = item
        tid = int(r["track_id"])
        plays = play_counts.get(tid, 0) if penalize_plays else 0
        pos = prev_pos.get(tid)
        if plays > 0:
            stale.add(tid)
            return (3, plays, pos if pos is not None else idx)
        if pos is not None and pos > head_cut:
            return (0, -pos, idx)
        if pos is None:
            return (1, 0, idx)
        stale.add(tid)
        return (2, pos, idx)

    ordered = [r for _, r in sorted(enumerate(rows), key=sort_key)]
    return ordered, stale


def build(rule: Rule, conn, os_client) -> list[dict[str, Any]]:
    """The rule's playlist rows, ready to save or push."""
    from . import dj_suggest

    ids = rule.select(conn, os_client)
    candidates = playable(ids, conn)
    penalize = not (rule.name in PLAY_POSITIVE_RULES or rule.name.startswith("Decade: "))
    prev_pos, play_counts = previous_state(rule.name, candidates, conn)
    candidates, stale_ids = rotate_candidates(
        candidates, prev_pos, play_counts, penalize_plays=penalize)
    acts = act_keys(candidates, conn)
    pool = cap_per_act(candidates, limit=POOL_TRACKS, acts=acts)
    tracks = dj_suggest._load([r["track_id"] for r in pool], conn, os_client)
    families = assign_families(genre_options(pool, conn), tracks)
    rows = prefer_common_families(pool, families, MAX_TRACKS)
    return flow_order(rows, conn, os_client, families=families, tracks=tracks,
                      acts=acts, stale_ids=stale_ids)


# -- saving --------------------------------------------------------------------

def slug(name: str) -> str:
    return "smart-" + (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "list")


def save_local(rule: Rule, rows: list[dict[str, Any]], conn, url: str = "") -> str:
    """Store (replace) the saved playlist; keeps a known YouTube Music link."""
    from . import localcache
    from .ytmusic_playlist import extract_video_id

    pid = slug(rule.name)
    if not url:
        old = conn.execute("SELECT url FROM saved_playlists WHERE playlist_id = %s",
                           (pid,)).fetchone()
        url = (old or {}).get("url") or ""
    localcache.delete_saved_playlist(pid, conn=conn)
    localcache.save_playlist(
        pid, rule.name, search_query=rule.description, url=url, source_kind=SOURCE_KIND,
        tracks=[{"position": i, "track_id": r["track_id"], "artist": r["artist"],
                 "title": r["title"], "url": r["url"],
                 "video_id": extract_video_id(r["url"]) or ""}
                for i, r in enumerate(rows, start=1)],
        conn=conn)
    return pid


def push_ytmusic(rule: Rule, rows: list[dict[str, Any]], yt, existing: dict[str, str]) -> str:
    """Make the YouTube Music playlist of this name hold these songs in order.

    Reuses a playlist with the same title (its id and links survive):
    keeps the longest leading subsequence already in ``rows`` order, removes
    stale or rotated-down items before it, and appends the rest in ``rows``
    order so a rotated opener actually moves to the top on YouTube Music too.
    """
    from .ytmusic_playlist import extract_video_id

    vids = [v for v in (extract_video_id(r["url"]) for r in rows) if v]
    pid = existing.get(rule.name.strip().lower())
    if not pid:
        return yt.create_playlist(rule.name, description=rule.description, video_ids=vids)
    current = (yt.get_playlist(pid, limit=500) or {}).get("tracks") or []
    wanted = set(vids)
    # Match the longest prefix of `vids` that already appears in order inside `current`.
    vid_idx = 0
    kept_set_ids: set[str] = set()
    kept_vids: set[str] = set()
    for t in current:
        v = t.get("videoId")
        sv = t.get("setVideoId")
        if not v or not sv:
            continue
        if vid_idx < len(vids) and v == vids[vid_idx]:
            kept_set_ids.add(sv)
            kept_vids.add(v)
            vid_idx += 1
    stale = [{"videoId": t["videoId"], "setVideoId": t["setVideoId"]} for t in current
             if t.get("videoId") and t.get("setVideoId")
             and (t["videoId"] not in wanted or t["setVideoId"] not in kept_set_ids)]
    if stale:
        yt.remove_playlist_items(pid, stale)
    missing = [v for v in vids if v not in kept_vids]
    if missing:
        yt.add_playlist_items(pid, missing)
    return pid


def refresh(*, push: bool = False, only: Optional[list[str]] = None,
            dry_run: bool = False, conn=None, os_client=None, yt=None) -> list[dict[str, Any]]:
    """Rebuild the rules' playlists. Returns one summary per rule."""
    from . import localcache
    from .osclient import client

    own = conn is None
    c = conn or localcache.connect()
    osc = os_client if os_client is not None else client()
    wanted = {n.strip().lower() for n in only} if only else None
    existing: dict[str, str] = {}
    if push and not dry_run:
        from .ytmusic_client import YTMusicClient

        yt = yt or YTMusicClient()
        existing = {str(p.get("title", "")).strip().lower(): p["playlistId"]
                    for p in yt.get_library_playlists(limit=500) if p.get("playlistId")}
    out = []
    try:
        for rule in RULES:
            if wanted and rule.name.lower() not in wanted:
                continue
            rows = build(rule, c, osc)
            summary = {"name": rule.name, "count": len(rows),
                       "first": [f"{r['artist']} - {r['title']}" for r in rows[:3]]}
            if not dry_run and rows:
                url = ""
                if push:
                    try:
                        ytid = push_ytmusic(rule, rows, yt, existing)
                        url = f"https://music.youtube.com/playlist?list={ytid}"
                        summary["ytmusic"] = ytid
                    except Exception as exc:
                        log.warning("smart playlists: YouTube Music sync failed for %s: %s",
                                    rule.name, exc)
                        summary["ytmusic_error"] = str(exc)
                summary["playlist_id"] = save_local(rule, rows, c, url=url)
            out.append(summary)
    finally:
        if own:
            c.close()
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--push", action="store_true", help="also sync YouTube Music playlists")
    ap.add_argument("--only", action="append", help="rebuild just this playlist (repeatable)")
    ap.add_argument("--dry-run", action="store_true", help="show what would be built")
    ap.add_argument("--list", action="store_true", help="list the rules and exit")
    args = ap.parse_args(argv)
    if args.list:
        for rule in RULES:
            print(f"{rule.name:32} {rule.description}")
        return 0
    for s in refresh(push=args.push, only=args.only, dry_run=args.dry_run):
        yt = f"  yt:{s['ytmusic']}" if s.get("ytmusic") else (
            f"  yt FAILED: {s['ytmusic_error'][:60]}" if s.get("ytmusic_error") else "")
        print(f"{s['name']:32} {s['count']:>3} songs{yt}  {' | '.join(s['first'])[:90]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
