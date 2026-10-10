# TUI Creative Visuals and Wildcard Queue

Session-derived implementation notes for the karaoke TUI class.

## Wildcard discovery queue

Treat `*` as browse-all/discovery mode. Build the candidate set from SQLite, apply active mood and genre filters, hydrate key/BPM/energy/play-count fields, then randomize the final queue. Do not rely on `ORDER BY artist, title` for the user-facing wildcard queue. Keep the sort selector for intentional ordering and synchronize duplicate selectors (sidebar and Browse overlay). A mood/energy default is more useful for discovery than key order.

## Non-blocking browse playback

Opening a selected browse row can issue blocking CDP calls. Close the overlay promptly and run `open_song_url()` in a Textual worker thread. Return notifications and error-panel updates through `call_from_thread()`. This prevents stale CDP WebSocket timeouts from freezing the event loop and swallowing a later `H` shortcut.

## Beat-synchronized duet animation

Extend the current visual renderer instead of adding another timer. A mirrored duet can use BPM phase for pose changes and travel, energy as the separation/independence control, dominant lyric sentiment for relationship state, and broad genre families for pose vocabulary. Tender/love sentiment can produce periodic meetings and a heart marker. Keep dancers grounded below 100 BPM; only enable the existing hop vocabulary at or above 100 BPM. Use fixed-height output so the surrounding TUI layout does not jump.

## Verification

Add tests for: wildcard shuffle after filtering, default mood/energy sort, sub-100-BPM grounded frames, tender meeting frames, and at least two genre-specific dance styles. Run the focused TUI/visual tests and the full pytest suite.
