# Admin TUI Folder Scan Progress

When adding or changing long-running Admin TUI flows such as local folder ingestion, do not use silent fire-and-forget API calls as the only feedback path. First-time user-tested flows need visible progress and a log to follow.

Recommended pattern:
- Run the operation inside the Admin TUI worker pool with `self.run_worker(..., thread=True)`.
- Hold `_acquire_bg_lock()` for the duration of the task and always release it in `finally`.
- Call `_mark_task_started()` before dispatch and `_mark_task_finished()` from the worker via `self.call_from_thread(...)`.
- Add a progress callback in the long-running library function, emitting events such as found, item_start, tagged, analysis_start/done, source_start/done, ingest_start, item_done, error, and done.
- In the Admin TUI, format those events into the right-side `#event-log` and update `#job-status` with current file index/count.
- Escape dynamic Rich/Textual markup in paths, especially square brackets in album folders like `[EP]`.
- Label visible buttons with shortcut keys, e.g. `Scan Folder (s)`, so the user can discover how to start the action after pasting into an input field.
- Also log progress through `karaoke.logger` so `/home/tina/.local/share/karaoke/logs/karaoke.log` can be followed externally.

Verification:
- Add unit tests for progress event emission in the backend function.
- Add Admin TUI formatter tests for paths with square brackets and final count summaries.
- Run `.venv/bin/python -m compileall src/` and `.venv/bin/python -m pytest`.
