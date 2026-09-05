"""Repair track rows that recorded a channel name instead of an artist.

Rows learned from a browser tab before the metadata cleaners existed kept what
the uploader called their channel, and often repeated the artist inside the
title as well::

    Queen Official  |  Queen - I Want to Break Free
    ModjoOfficial   |  Lady
    Samuel Jack     |  Samuel Jack 'California Sun'

Both halves are fixed here with the same helpers that clean new rows, so this
is a backfill of behaviour the pipeline already has rather than a second,
divergent set of rules:

- the artist goes through :func:`karaoke.lyrics.clean_artist`, which strips
  "- Topic" and the "Official"/"VEVO" channel suffixes;
- a title that merely repeats the artist has that prefix removed, and stray
  wrapping quotes with it.

Renaming often reveals a duplicate — ``Queen Official | Queen - I Want to Break
Free`` becomes ``Queen | I Want to Break Free``, which the library already has
under its proper name. That is deliberate: run ``make db-cleanup`` afterwards
and the deduplicator merges them, sources and all.

Nothing is deleted here and no row is merged; only names change.

Run:  ``python scripts/fix_channel_names.py``
      ``python scripts/fix_channel_names.py --apply``
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from karaoke import localcache                    # noqa: E402
from karaoke.lyrics import clean_artist           # noqa: E402

# Quote characters uploaders wrap titles in: 'California Sun', "Army Ants".
_QUOTES = "\"'“”‘’"


# Words that mean the artist name is part of a *credit*, not a repetition:
# "Ren x Chinchilla - How To Be Me" names two acts, and removing the first
# leaves "x Chinchilla - How To Be Me", which is worse than doing nothing.
_COLLAB = re.compile(r"^(?:x|&|and|feat\.?|ft\.?|vs\.?|with|w/)\b",
                     re.IGNORECASE)


def strip_artist_prefix(artist: str, title: str) -> str:
    """Remove a leading repeat of the artist from a title.

    "Queen - I Want to Break Free" under artist "Queen" is the same information
    twice, and that duplication is what stops the row matching the same song
    stored under its proper name.

    A **separator is required**. Without one, "Soulfly XIII" under artist
    "Soulfly" looks like a repetition but is the actual title -- the band
    numbers its instrumentals that way -- and stripping would rename the track
    to "XIII".
    """
    cleaned = (title or "").strip()
    name = (artist or "").strip()
    if not name or not cleaned:
        return cleaned

    match = re.match(rf"^{re.escape(name)}\s*[-–—:]\s+(?P<rest>.+)$",
                     cleaned, flags=re.IGNORECASE)
    if match:
        rest = match.group("rest").strip()
        return cleaned if _COLLAB.match(rest) else (rest or cleaned)

    # The other shape uploaders use: the artist followed by a quoted title.
    quoted = re.match(rf"^{re.escape(name)}\s+[{_QUOTES}](?P<rest>.+)[{_QUOTES}]$",
                      cleaned, flags=re.IGNORECASE)
    if quoted:
        return quoted.group("rest").strip() or cleaned
    return cleaned


def strip_quotes(title: str) -> str:
    """Unwrap a title that is entirely inside quotes."""
    out = (title or "").strip()
    if len(out) > 2 and out[0] in _QUOTES and out[-1] in _QUOTES:
        return out[1:-1].strip() or out
    return out


def proposed(row) -> tuple[str, str]:
    """The corrected (artist, title) for a row."""
    artist = clean_artist(row["artist"] or "")
    title = strip_quotes(strip_artist_prefix(artist, row["title"] or ""))
    # Also handle the title repeating the *original* channel name.
    title = strip_quotes(strip_artist_prefix(row["artist"] or "", title))
    return (artist or (row["artist"] or ""), title or (row["title"] or ""))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="write the corrections (default is a dry run)")
    args = ap.parse_args()

    with localcache.connect() as conn:
        rows = conn.execute(
            "SELECT track_id, artist, title FROM tracks ORDER BY track_id"
        ).fetchall()

        changed = 0
        for row in rows:
            artist, title = proposed(row)
            if artist == (row["artist"] or "") and title == (row["title"] or ""):
                continue
            print(f"  {row['track_id']:>4}  {row['artist']!r} - {row['title']!r}")
            print(f"        -> {artist!r} - {title!r}")
            if args.apply:
                conn.execute(
                    "UPDATE tracks SET artist = ?, title = ? WHERE track_id = ?",
                    (artist, title, row["track_id"]),
                )
            changed += 1

        if args.apply:
            conn.commit()
        print(f"\n{changed} row(s) corrected"
              f"{'' if args.apply else '  (dry run — nothing written)'}")
        if changed and args.apply:
            print("Some may now duplicate an existing row; run: make db-cleanup")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
