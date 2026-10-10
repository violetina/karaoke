# Validated runtime lessons

- Web TUI runs under `karaoke-webtui.service` on port 8001; Python changes reload with `systemctl --user restart karaoke-webtui.service`, while unit-file changes require `make systemd-install` first.
- The Web TUI service owns a textual-serve parent and WebSocket child TUI processes. `TimeoutStopSec=10` plus `KillMode=control-group` bounds shutdown when Ctrl+C or worker threads leave a child alive.
- Large archive ingestion overlaps with TUI reads. SQLite needs a busy timeout; UI filter refreshes should catch `database is locked` and warn/defer instead of terminating the screen.
- The full collection scan is a sequential per-file loop, not automatically one Celery task per file. Use per-file progress logs and checkpoint/manifest support for efficient interruption and resume.
- Initial TUI wildcard loading should be bounded and non-autoplaying; current responsive behavior samples 70 rows. Typed search can still query the full database.
- Local Karaoke queue state and YouTube Music temporary `TL...` queue state are separate. A populated local queue should make Follow Queue (`o`) authoritative; persistent playlists via `ytmusicapi` are not a live queue controller.
- Pass actual widget width to ASCII visuals, hard-clip actors to owned regions, and use a compact single-actor fallback when two poses do not fit.
