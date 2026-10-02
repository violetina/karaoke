"""Fully post-process every track that belongs to a saved playlist.

The library at large is built from disk, so the playback path deliberately
never downloads audio just to analyse harmony -- folder_scan covers those files
without the network. Playlist tracks are the exception: a bounded set the user
has explicitly chosen, worth fetching audio for so they come out fully
processed (key/BPM, harmony and chords, lyric sync, word timings).

Usage:

    python scripts/enqueue_playlists.py                 # every saved playlist
    python scripts/enqueue_playlists.py --dry-run       # show what would queue
    python scripts/enqueue_playlists.py --playlist PLxx # one playlist only
    python scripts/enqueue_playlists.py --limit 200     # cap the batch
"""
from __future__ import annotations

import argparse

from karaoke import localcache
from karaoke.postprocess_queue import needs_postprocessing, publish_postprocess_task


def playlist_tracks(conn, playlist_id: str | None = None) -> list[dict]:
    """Return distinct tracks that belong to a saved playlist, with a source URL."""
    cur = conn.cursor()
    where = "WHERE spt.track_id IS NOT NULL"
    params: tuple = ()
    if playlist_id:
        where += " AND spt.playlist_id = %s"
        params = (playlist_id,)
    cur.execute(
        f"""
        SELECT DISTINCT ON (t.track_id)
               t.track_id, t.artist, t.title,
               COALESCE(s.url, spt.url, '') AS url
        FROM saved_playlist_tracks spt
        JOIN tracks t ON t.track_id = spt.track_id
        LEFT JOIN sources s ON s.source_id = (
            SELECT source_id FROM sources
            WHERE track_id = t.track_id AND kind IN ('youtube', 'youtube_music')
            ORDER BY CASE WHEN kind = 'youtube_music' THEN 0 ELSE 1 END, source_id
            LIMIT 1
        )
        {where}
        ORDER BY t.track_id
        """,
        params,
    )
    return [dict(r) for r in cur.fetchall()]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--playlist", help="Only this saved playlist id")
    ap.add_argument("--limit", type=int, help="Queue at most N tracks")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would be queued without publishing")
    args = ap.parse_args()

    conn = localcache.connect()
    rows = playlist_tracks(conn, args.playlist)
    print(f"{len(rows)} distinct tracks across saved playlists"
          f"{' ' + args.playlist if args.playlist else ''}\n")

    queued = complete = failed = 0
    for row in rows:
        if args.limit is not None and queued >= args.limit:
            print(f"\nstopping at --limit {args.limit}; "
                  f"{len(rows) - (queued + complete)} tracks left unqueued")
            break
        # Playlists are meant to come out fully processed, so the search-derived
        # steps (vectors, chords) count here as much as the database ones.
        pending = needs_postprocessing(row["track_id"], conn)
        if not pending:
            complete += 1
            continue
        if args.dry_run:
            print(f"would queue: {row['artist']} - {row['title']}  [{', '.join(pending)}]")
            queued += 1
            continue
        if publish_postprocess_task(row["artist"], row["title"], row["url"],
                                    include_timings=True, full=True):
            print(f"queued: {row['artist']} - {row['title']}  [{', '.join(pending)}]")
            queued += 1
        else:
            failed += 1

    conn.close()
    verb = "Would queue" if args.dry_run else "Queued"
    print(f"\n{verb} {queued}; {complete} already complete; {failed} failed to publish.")


if __name__ == "__main__":
    main()
