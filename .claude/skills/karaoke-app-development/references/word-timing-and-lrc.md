# Word-level timing, LRC formats, and the instrumental-gap bug

## The bug: word highlight drifts across instrumental riffs

`LyricTimeline.active_fraction` (`src/karaoke/player.py`) assumes a line lasts
until the *next line's* timestamp:

```python
end = self.lines[a + 1][0] if a + 1 < len(self.lines) else start + tail
```

LRCLIB gives **line-level** timestamps only, so the renderer spreads a line's
words evenly across that span. When an instrumental break follows a line, the
words crawl across the whole riff.

Reproduced on real cache data (`Ren - Hi Ren`, 146 lines, gaps of 21.8s and
51.7s). With a 10-word line at 60.0s and the next line at 111.7s:

| elapsed | frac | highlighted word |
|---|---|---|
| 64.0s | 0.08 | `When` — singer already finished the line |
| 80.0s | 0.39 | `seventeen` |
| 110.0s | 0.97 | `void` |

This affects **every** LRCLIB-sourced track, not just caption-derived ones.

Quick reproduction:

```python
from karaoke.player import LyricTimeline, active_word_index
tl = LyricTimeline([(60.0, "When I was seventeen I shouted out into the void"),
                    (111.7, "next line")])
for e in (60, 64, 80, 110):
    f = tl.active_fraction(e)
    print(e, round(f, 2), active_word_index(tl.lines[0][1], f))
```

Fix direction: cap a line's duration at
`min(next_line_start, start + cap)` where `cap` derives from word count
(~0.55s/word, floor 2s, ceiling 12s) rather than a flat constant — a flat cap
misbehaves on legitimately long, slowly-sung lines.

## Enhanced LRC is the standard for word-level timing

Standard LRC is line-level. **Enhanced LRC** adds per-word timestamps in angle
brackets and is what karaoke-style highlighting actually targets (supported by
AIMP, foobar2000/ESLyric, Kugou, QQ Music, NetEase):

```
[00:12.00]<00:12.00>I <00:12.30>see <00:12.60>trees <00:12.90>of <00:13.20>green
```

Rules: the `[mm:ss.xx]` line tag comes first; the first word tag usually equals
the line tag; no space between a tag and its word.

Note: Spotify and Apple Music use proprietary formats and do **not** accept
external LRC.

## Free word timings already available

YouTube `json3` captions carry per-word `tOffsetMs` (see
`references/sqlite-vector-source-model.md`), e.g.
`(21920, 'Heat'), (22000, ' up here.')`. `caption_sync` currently collapses
these to the cue start. Emitting Enhanced LRC instead gives word-accurate
timing with **zero user effort** — usually higher leverage than hand-tapping,
since tapping a 146-line song is tedious and human jitter is ~100-200ms.

## Codebase gotchas to check before building on this

- **`parse_lrc` silently drops empty-text timestamps.** `[00:14.00]` with no
  text — the conventional LRC idiom for "line ends here / instrumental" — is
  discarded. Verify with
  `parse_lrc("[00:10.00]a\n[00:14.00]\n[01:05.00]b")` → only two entries.
  Any end-marker feature needs explicit parser support.
- **`put_cached_lyrics` DELETEs existing `kind='approved'` rows** before
  inserting (`localcache.py`). A naive "save learned timings" would destroy the
  LRCLIB plain lyrics. Merge into the existing row instead: replace only
  `synced_lyrics`, keep `plain_lyrics`.
- **Don't change the `lines: list[tuple[float, str]]` shape.** Lots of code and
  tests index `[0]`/`[1]`. Add parallel optional structures on `LyricTimeline`
  (e.g. `ends: dict[int, float]`, `word_times: dict[int, list[float]]`) and
  resolve in priority order: real word times → explicit end → duration cap →
  `start + tail`.
- **`_KeyReader`** (cbreak, non-blocking, no-op when stdin isn't a TTY) plus the
  existing `v`/`b`/`0` nudge dispatch is the pattern to extend for any new
  interactive key mode. Test the dispatch as a pure function
  (`handle_key(state, key, elapsed, idx)`) so tests need no TTY.
