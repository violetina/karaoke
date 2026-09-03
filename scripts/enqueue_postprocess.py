"""Scan SQLite for tracks missing post-processing and enqueue them all.

Fills the RabbitMQ post-processing queue from the current library state. Safe to
re-run at any time — the worker re-checks `needs_postprocessing` per task and
skips tracks that are already complete. Use this to (re)build the queue after a
broker reset, since the queue is intentionally non-persistent.
"""
from __future__ import annotations

from karaoke import localcache
from karaoke.postprocess_queue import needs_postprocessing, publish_postprocess_task


def main() -> None:
    conn = localcache.connect()
    cur = conn.cursor()
    cur.execute("SELECT track_id, artist, title FROM tracks ORDER BY artist, title")
    tracks = cur.fetchall()

    enqueued = 0
    skipped = 0
    failed = 0
    for t in tracks:
        track_id, artist, title = t["track_id"], t["artist"], t["title"]
        if not (artist or title):
            continue
        pending = needs_postprocessing(track_id, conn)
        if not pending:
            skipped += 1
            continue
        # Pick a source URL to attach so the worker can download/analyze.
        row = cur.execute(
            """
            SELECT url FROM sources
            WHERE track_id = ? AND kind IN ('youtube', 'youtube_music')
            ORDER BY CASE WHEN kind = 'youtube_music' THEN 0 ELSE 1 END
            LIMIT 1
            """,
            (track_id,),
        ).fetchone()
        url = row["url"] if row else ""
        if publish_postprocess_task(artist, title, url):
            print(f"enqueued: {artist} - {title}  [{', '.join(pending)}]")
            enqueued += 1
        else:
            failed += 1

    conn.close()
    print(f"\nDone. Enqueued {enqueued}; skipped {skipped} (already complete); "
          f"{failed} failed to publish (broker unreachable?).")
    if failed:
        print("Tip: start the broker port-forward first:  make mq-port-forward")


if __name__ == "__main__":
    main()
