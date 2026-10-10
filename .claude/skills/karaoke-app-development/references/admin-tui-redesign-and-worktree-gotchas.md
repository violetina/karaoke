# Admin TUI redesign patterns + worktree/rebase gotchas

Session-specific supplement to `admin-tui-and-backend-service-pitfalls.md` and
`git-worktree-pr-merge-lifecycle.md`. Captures a redesign of `make admin`.

## FIRST: `make admin` is a SEPARATE app from `make tui`
Two distinct Textual apps live in this repo:
- `karaoke.tui` (`make tui`) — the player-facing karaoke TUI ("the normal TUI").
- `karaoke.admin_tui` / `KaraokeAdminApp` (`make admin`) — the backend-ops
  dashboard: Celery worker/queue status, audio backfill, vector rebuild,
  recording analysis, Whisper align, folder ingest, error-log viewer,
  connected-clients panel, web-UI shutdown.

When the user says "the admin TUI" they mean `src/karaoke/admin_tui.py`, NOT
`tui.py`. When asked to "redesign an existing piece," run `make <target>` and
read the launched module BEFORE editing anything. In this session I repeatedly
edited `tui.py` and even scaffolded a brand-new admin file, which was wrong and
frustrated the user ("go f* yourself check the main make admin... i asked to
redesign an existing peace"). The `Makefile` `<target>:` recipe names the exact
module (`$(PYTHON) -m karaoke.admin_tui`) — grep it first.

## Admin TUI layout: bound panels, contain the log
`KaraokeAdminApp` shipped with no `CSS`, so a long traceback in the
`#error-list` OptionList spilled over/under other panels. Fix:
- Bounded/`auto` height + `border` + `margin-bottom` on each panel container.
- Log gets its own scroll box: `#log-container { height: 1fr; min-height: 6; }`,
  `#error-list { height: 1fr; overflow-y: auto; }`.
- Whole stack scrolls on short terminals:
  `#admin-workspace { layout: vertical; height: 1fr; overflow-y: auto; }`.
- Bound tables (`#worker-table { height: 5; }`) so a long unit list can't push
  lower panels off-screen.
- Verify headless with `app.run_test(size=(100,40))`: assert
  `error_list.region.bottom <= log_container.region.bottom + 1` and
  `workspace.virtual_size.height > viewport height` (it actually scrolls).

## Connected-clients panel
No status endpoint for the textual-serve web UI, so count live browser tabs from
ESTABLISHED sockets on the serve port:
`ss -Htn state established '( sport = :<PORT> )'` (default PORT 8001; ~one
websocket per tab). Wrap in try/except — `ss` may be missing or server down;
return `[]`, never raise. Playback clients: `ApiClient.list_play_sessions()`
(control API, best-effort). Refresh on a `set_interval` + a manual key binding.

## Graceful web-UI shutdown
The web TUI has NO systemd unit — it's `scripts/web_serve.py` run by hand. Stop
it cleanly by SIGTERM (signal 15, NOT SIGKILL) so browser tabs get a clean
socket close: `pgrep -f web_serve.py` → `os.kill(pid, 15)`, guarding each kill
against `ProcessLookupError`/`PermissionError`/`ValueError`. Report the count
signalled; never touch the admin process itself.

## Git: `git rebase --continue` hangs the terminal tool (nvim editor)
After staging conflict resolutions, `git rebase --continue` opens `$EDITOR`
(nvim) for the commit message and BLOCKS; the terminal tool then times out with
`Nvim: Caught deadly signal 'SIGTERM'`, leaving the rebase half-applied.
Non-interactive fix: `GIT_EDITOR=true git rebase --continue` (or
`git -c core.editor=true rebase --continue`). Same for `git commit --amend`
mid-rebase.

Bigger lesson: for a **redesign of an existing feature** (not "carry an old
branch forward"), don't rebase a stale/legacy feature branch onto main — cut a
fresh branch from `origin/main` (`git checkout -B feat/<x> main` after the
primary worktree pulled main) and re-apply only the intended change. Rebasing a
legacy branch drags in squash-merged history and conflicts in files you never
meant to touch.

## Git: `gh pr merge --delete-branch` fails under worktrees (merge still lands)
`gh pr merge <N> --merge --delete-branch` errors with
`fatal: 'main' is already used by worktree at /home/tina/karaoke` — but the
GitHub merge SUCCEEDS; only the local branch-switch/delete fails. Do NOT re-run.
Verify + clean up manually:
`gh pr view <N> --json state,mergeCommit` (expect MERGED) →
`cd /home/tina/karaoke && git checkout main && git pull --ff-only origin main` →
`git worktree remove /home/tina/karaoke-<feature> --force` →
`git branch -d feat/<feature>`.
