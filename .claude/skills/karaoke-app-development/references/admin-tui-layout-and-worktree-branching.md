# Admin Operations TUI (`make admin`), Worktree Branching, & Panel Containment

Key architectural and workflow lessons from developing and maintaining the backend operations TUI (`karaoke.admin_tui`) and coordinating worktree branches.

---

## 1. Separate TUIs: Main Player TUI vs. Admin Operations TUI

The codebase maintains two distinct terminal applications with completely different responsibilities:
- **`karaoke.tui` (`make tui`)**: The player-facing lyrics and media control interface. Uses playerctl, MPRIS, cover art rendering, sentiment arcs, and audio theory.
- **`karaoke.admin_tui` (`make admin`)**: The dedicated backend operations and worker control dashboard. Manages Celery worker scaling (`+` / `-` / `R`), Flower visibility, RabbitMQ queue depth, audio backfill, vector indexing, Whisper lyric alignment, and connected clients.

**Critical Pitfall**: Never add administrative screens or modal popups (like `ctrl+a`) to `karaoke.tui`. Always inspect `Makefile` and `src/karaoke/admin_tui.py` first.

---

## 2. Git Worktree Branching: Local `main` vs. `origin/main`

In repositories where rapid local development occurs on `main` (such as solo repos with unpushed commits):
- `origin/main` may lag significantly behind local `main`.
- Creating a worktree against `remotes/origin/feat/tui` or rebasing onto `origin/main` pulls stale commits and generates false merge conflicts against today's work.
- **Rule**: When branching a feature worktree from `main`, branch from the **local** `main` ref:
  ```bash
  git checkout -B feat/<name> main
  ```
- Always verify commit alignment first:
  ```bash
  git log -1 main --oneline
  git log -1 origin/main --oneline
  ```

---

## 3. Textual Admin Dashboard Layout & Panel Containment

Multi-panel dashboards without explicit CSS suffer from widgets expanding, overlapping, or pushing siblings out of view:

### Bounded Containers & Dedicated Scroll Regions
- Every distinct panel (`#worker-container`, `#clients-container`, `#pipeline-container`, `#log-container`) must have defined borders and padding.
- **The Log Containment Pattern**: Live log lists (`OptionList`) containing long multi-line tracebacks must be contained in a dedicated container with `overflow-y: auto`. Never let the log list grow unboundedly, or it will clip and overlap neighbouring controls.
- **Outer Workspace Scrolling**: Set the outer workspace container (`#admin-workspace`) to `overflow-y: auto; height: 1fr;`. If the terminal window is smaller than the combined height of all panels, the workspace scrolls cleanly rather than truncating panels.

```css
#admin-workspace { layout: vertical; height: 1fr; overflow-y: auto; }

#worker-container, #pipeline-container, #clients-container, #log-container {
    border: round $primary;
    padding: 0 1;
    margin-bottom: 1;
    height: auto;
}

#worker-table { height: 5; margin-bottom: 1; }
#clients-table { height: 6; }

#log-container { height: 1fr; min-height: 6; }
#error-list { height: 1fr; overflow-y: auto; border: none; }
```

---

## 4. Monitoring Connected Clients & Graceful Web UI Shutdown

### Connected Clients Discovery
1. **Web TUI Tabs (textual-serve)**: `scripts/web_serve.py` serves browser sessions over WebSockets on port 8001. Query established socket connections using `ss`:
   ```bash
   ss -Htn state established '( sport = :8001 )'
   ```
2. **Playback Sessions**: Query the control API endpoint `GET /api/play/sessions` via `ApiClient.list_play_sessions()`.

### Graceful Web UI Shutdown (`X`)
Ad-hoc server scripts like `scripts/web_serve.py` do not run as systemd units. To stop them cleanly without dropping active TCP connections into RST:
```python
out = subprocess.run(["pgrep", "-f", "web_serve.py"], capture_output=True, text=True).stdout
for pid in out.split():
    os.kill(int(pid), 15)  # SIGTERM allows textual-serve to close WebSockets cleanly
```

---

## 5. Event Pipeline Review & Celery Schema Changes

- When API schemas evolve (e.g. `ctrl_api` `/api/workers/status` returning `worker_details` instead of `units`), always support both formats defensively in the Admin TUI:
  ```python
  units = res.get("worker_details") or res.get("units") or []
  ```
- Core logic functions renamed in post-processing refactors (e.g. `_run_sync` -> `run_sync_logic`) must be updated across all callers in `admin_tui.py`.
