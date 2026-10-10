# Scan-mode lyric sync offset (lyrics lead the audio)

## Symptom
In scan mode the highlighted lyric runs **ahead** of the audible sound. Observed
this session at ~1.3s ahead for a YouTube tab in the dedicated Chrome player.

## Root cause
Browser MPRIS `position` (what `playerctl position` reports) is slightly AHEAD of
audible output because of output/device buffering. The old `_tick_lyrics` fed the
raw position straight into the timeline with **zero offset**, so the highlight led
the sound. There was no offset knob anywhere.

## Fix (LANDED, src/karaoke/tui.py)
Subtract a positive sync offset from the reported position before highlighting:

```python
elapsed = max(0.0, pos - self._sync_offset)
```

- Default offset `1.3`s via module helper `_default_sync_offset()`, reading
  `KARAOKE_SYNC_OFFSET` (falls back to 1.3 on unset/bad value).
- Live nudge keys bound in the TUI: `,` = `action_sync_earlier` (offset +0.1,
  lyrics EARLIER) and `.` = `action_sync_later` (offset -0.1, lyrics LATER). Offset
  may go negative to delay lyrics past the raw position. Textual binding names are
  `comma` and `full_stop`.
- `_nudge_sync()` re-runs `_tick_lyrics()` immediately so the change is visible
  without waiting for the next poll.
- The current offset is surfaced on the now-playing panel:
  `synced lyrics · <source> · offset ±X.Xs (, / .)`.
- Tests: `tests/test_tui.py::test_default_sync_offset_*` (env / fallback / default).

## Notes / possible follow-up
- This is a single GLOBAL offset, not per-track. If songs drift differently, tune
  live with `,`/`.`. A future enhancement is persisting a per-track offset back to
  the DB so each song remembers its own — offered to the user, not yet built.
- Distinguish this from a wrong-track transition: when diagnosing "sync looks off",
  first confirm `detect.current_metadata()` matches what is actually playing (a
  track change mid-diagnosis can look like a sync bug).
