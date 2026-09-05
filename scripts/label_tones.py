"""Read the attitude of every lyric worth reading.

The second axis beside genre. Genre lives in the sound; tone lives in the
words, and together they say what neither says alone -- "cynical pop" is a
description, and a track can sound cheerful while its words are bleak.

Skips transcribed lyrics: a Whisper guess has no attitude worth reading, only
the model's. Skips short ones: a chorus stub is not enough text to judge.
"""
from __future__ import annotations

import argparse
from typing import Optional

from karaoke import localcache, tone
from karaoke.logger import log


def run(*, overwrite: bool = False, dry_run: bool = False,
        limit: Optional[int] = None) -> int:
    conn = localcache.connect()
    localcache.ensure_tone_table(conn)

    print(f"embedding {len(tone.TONES)} tone label(s)")
    labels = tone.label_vectors()
    if not labels:
        print("no labels could be embedded")
        return 1

    rows = conn.execute(
        "SELECT t.track_id, t.artist, t.title, l.plain_lyrics, l.source"
        "  FROM tracks t"
        "  JOIN lyrics l ON l.track_id = t.track_id AND l.kind = 'approved'"
        " WHERE length(COALESCE(l.plain_lyrics, '')) >= ?"
        " ORDER BY t.artist, t.title", (tone.MIN_CHARS,)).fetchall()
    print(f"{len(rows)} track(s) with lyrics long enough to read\n")

    read = skipped = refused = unclear = 0
    for row in rows:
        if limit and read >= limit:
            break
        track_id = int(row["track_id"])
        if not overwrite and localcache.tone_for(track_id, conn) is not None:
            skipped += 1
            continue

        verdict = tone.classify_lyrics(row["plain_lyrics"], row["source"] or "",
                                       labels)
        name = f"{(row['artist'] or '')[:20]:22} {(row['title'] or '')[:26]:28}"
        if verdict is None:
            refused += 1
            if not dry_run:
                localcache.clear_tone(track_id, conn)
            continue

        mark = "" if verdict.clear else f"  ~{verdict.runner_up.split()[0]}"
        if not verdict.clear:
            unclear += 1
        print(f"  {name} {verdict.short:11} {verdict.score:+.3f}{mark}")
        if not dry_run:
            localcache.record_tone(track_id, verdict, conn)
        read += 1

    print(f"\n{read} read ({unclear} too close to call), "
          f"{refused} not readable, {skipped} already done")
    if not dry_run:
        print("\nby tone:")
        for r in localcache.tone_counts(conn):
            print(f"   {r['n']:4}  {r['tone']:26} mean {r['mean_score']:+.3f}")
    conn.close()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args(argv)
    log.info("reading lyric tones")
    return run(overwrite=args.overwrite, dry_run=args.dry_run, limit=args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
