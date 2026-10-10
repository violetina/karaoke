# Admin TUI Visual QA, Event Visibility, and RGB Bounds

Session-derived guidance for future `make admin` / TUI work in the karaoke project.

## User workflow corrections
- When the user says "admin TUI", work on the existing dedicated app launched by `make admin` (`src/karaoke/admin_tui.py`). Do not start in the normal player TUI (`src/karaoke/tui.py`) unless explicitly asked.
- Before admin-TUI coding, inspect `Makefile`, docs, and the existing `admin_tui.py`; do not recreate an app that already exists.
- Use screenshots from `~/Pictures/Screenshots/` as first-class evidence when the user reports layout issues. Headless Textual screenshots are useful, but compare against the user's actual screenshot when available.

## Admin log layout pattern
- A single vertical stack leaves the diagnostics/log section off-screen or cramped. Prefer a horizontal split dashboard for operations apps.
- Use a wider right pane when logs/events are the main diagnostic surface: left controls around 45%, right visibility pane around 55%.
- Right pane order that worked well:
  1. Visible current pipeline task status (`RUNNING`, `DONE`, failure) so the single-operation lock is understandable.
  2. Latest platform events from the Argo/OpenSearch event ledger.
  3. Contained error log diagnostics.
- Do not use `OptionList` for logs. Use `RichLog(highlight=True, markup=True, wrap=True)` and update only when content changed to avoid flicker and scroll reset.

## Running-task feedback in admin TUI
- If a UI allows only one pipeline task at a time (`_bg_busy` + cross-process lock), surface that state in the UI, not only as a transient `notify()` toast.
- Add helpers similar to:
  - `_mark_task_started(label)` -> set `RUNNING <label>` in a visible `Static`.
  - `_mark_task_finished(label, message, severity=...)` -> set `DONE` or error state and clear active task metadata.
- Keep the lock; make it legible.

## Event-driven refresh pattern
- Admin TUI should poll `ApiClient.recent_events()` and refresh event/log/worker panels via `call_from_thread` when new events arrive.
- Normal/web TUI should use the same event ledger to refresh track info and queue/player-related panes in place instead of full browser reloads. Full reload under `textual-serve` tears down the TUI child and kills in-process sampling/recording/mic loops.
- Future event classes to reserve/handle: `karaoke.playback.*`, `karaoke.queue.*`, `karaoke.metadata.*` / Wikibase/Wikidata enrichment events.

## RGB color bounds regression
- Mood-square animation can push RGB channels negative if a sinusoidal boost is allowed below zero. Clamp every generated/animated channel to `0..255` before constructing Rich/Textual style strings.
- Add both source-level and render-boundary defenses:
  - Clamp in `visuals.animate_mood_pixels()` after channel arithmetic.
  - Clamp in `coverart.to_text()` before `style=f"on rgb({r},{g},{b})"` so any caller is protected.
- Regression test with values resembling observed failures (`rgb(62,34,-18)`, `rgb(76,51,-3)`, `rgb(-8,-9,1)`) and assert all channels remain within `0..255` and `coverart.to_text()` does not raise.
