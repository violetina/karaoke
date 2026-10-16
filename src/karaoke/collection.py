"""The graph's collector: songs gathered while exploring, saved or played.

Nothing here is new machinery. Saving writes an ordinary saved playlist --
which the graph's own "Load Playlist" menu then offers, so a collection can be
explored as a graph. Playing takes the TUI's own route: a temporary YouTube
Music playlist opened in the kiosk, which plays the songs in order by itself,
and the same rows stored as the active queue so the stage views and the TUI
show what is coming.

One rule is inherited on purpose (ytmusic_playlist.
queue_rows_to_ytmusic_candidates): only songs with a known YouTube source go
into the YouTube Music queue. Matching the rest by title could add the wrong
recording, so they are reported, not guessed.
"""
from __future__ import annotations

import re
import time
from typing import Any, Optional

from . import localcache
from .logger import log

#: The YouTube Music queue is capped like the TUI's, for the same reason:
#: the API slows and fails on longer playlists.
MAX_QUEUE = 50

COLLECTION_QUEUE_ID = "graph-collection"


def rows_for(track_ids: list[int], conn) -> list[dict[str, Any]]:
    """Queue rows for tracks, in the given order, with their best YouTube URL.

    YouTube Music sources first (usually the audio release), then YouTube.
    Unknown ids are dropped; a track without a YouTube source keeps an empty
    URL, so callers can say which songs cannot be queued.
    """
    if not track_ids:
        return []
    rows = conn.execute(
        """
        SELECT t.track_id, t.artist, t.title, a.bpm, a.detected_key, a.energy,
               (SELECT s.url FROM sources s
                 WHERE s.track_id = t.track_id AND s.kind IN ('youtube_music', 'youtube')
                 ORDER BY CASE s.kind WHEN 'youtube_music' THEN 0 ELSE 1 END
                 LIMIT 1) AS url,
               (SELECT s.kind FROM sources s
                 WHERE s.track_id = t.track_id AND s.kind IN ('youtube_music', 'youtube')
                 ORDER BY CASE s.kind WHEN 'youtube_music' THEN 0 ELSE 1 END
                 LIMIT 1) AS kind
        FROM tracks t LEFT JOIN track_analysis a ON a.track_id = t.track_id
        WHERE t.track_id = ANY(%s)
        """, (list(track_ids),)).fetchall()
    by_id = {int(r["track_id"]): r for r in rows}
    out = []
    for tid in track_ids:
        r = by_id.get(int(tid))
        if r is None:
            continue
        out.append({
            "track_id": int(tid), "artist": r["artist"] or "", "title": r["title"] or "",
            "url": r["url"] or "", "kind": r["kind"] or "",
            "bpm": r["bpm"], "key": r["detected_key"], "energy": r["energy"],
        })
    return out


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "collection"


def save(name: str, track_ids: list[int], conn) -> dict[str, Any]:
    """Store the collection as a saved playlist; returns its id and count."""
    from .ytmusic_playlist import extract_video_id

    name = (name or "").strip() or time.strftime("Graph collection %Y-%m-%d %H:%M")
    rows = rows_for(track_ids, conn)
    playlist_id = f"graph-{_slug(name)}"
    localcache.save_playlist(
        playlist_id, name, source_kind="graph",
        tracks=[{"position": i, "track_id": r["track_id"], "artist": r["artist"],
                 "title": r["title"], "url": r["url"],
                 "video_id": extract_video_id(r["url"]) or ""}
                for i, r in enumerate(rows, start=1)],
        conn=conn)
    return {"playlist_id": playlist_id, "name": name, "count": len(rows)}


def play(name: str, track_ids: list[int], conn) -> dict[str, Any]:
    """Play the collection in order: YouTube Music queue + active queue."""
    from . import ytmusic_playlist
    from .browse import open_song_url

    name = (name or "").strip() or "Karaoke: Graph collection"
    rows = rows_for(track_ids, conn)
    playable = [r for r in rows if ytmusic_playlist.extract_video_id(r["url"])][:MAX_QUEUE]
    skipped = [f"{r['artist']} - {r['title']}" for r in rows
               if not ytmusic_playlist.extract_video_id(r["url"])]
    if not playable:
        return {"status": "nothing_playable", "skipped": skipped}

    # The active queue first: the stage's "up next" and the TUI follow it even
    # if the YouTube Music side fails below.
    localcache.save_active_queue(COLLECTION_QUEUE_ID, name, playable, current_index=0, conn=conn)
    localcache.record_queue_event("loaded", track_id=playable[0]["track_id"],
                                  artist=playable[0]["artist"], title=playable[0]["title"],
                                  metadata={"source": "graph-collector", "count": len(playable)},
                                  conn=conn)
    first = ytmusic_playlist.extract_video_id(playable[0]["url"])
    try:
        result = ytmusic_playlist.create_temp_queue_playlist(
            playable, name=name, max_tracks=MAX_QUEUE, conn=conn)
        url = f"https://music.youtube.com/watch?v={first}&list={result.playlist_id}"
        open_song_url(url, "youtube_music_playlist", prefer_audio=True)
        return {"status": "playing", "mode": "ytmusic_queue", "playlist_id": result.playlist_id,
                "count": len(playable), "skipped": skipped}
    except Exception as exc:
        # No YouTube Music account (or it is down): still start the first song;
        # the local queue holds the rest for the TUI's Follow Queue.
        log.warning("collector: YouTube Music queue failed (%s); playing the first song", exc)
        open_song_url(playable[0]["url"], playable[0]["kind"] or "youtube",
                      artist=playable[0]["artist"], title=playable[0]["title"],
                      prefer_audio=True)
        return {"status": "playing", "mode": "first_song_only", "count": len(playable),
                "skipped": skipped, "message": f"YouTube Music queue unavailable: {exc}"}
