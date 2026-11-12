"""Repair tracks whose artist/title were split on a version qualifier (dry-run by default).

``Song - Remastered 2012`` (or ``Song (2002 - Remaster)``) was once read as
``Artist - Title``, giving artist ``Song`` and title ``Remastered 2012`` (or a title
cut inside the brackets). The real artist is taken from the other tracks of the same album;
the song becomes the title with its qualifier in brackets. When that artist+title already
exists the row is merged into it (sources, genre, moods and so on move with
``db_cleanup.merge_tracks``), otherwise it is renamed. Rows with no album to learn the
artist from are only reported.

Usage:
    PYTHONPATH=src python scripts/fix_version_only_titles.py          # report
    PYTHONPATH=src python scripts/fix_version_only_titles.py --apply
"""
from __future__ import annotations

import argparse
import importlib.util
import re
from pathlib import Path

from karaoke import localcache
from karaoke.release_year import is_compilation_name, is_live, norm

QUALIFIER = re.compile(
    r"^\(?\s*(?:(?:19|20)\d{2}\s+)?remaster(?:ed)?(?:\s+(?:19|20)\d{2})?\s*\)?$", re.I)
CUT_BRACKET = re.compile(r"^(?P<song>.*?)\s*\((?P<year>(?:19|20)\d{2})$")


def split_bad_row(artist: str, title: str):
    """``(song, qualifier)`` if this row is a qualifier split off a song, else None."""
    if not QUALIFIER.match(title.strip()):
        return None
    m = CUT_BRACKET.match(artist.strip())
    if m:
        return m["song"], f"{m['year']} {title.strip(chr(41)).strip()}"
    return artist.strip(), title.strip()


def load_merge():
    spec = importlib.util.spec_from_file_location(
        "db_cleanup", Path(__file__).resolve().parent / "db_cleanup.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.merge_tracks


def run(apply: bool) -> int:
    merge_tracks = load_merge() if apply else None
    with localcache.connect() as conn:
        rows = conn.execute("SELECT track_id, artist, title, album FROM tracks"
                            " WHERE title ~* 'remaster'"
                            " ORDER BY track_id").fetchall()
        plan, unresolved = [], []
        for r in rows:
            bad = split_bad_row(r["artist"], r["title"])
            if not bad:
                continue
            song, qualifier = bad
            artist = None
            # A compilation's tracks have many artists, so its siblings say nothing.
            if (r["album"] or "").strip() and not is_compilation_name(r["album"]) \
                    and not is_live(r["album"]):
                sib = conn.execute(
                    "SELECT artist, count(*) n FROM tracks WHERE lower(album) = lower(%s)"
                    " AND track_id <> %s AND NOT (title ~* '^\\(?\\s*((19|20)[0-9]{2} )?"
                    "remaster') GROUP BY artist ORDER BY n DESC LIMIT 1",
                    (r["album"], r["track_id"])).fetchone()
                artist = sib["artist"] if sib else None
            if not artist:
                unresolved.append(r)
                continue
            new_title = f"{song} ({qualifier})"
            dest = None
            for cand in conn.execute(
                    "SELECT track_id, title FROM tracks WHERE track_id <> %s AND artist = %s",
                    (r["track_id"], artist)).fetchall():
                if norm(cand["title"]) in (norm(song), norm(new_title)):
                    dest = cand
                    break
            plan.append((r, artist, new_title, dest))

        for r, artist, new_title, dest in plan:
            action = f"merge into #{dest['track_id']} '{dest['title']}'" if dest else "rename"
            print(f"#{r['track_id']:>6} {r['artist'][:30]:30} | {r['title'][:22]:22} -> "
                  f"{artist[:24]} | {new_title[:44]}  [{action}]")
        for r in unresolved:
            print(f"#{r['track_id']:>6} unresolved (no usable album to learn the artist from): "
                  f"{r['artist']} | {r['title']}")
        print(f"{len(plan)} repairable ({sum(1 for p in plan if p[3])} merges), "
              f"{len(unresolved)} unresolved")
        if not apply:
            return 0
        for r, artist, new_title, dest in plan:
            if dest:
                merge_tracks(r["track_id"], dest["track_id"], conn)
            else:
                conn.execute("UPDATE tracks SET artist = %s, title = %s WHERE track_id = %s",
                             (artist, new_title, r["track_id"]))
            conn.commit()
        print("applied")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true")
    raise SystemExit(run(ap.parse_args().apply))
