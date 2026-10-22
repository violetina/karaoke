"""Playlists that rebuild themselves from what the library knows.

Two kinds, all named so they can be told apart from hand-made lists:

* ``Genre: <name>`` -- the same match as the TUI's ``H`` genre filter: an
  artist's genres (broad or specific) when the artist is classified, the
  track's own CLAP genre otherwise.
* ``Smart: <name>`` -- rules over the analysis: the mood timeline's feelings,
  swings and sound-vs-words contrast, and the CLAP timbre of a playlist you
  already have ("More like Komijn 2025").

Every list keeps to songs that can be played (a YouTube source), favours
songs with synced lyrics -- this is a karaoke library -- allows two songs per
act, and is then put in *flow* order: each next song is the one that follows
best under the DJ booth's own scoring (sound, mood hand-off, key, tempo), so
a list plays like a set rather than a sorted table.

Rebuilding replaces each list's contents in place: the saved playlist row and,
with ``--push``, the YouTube Music playlist of the same name (its id, and so
any kiosk link to it, survive). Nothing here deletes a playlist.

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
MAX_TRACKS = 70
PER_ACT = 2
SOURCE_KIND = "smart"

GENRES = ("blues", "jazz", "folk", "soul", "reggae", "country", "latin",
          "ambient", "electronic", "hip hop", "punk", "metal")


@dataclass(frozen=True)
class Rule:
    name: str
    description: str
    #: (conn, os_client) -> candidate track ids, best first.
    select: Callable[[Any, Any], list[int]]


# -- selectors ---------------------------------------------------------------

#: Lyrics first: synced, then plain, then none. Most played next.
_KARAOKE_ORDER = """
    CASE WHEN EXISTS (SELECT 1 FROM lyrics l WHERE l.track_id = t.track_id
                       AND COALESCE(l.synced_lyrics, '') != '') THEN 0
         WHEN EXISTS (SELECT 1 FROM lyrics l WHERE l.track_id = t.track_id
                       AND COALESCE(l.plain_lyrics, '') != '') THEN 1
         ELSE 2 END,
    t.play_count DESC NULLS LAST, t.track_id"""


def genre_ids(genre: str, conn) -> list[int]:
    """Tracks the TUI's ``H`` filter shows for this genre, best first.

    The filter's match, ranked by how much the song *belongs* to the genre:
    every artist genre has weight 1.0, and an act tagged with five broad
    genres (Blondie: rock, pop, punk, soul via disco, electronic) would
    otherwise open "Soul". So: dedicated acts (at most two broad genres)
    first, then broad ones, then tracks known only by the CLAP guess on the
    song itself -- the least reliable ("shoegaze" Billie Holiday).
    """
    rows = conn.execute(f"""
        WITH breadth AS (
            SELECT artist_normalized, count(DISTINCT broad_genre) AS n
            FROM artist_genres GROUP BY artist_normalized)
        SELECT t.track_id FROM tracks t
        LEFT JOIN track_genre g ON g.track_id = t.track_id
        LEFT JOIN breadth b ON b.artist_normalized = lower(trim(t.artist))
        WHERE EXISTS (SELECT 1 FROM artist_genres ag
                       WHERE (lower(trim(ag.broad_genre)) = lower(trim(%(g)s))
                              OR lower(trim(ag.genre)) = lower(trim(%(g)s)))
                         AND ag.artist_normalized = lower(trim(t.artist)))
           OR (b.n IS NULL
               AND lower(trim(COALESCE(g.genre, ''))) = lower(trim(%(g)s)))
        ORDER BY CASE WHEN b.n IS NULL THEN 2 WHEN b.n <= 2 THEN 0 ELSE 1 END,
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


def _genre_rule(genre: str) -> Rule:
    return Rule(f"Genre: {genre.title()}",
                f"Like the TUI's H filter on '{genre}', lyrics first, in flow order",
                lambda conn, osc, g=genre: genre_ids(g, conn))


RULES: tuple[Rule, ...] = tuple(_genre_rule(g) for g in GENRES) + (
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


def cap_per_act(rows: list[dict[str, Any]], per_act: int = PER_ACT,
                limit: int = MAX_TRACKS) -> list[dict[str, Any]]:
    """At most ``per_act`` songs per act, one version of each song, up to ``limit``."""
    from .dj_suggest import artist_key

    seen: dict[str, int] = {}
    # One version of a song per list, whoever plays it: "Rory Gallagher &
    # Muddy Waters" is another act but the same song, and flow order would
    # put the two side by side because they sound alike.
    songs: set[str] = set()
    out = []
    for r in rows:
        key = artist_key(r["artist"])
        song = song_key(r["title"])
        if seen.get(key, 0) >= per_act or song in songs:
            continue
        songs.add(song)
        seen[key] = seen.get(key, 0) + 1
        out.append(r)
        if len(out) >= limit:
            break
    return out


def flow_order(rows: list[dict[str, Any]], conn, os_client) -> list[dict[str, Any]]:
    """Sequence the rows as a set: each next song follows the last best.

    Starts from the first row (the rule's best pick) and greedily takes the
    candidate the booth's "flow" scoring likes most after the current song.
    Avoids two songs by one act back to back when anything else fits.
    """
    from . import dj_suggest

    if len(rows) < 3:
        return rows
    tracks = dj_suggest._load([r["track_id"] for r in rows], conn, os_client)
    by_id = {r["track_id"]: r for r in rows}
    order = [rows[0]["track_id"]]
    left = [r["track_id"] for r in rows[1:]]
    while left:
        cur = tracks.get(order[-1])
        cur_act = dj_suggest.artist_key(by_id[order[-1]]["artist"])

        def fit(tid: int) -> float:
            cand = tracks.get(tid)
            s = dj_suggest.score(cur, cand, "flow") if cur and cand else None
            same_act = dj_suggest.artist_key(by_id[tid]["artist"]) == cur_act
            return (s if s is not None else -1.0) - (1.0 if same_act else 0.0)

        nxt = max(left, key=fit)
        order.append(nxt)
        left.remove(nxt)
    return [by_id[t] for t in order]


def build(rule: Rule, conn, os_client) -> list[dict[str, Any]]:
    """The rule's playlist rows, ready to save or push."""
    ids = rule.select(conn, os_client)
    rows = cap_per_act(playable(ids, conn))
    return flow_order(rows, conn, os_client)


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
    """Make the YouTube Music playlist of this name hold exactly these songs.

    Reuses a playlist with the same title (its id and links survive):
    removes what no longer belongs, adds what is new. Creates it otherwise.
    """
    from .ytmusic_playlist import extract_video_id

    vids = [v for v in (extract_video_id(r["url"]) for r in rows) if v]
    pid = existing.get(rule.name.strip().lower())
    if not pid:
        return yt.create_playlist(rule.name, description=rule.description, video_ids=vids)
    current = (yt.get_playlist(pid, limit=500) or {}).get("tracks") or []
    wanted = set(vids)
    stale = [{"videoId": t["videoId"], "setVideoId": t["setVideoId"]} for t in current
             if t.get("videoId") and t.get("setVideoId") and t["videoId"] not in wanted]
    have = {t.get("videoId") for t in current} - {s["videoId"] for s in stale}
    if stale:
        yt.remove_playlist_items(pid, stale)
    missing = [v for v in vids if v not in have]
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
