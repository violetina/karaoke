# Web TUI, Queue, and Long-Running Ingestion Lessons

## Web TUI service lifecycle

For a Textual TUI served through `textual-serve`, manage it with a dedicated `systemd --user` unit rather than an ad-hoc terminal process. The unit should use `PYTHONPATH` and the project venv explicitly, include `Restart=on-failure`, `TimeoutStopSec=10`, and `KillMode=control-group`. A stale WebSocket child can otherwise leave `systemctl restart` stuck in `deactivating (stop-sigterm)`. After Python edits, restart the Web TUI unit; after unit-file edits, run `make systemd-install` first, then restart. Verify both `systemctl --user is-active` and an HTTP GET to the configured port.

## SQLite contention during unattended ingestion

A large folder/cache importer and the Web TUI can legitimately overlap. Configure SQLite connections with a meaningful connection timeout and `PRAGMA busy_timeout` so a transient writer lock does not crash the UI. UI filter/refresh handlers should catch database-busy exceptions, log them, and notify/defer rather than allowing a Textual child process to terminate. Keep importer progress in a dedicated logfile and make per-file errors non-fatal.

## Bounded startup and queue authority

Do not load/render an entire growing music library on TUI startup. Bound the initial browse sample (for example, 70 items) and use random sampling for wildcard browse. Do not autoplay the initial wildcard sample. Once a local queue is populated, Follow Queue should become authoritative; prevent the normal next-track action from falling back to YouTube Music's unrelated temporary queue while a local queue is active.

## CDP navigation and queue races

CDP calls can block or time out when the kiosk WebSocket is stale. Never perform blocking song navigation on the Textual UI thread; use a worker and marshal notifications/UI updates back with `call_from_thread`. Investigate rapid next/previous behavior separately from YouTube Music's temporary `TL...` queue: `ytmusicapi` manages persistent playlists but not the live queue, which must be handled through CDP or by making the local queue authoritative.

## Music-reactive ASCII animation

For terminal dancers, use the actual visual widget width, enforce hard left-half/right-half ownership boundaries, and fall back to one compact complete dancer when two figures cannot fit. Stored audio analysis provides useful stable proxies: energy/loudness for bass-like motion and spectral brightness for high-tone motion. Keep slow songs grounded by disabling hops below the requested BPM threshold. Sentiment and genre can drive interaction style, but clipping/overlap safety takes priority over visual complexity.
