# TUI branch parity with `main`: key/BPM and gap markers

Session lesson from karaoke TUI feature work.

## Reliable workflow

1. Work in the matching feature worktree, not `/home/tina/karaoke` main:
   - TUI feature branch: `/home/tina/karaoke-tui`
   - Use `PYTHONPATH=src` or Make targets so imports come from the worktree.

2. Before diagnosing a missing TUI feature, compare/rebase against `origin/main`:
   - `main` may already contain related runtime fixes from merged PRs.
   - Example: word timing and instrumental gap handling lived in `main`; the TUI branch looked broken only because it was behind.

3. If rebasing an old feature branch onto `origin/main`, expect commits that are already upstream:
   - Prefer skipping superseded/cherry-picked commits during rebase rather than resolving old duplicate add/add conflicts by hand.
   - After rebase, re-check `git log --oneline --decorate` and `git status -sb`.

4. Gap/music-fill behavior is runtime rendering, not usually a DB transform:
   - `LyricTimeline.line_end`, `in_gap`, and `gap_progress` infer instrumental sections from long gaps after capped line duration.
   - `_render_body` calls `_gap_marker(...)` and displays a note/progress marker such as `♪ ───•` while waiting for the next lyric.
   - Enhanced LRC word tags/end markers are parsed by `parse_enhanced_lrc`; no extra manual postprocessing is needed once the branch includes that code.

5. Key/BPM display path:
   - Analysis rows live in `track_analysis` and are read through `track_analysis.get_analysis(...)`.
   - The TUI can resolve the current track by `track_id` when browsing, or by artist/title using `localcache.find_track_id(...)` while following Spotify/MPRIS.
   - Show a compact `key: ...  bpm: ...` in the now-playing panel for immediate feedback, and keep the richer key/BPM panel in the visuals column.

## Verification checklist

- Run `make test` in `/home/tina/karaoke-tui`.
- Run the cache analyzer from the worktree if new YouTube files were staged:
  `PYTHONPATH=src .venv/bin/python scripts/analyze_all_cached.py`
- Check analysis coverage with SQLite via project modules (`localcache`, `track_analysis`) rather than guessing from cache files alone.
- Push rebased feature branches with `--force-with-lease` only when rewriting the feature branch is intended and safe.
