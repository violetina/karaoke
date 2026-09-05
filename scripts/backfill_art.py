"""Fill in cover art for tracks that have never been played since it was kept.

Nothing local can supply it, which is worth stating because both obvious
routes look like they should work:

- **The art cache cannot be walked backwards.** A remote cover is stored under
  ``sha256(artUrl)[:32]`` and the URL is kept nowhere, so those 48 files are
  one-way hashes with no route back to a track.
- **The cached media has no pictures in it.** Measured, not assumed: 0 of 120
  cached YouTube files carry a video stream. They are audio-only downloads
  (opus, m4a) with no attached image, so there is no frame to sample and no
  embedded cover to extract.

What does work is the thumbnail, which YouTube addresses by video id --
``i.ytimg.com/vi/<id>/...`` -- and a track's source URL yields that id. No API
key, no quota, and it is the same image the player would have published had
the track been playing.

Sizes are tried largest first: ``maxresdefault`` does not exist for every
video, and 404 is how that is discovered.

**Frames are chosen, not taken.** A thumbnail is a still, but the same
selection guards against a black or flat image, which is worth keeping for the
day this points at something with more than one frame.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from statistics import pvariance
from typing import Optional

from karaoke import cover_store, coverart, localcache
from karaoke.config import settings
from karaoke.logger import log

# Where to look for a frame, as a fraction of the running time. A quarter in is
# usually past the intro; the later offsets are fallbacks, and 0 is last
# because it is the least likely to be interesting and the most likely to be
# black -- except for an embedded cover image, where it is the only frame there
# is, which is why it is present at all.
SEEK_FRACTIONS = (0.25, 0.5, 0.1, 0.0)

# A frame darker than this is a fade or a black title card.
MIN_BRIGHTNESS = 24.0

# Flatter than this and the frame carries no image: a solid colour, or the
# letterboxing either side of one.
MIN_VARIANCE = 120.0


def frame_quality(pixels) -> tuple[float, float]:
    """Mean brightness and colour variance of a sampled frame."""
    if not pixels:
        return (0.0, 0.0)
    values = [sum(cell) / 3.0 for row in pixels for cell in row]
    if not values:
        return (0.0, 0.0)
    mean = sum(values) / len(values)
    return (mean, pvariance(values) if len(values) > 1 else 0.0)


def best_frame(path: Path, *, cols: int, rows: int):
    """The most informative frame from a media file, or None.

    Returns ``(pixels, seek, brightness, variance)``.
    """
    length = coverart.duration(path) or 0.0
    tried: list[tuple[float, float, float, list]] = []
    for fraction in SEEK_FRACTIONS:
        seek = length * fraction if length > 0 else 0.0
        pixels = coverart.sample(path, cols, rows, seek=seek)
        if not pixels:
            continue
        brightness, variance = frame_quality(pixels)
        if brightness >= MIN_BRIGHTNESS and variance >= MIN_VARIANCE:
            return (pixels, seek, brightness, variance)
        tried.append((brightness, variance, seek, pixels))
    if not tried:
        return None
    # Nothing cleared the bar; keep the least bad rather than nothing, since a
    # dim cover still beats an empty panel.
    brightness, variance, seek, pixels = max(tried, key=lambda t: (t[0], t[1]))
    return (pixels, seek, brightness, variance)


# Largest first. maxresdefault is absent for plenty of videos and the CDN
# answers 404, which is the only way to find out; hqdefault always exists.
THUMBNAIL_SIZES = ("maxresdefault", "sddefault", "hqdefault")


def thumbnail_source(video_id: str) -> Optional[tuple[Path, str]]:
    """Fetch a video's thumbnail into the art cache, best size first."""
    for size in THUMBNAIL_SIZES:
        url = f"https://i.ytimg.com/vi/{video_id}/{size}.jpg"
        path = coverart.fetch_remote_art(url)
        if path is not None:
            return (path, url)
    return None


def cached_media() -> dict[str, Path]:
    """Cached YouTube media, by video id.

    Kept for the local-first check even though none of it currently carries a
    picture: a future download that keeps video would be usable without a
    network round trip, and the check costs nothing.
    """
    directory = Path(settings.youtube_dir)
    if not directory.is_dir():
        return {}
    found: dict[str, Path] = {}
    for path in sorted(directory.glob("*")):
        if path.is_file() and path.suffix in (".webm", ".m4a", ".mp4", ".mkv",
                                              ".opus", ".ogg"):
            found.setdefault(path.stem, path)
    return found


def backfill(*, dry_run: bool = False, limit: Optional[int] = None,
             overwrite: bool = False) -> int:
    conn = localcache.connect()
    cover_store.ensure_table(conn)
    media = cached_media()
    print(f"{len(media)} cached media file(s)\n")

    rows = conn.execute(
        "SELECT t.track_id, t.artist, t.title, s.url"
        " FROM tracks t JOIN sources s ON s.track_id = t.track_id"
        " WHERE s.url LIKE '%youtu%' ORDER BY t.artist, t.title").fetchall()

    kept = skipped = failed = 0
    for row in rows:
        if limit and kept >= limit:
            break
        vid = localcache.extract_youtube_id(row["url"] or "")
        if not vid:
            continue

        track_id = int(row["track_id"])
        if not overwrite and cover_store.grid_for_track(track_id, conn) is not None:
            skipped += 1
            continue

        label = f"{row['artist'][:22]:24} {row['title'][:28]:30}"
        if dry_run:
            print(f"  {label} would fetch {vid}")
            kept += 1
            continue

        # Local media first, on the chance a download ever keeps its video.
        source = media.get(vid)
        art_url = row["url"] or ""
        if source is None or coverart.probe_size(source) is None:
            fetched = thumbnail_source(vid)
            if fetched is None:
                print(f"  {label} no thumbnail")
                failed += 1
                continue
            source, art_url = fetched

        chosen = best_frame(source, cols=cover_store.STORE_COLS,
                            rows=cover_store.STORE_ROWS)
        if chosen is None:
            print(f"  {label} unreadable")
            failed += 1
            continue

        pixels, seek, brightness, variance = chosen
        note = f"bright {brightness:5.1f}  var {variance:7.0f}"
        key = cover_store.store(pixels, conn, source_url=art_url)
        if key is None:
            failed += 1
            continue
        cover_store.link(track_id, key, conn)
        kept += 1
        print(f"  {label} {note}")

    info = cover_store.stats(conn)
    print(f"\n{kept} kept, {skipped} already had art, {failed} failed")
    print(f"library now holds {info['covers']} cover(s) for {info['tracks']} "
          f"track(s), {info['bytes'] / 1024:.0f} KB")
    conn.close()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--overwrite", action="store_true",
                    help="re-sample tracks that already have kept art")
    args = ap.parse_args(argv)
    log.info("backfilling cover art from cached media")
    return backfill(dry_run=args.dry_run, limit=args.limit,
                    overwrite=args.overwrite)


if __name__ == "__main__":
    raise SystemExit(main())
