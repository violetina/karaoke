"""Queue a bounded batch of tracks missing current-version mood and/or chords.

``enqueue_postprocess.py`` deliberately skips the OpenSearch/derived steps so a
library-wide sweep cannot download the whole library. This is the explicit,
bounded version: it picks tracks whose ``track_moods``/``track_chords`` row is
missing or older than ``MOOD_VERSION``/``CHORDS_VERSION``, keeps those with a
downloadable or local source, and publishes one task per track running only the
missing steps (``full=True`` lets the chain fetch audio when there is no file).

Rows are interleaved round-robin by artist so a batch spreads across the
library instead of draining one big artist, and ``--exclude-top N`` leaves out
the N artists with the most tracks. Otherwise batches follow track_id order. Tracks already queued but not finished
still count as missing, so pass ``--offset`` to move past an in-flight batch.

Run:  ``python scripts/enqueue_mood_chords.py --limit 1000 [--offset N] [--dry-run]``
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from karaoke import localcache  # noqa: E402
from karaoke.chords import CHORDS_VERSION  # noqa: E402
from karaoke.mood_timeline import MOOD_VERSION  # noqa: E402
from karaoke.postprocess_queue import (  # noqa: E402
    has_downloadable_source,
    publish_postprocess_task,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--exclude-top", type=int, default=0,
                    help="skip the N artists with the most tracks in the library")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with localcache.connect() as conn:
        # Group by primary MusicBrainz artist (credit position 0); tracks with no
        # link fall back to their artist string, so collab credits and spelling
        # variants of one act count once.
        allrows = conn.execute(
            """
            SELECT t.track_id, t.artist, t.title,
                   COALESCE(ta.mbid, 'str:' || lower(t.artist)) AS grp,
                   COALESCE(a.name, t.artist) AS grp_name,
                   (m.track_id IS NULL OR m.mood_version < %s) AS need_mood,
                   (c.track_id IS NULL OR c.chords_version < %s) AS need_chords
            FROM tracks t
            LEFT JOIN track_artists ta ON ta.track_id = t.track_id AND ta.position = 0
            LEFT JOIN artists a ON a.mbid = ta.mbid
            LEFT JOIN track_moods m ON m.track_id = t.track_id
            LEFT JOIN track_chords c ON c.track_id = t.track_id
            WHERE length(trim(t.artist)) > 0
            ORDER BY t.track_id
            """,
            (MOOD_VERSION, CHORDS_VERSION),
        ).fetchall()
        from collections import Counter, defaultdict
        # A genre needs a CLAP vector. Ask the index, not track_genre: a track
        # that matched no label has no genre row but is already embedded, and
        # re-running it would only reproduce the same non-answer.
        from opensearchpy import helpers
        from karaoke import clap_vector
        from karaoke.osclient import client as os_client
        embedded = {int(h["_source"]["track_id"]) for h in helpers.scan(
            os_client(), index=clap_vector.CLAP_INDEX, _source=["track_id"],
            query={"query": {"exists": {"field": "clap_vector"}}})}
        allrows = [{**r, "need_clap": r["track_id"] not in embedded} for r in allrows]
        rows = [r for r in allrows if r["need_mood"] or r["need_chords"] or r["need_clap"]]
        if args.exclude_top:
            sizes = Counter(r["grp"] for r in allrows)
            names = {r["grp"]: r["grp_name"] for r in allrows}
            top = {g for g, _ in sorted(sizes.items(), key=lambda x: (-x[1], x[0]))[:args.exclude_top]}
            print("excluding:", ", ".join(f"{names[g]} ({sizes[g]})" for g in top))
            rows = [r for r in rows if r["grp"] not in top]
        # Round-robin across artists (first track of each, then second, ...).
        by_artist = defaultdict(list)
        for r in rows:
            by_artist[r["grp"]].append(r)
        rows = [r for _, r in sorted(
            ((i, r) for lst in by_artist.values() for i, r in enumerate(lst)),
            key=lambda x: (x[0], x[1]["track_id"]))]
        print(f"{len(rows)} tracks missing current mood, chords and/or a CLAP vector (genre)")

        queued = no_source = failed = 0
        artists_hit: set = set()
        seen = 0
        for r in rows:
            if queued >= args.limit:
                break
            if not has_downloadable_source(r["track_id"], conn):
                no_source += 1
                continue
            seen += 1
            if seen <= args.offset:
                continue
            steps = [s for s, need in (("chords", r["need_chords"]),
                                       ("mood", r["need_mood"]),
                                       ("clap", r["need_clap"])) if need]
            url_row = conn.execute(
                "SELECT url FROM sources WHERE track_id = %s AND kind IN "
                "('youtube','youtube_music') ORDER BY (kind='youtube_music') DESC LIMIT 1",
                (r["track_id"],),
            ).fetchone()
            artists_hit.add(r["grp"])
            if args.dry_run:
                queued += 1
                continue
            ok = publish_postprocess_task(
                r["artist"], r["title"], url_row["url"] if url_row else "",
                full=True, steps=steps)
            # Returns a task id (truthy) on success, False/None on failure.
            if ok:
                queued += 1
            else:
                failed += 1
        verb = "Would queue" if args.dry_run else "Queued"
        print(f"{verb} {queued}; skipped {no_source} with no downloadable source; "
              f"{failed} failed to publish; {len(artists_hit)} distinct primary artists.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
