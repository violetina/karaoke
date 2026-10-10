# Scan-mode lyric resolution: decorated & multi-artist titles

## Symptom
A song that HAS synced lyrics in the DB shows "no lyrics" / "text doesn't follow"
in scan mode, pushing the user to forced-browse. **Forced-browse is STATIC by
design** (library-driven; the clock does not own the lyrics panel), so timestamps
show but never advance — that is expected, not the bug. The real bug is that
`resolve_lyrics` failed to match the cached track.

## Cause 1: decorated browser titles (FIXED — clean_title fallback)
Browser MPRIS titles carry suffixes the cached track lacks, e.g.
`Too Many Chiefs... Not Enough Indians (2019 Remastered)` vs the DB's
`Too Many Chiefs... Not Enough Indians`. Exact `find_track_id` misses.

Fix (LANDED, src/karaoke/detect.py `resolve_lyrics`): after the exact match misses,
retry with `lyrics.clean_title(det.title)` and, on hit, return the DB row's
canonical artist/title + lyrics.
```python
from .lyrics import clean_title
cleaned = clean_title(det.title)
if cleaned and cleaned != det.title:
    track_id = localcache.find_track_id(det.artist, cleaned, conn)
    ...
```
`clean_title` strips version suffixes (Live / Remastered / feat. / edit / acoustic /
extended / bonus / demo …) via `_TITLE_SUFFIX`, applied repeatedly for stacked
suffixes. It does NOT strip `(Official Video)` — that YouTube decoration is handled
upstream by `parse_youtube_title`, not by `clean_title`.
Regression test: `tests/test_detect_resolve.py::test_resolve_lyrics_strips_remaster_suffix`.

## Cause 2: multi-artist comma vs ampersand (OPEN follow-up)
Browser reports `"Inpatient, Ren, Chris Webby"` (commas) while the DB stores
`"Inpatient, Ren & Chris Webby"` (ampersand). Title matches but exact-artist
matching fails, so lyrics don't resolve even though they exist. Not yet fixed;
candidate fix is a fuzzy-artist or title-distinctive fallback in `resolve_lyrics`.
This is the same multi-artist class that `make db-cleanup` dedups at the track
level (see SKILL.md dedup pitfall), but resolution-time matching still needs it.

## Diagnosis recipe
```python
from karaoke import detect, localcache
det = detect.detect_active(); conn = localcache.connect()
a, t, ly = detect.resolve_lyrics(det, conn)
print(det.mode, repr(a), repr(t), None if ly is None else ly.has_synced)
```
If `resolve_lyrics` returns `synced? None` but a direct DB query shows the track
DOES have `synced_lyrics`, it's a matching miss (decoration or multi-artist), not a
missing-lyrics problem. Also confirm `detect.current_metadata()` matches what's
actually playing — a track change mid-diagnosis mimics a sync/resolution bug.
