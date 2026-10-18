#!/usr/bin/env python
"""Score mood-timeline feelings against Tina's by-ear verdicts.

The verdicts (mood_check_verdicts.json in the data dir) come from listening to
the "Karaoke: Check mood analyse" playlist on 2026-10-18. EXPECT maps each
verdict to the feeling words that would count as agreeing; it is an
interpretation of her words, kept here so it can be argued with. Nothing is
stored: the analysis runs without saving.

As built: 13/16 (misses: Video Games -- no lyrics in the DB; Crows -- angry
without aggressive sound or words; Pow Wow -- irony). Measured on the songs
that motivated the changes, so treat it as optimistic until a fresh round.
"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from karaoke import localcache, chord_timeline, mood_timeline as mt
from karaoke.config import settings
from karaoke.lyrics import parse_lrc
EXPECT = {1: {"romantic", "calm", "tender", "hopeful"}, 2: {"sad", "melancholic", "longing"}, 3: {"dreamy"},
          4: {"uplifting", "hopeful", "calm"}, 5: {"energetic", "intense", "dramatic"}, 7: {"dramatic", "cheerful", "festive"},
          9: {"longing", "melancholic"}, 10: {"angry"}, 11: {"cheerful", "melancholic", "happy", "sad"},
          13: {"festive", "energetic", "cheerful"}, 14: {"frustrated", "blue"}, 15: {"dreamy", "calm", "romantic"},
          16: {"cheerful", "happy"}, 17: {"sad", "melancholic", "blue"}, 18: {"frustrated", "blue"}, 19: {"romantic", "tender"}}
verdicts = json.loads((Path(settings.data_dir) / "mood_check_verdicts.json").read_text())
hit = n = 0
with localcache.connect() as c:
    for v in verdicts:
        if v["n"] not in EXPECT: continue
        tid = v["track_id"]; path = chord_timeline.local_audio(tid, c)
        if path is None: print(f"#{v['n']} no audio"); continue
        r = c.execute("SELECT synced_lyrics, plain_lyrics FROM lyrics WHERE track_id=%s AND kind='approved'", (tid,)).fetchone()
        lines = [(t, x) for t, x in parse_lrc((r or {}).get("synced_lyrics") or "") if x.strip()] if r else []
        ch = c.execute("SELECT harmony FROM track_chords WHERE track_id=%s", (tid,)).fetchone()
        blues = bool(ch and (ch["harmony"] or {}).get("style") == "blues")
        tl = mt.analyse(str(path), lines, (r or {}).get("plain_lyrics") or "", blues=blues)
        feels = {s.feeling for s in tl.segments}
        dom = tl.summary["dominant_feeling"]
        ok = dom in EXPECT[v["n"]] or bool(feels & EXPECT[v["n"]])
        n += 1; hit += ok
        print(f"#{v['n']:<2} {'✓' if ok else '✗'} {v['title'][:22]:22} you: {v['verdict'][:34]:34} | now: {tl.summary['feeling_arc'][:60]}  [contrast: {tl.summary['contrast']}]")
print(f"\nfeeling matches your words: {hit}/{n}")
