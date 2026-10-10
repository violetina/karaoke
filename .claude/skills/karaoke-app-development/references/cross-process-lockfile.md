# Cross-process concurrency locks for heavy pipeline tasks

When multiple entry points can trigger the same expensive, mutating operation
(OpenSearch vector rebuild, audio gap-fill/backfill, recording analysis), an
in-process `threading.Lock` is NOT enough — a second `make admin`, the CLI, a
systemd worker, or a cron job runs in a SEPARATE interpreter and sails straight
past a thread lock. Use an OS-level file lock instead.

## `karaoke.lockfile.ProcessLock`
A small wrapper over `fcntl.flock`:
- Lockfiles live under `~/.local/share/karaoke/locks/<name>.lock`
  (`Path(settings.data_dir) / "locks"`).
- `acquire(blocking=False)` opens the file and `flock(fd, LOCK_EX | LOCK_NB)`;
  returns `True` on success, `False` (caught `BlockingIOError/OSError`) when
  another process holds it. `release()` does `flock(fd, LOCK_UN)` + `os.close`.
- Supports `with ProcessLock(name) as ok:` (context manager).

```python
from karaoke.lockfile import ProcessLock
lock = ProcessLock("vector_rebuild")
if not lock.acquire():
    return VectorIndexStats()   # someone else is already rebuilding
try:
    ...  # do the work
finally:
    lock.release()
```

## Where it's wired
- `vector_index.rebuild_from_sqlite` → `ProcessLock("vector_rebuild")`
  (replaced the older in-process `threading.Lock`).
- `scripts/fill_analysis_and_vector_gaps.py` → `ProcessLock("gap_fill")` around
  the whole run; a second invocation prints "already running… Exiting."
- `admin_tui._acquire_bg_lock` → `ProcessLock("admin_pipeline")` in ADDITION to
  the fast in-process `_bg_busy` flag, released in the worker's `finally`.

## Why flock (not a plain lockfile you create/delete)
`flock` is advisory but kernel-managed: the lock is **auto-released when the
holding process dies**, even on crash/kill. So there is no stale-lock cleanup to
write — the `.lock` file may persist on disk but carries no lock once its owner
exits. A "create file / delete file" scheme leaks a stale lock on any crash.

## Verifying it works (genuine two-process test)
Thread-only tests can't prove cross-process exclusion. Spawn two real
subprocesses: A `acquire()`s and sleeps; while it holds, B `acquire()` must
return `False`. Also unit-test same-name/different-fd exclusion and the context
manager (`tests/test_lockfile.py`).

## Test interaction pitfall
When a Textual action's real work runs in a mocked `run_worker` (so its `finally`
that calls `release()` never executes), the lockfile stays held for the rest of
the test process and the next action is (correctly) refused. In such tests call
the release path explicitly (`app._release_bg_lock()`) between dispatched
actions, or initialise `_bg_busy=False`/`_bg_lock=None` on the `__new__`
instance.
