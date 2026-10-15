#!/usr/bin/env python
"""Score mood timelines against a reference set with known moods and swings.

The set is small and chosen for songs whose mood is uncontroversial, plus
known borderliners and structural swings. Track ids are this library's.
Reported when the module was built (src/karaoke/mood_timeline.py docstring):
raw CLAP 6/15, calibrated 7/15, + lexicon 9/15, + lyric meaning 11/15
(whole-song), 10/15 for the time-weighted per-segment timeline.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from karaoke import localcache, mood_timeline  # noqa: E402

REFERENCE = {
    "angry": [1580, 18231, 5103],            # Hey Joe (Body Count), Paranoid, The Pretender
    "sad": [5065, 15536, 10465, 9174, 16714],  # Tears in Heaven, The Scientist, Pale Blue Eyes, Danny Boy, Summertime Sadness
    "happy": [17273, 3186, 4605, 9277],       # Our House, Heart of Glass, Cheek to Cheek, A Boy Named Sue
    "tender": [4611, 5082, 4872],             # Someone to Watch Over Me, Wonderful Tonight, Dream a Little Dream
    "borderline": [10400, 17485, 571],        # Perfect Day, Wild Is the Wind, Feeling Good
    "swing": [146, 1606],                     # Bohemian Rhapsody, Moya
}


def main() -> int:
    ok = total = 0
    with localcache.connect() as conn:
        for label, ids in REFERENCE.items():
            for tid in ids:
                tl = mood_timeline.analyse_track(tid, conn)
                name = conn.execute("SELECT artist || ' - ' || title AS n FROM tracks"
                                    " WHERE track_id = %s", (tid,)).fetchone()["n"]
                if tl is None:
                    print(f"{label:10}   {name[:36]:36} no audio on disk")
                    continue
                s = tl.summary
                scored = label in mood_timeline.AUDIO_MOODS
                hit = s["dominant"] == label
                total += scored
                ok += scored and hit
                mark = ("✓" if hit else "✗") if scored else " "
                print(f"{label:10} {mark} {s['dominant']:6} swings={s['swing_count']} "
                      f"border={s['borderline_share']:4.0%}  {name[:36]:36} {s['arc'][:60]}")
    print(f"\ndominant mood: {ok}/{total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
