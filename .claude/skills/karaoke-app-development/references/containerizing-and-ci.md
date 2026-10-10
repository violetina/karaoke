# Containerizing the karaoke app + CI dependency traps

Lessons from splitting the FastAPI backend and deploying it to the local
`kind-karaoke` cluster.

## Split along the host/cluster boundary FIRST

Not every endpoint can be containerized. `POST /api/play` calls
`open_song_url()` → `xdg-open`/`playerctl`, which needs a display server and a
media player. In a pod it can only ever return 500.

Rather than shipping a permanently-broken endpoint, split the service:

- `karaoke.api` — **library API**: read-only over SQLite (tracks, lyrics,
  stats, logs). Deployable. Binds `0.0.0.0`.
- `karaoke.ctrl_api` — **control API**: owns playback. Runs on the desktop
  session, binds loopback only (it spawns local processes).

Add a test asserting the playback route is *absent* from the deployable app
(`/api/play` → 404, and `"/api/play" not in {r.path for r in app.routes}`) so it
cannot creep back into the image.

## Pitfall: a shared/symlinked venv hides missing CI dependencies

The worktree pattern symlinks `.venv` from the main repo, so the dev venv has
*every* optional dependency. CI installs only what the workflow lists. A module
that transitively imports an extra will pass locally and fail in CI at
collection time.

Real instance: `ctrl_api` did `from .browse import open_song_url`, and
`browse.py` imports `textual` at module level → CI died with
`ModuleNotFoundError: No module named 'textual'` while local runs were green.

**Fix the import graph, not the CI dependency list.** `open_song_url` is about
launching URLs, not about the TUI, so it moved to `karaoke/player_open.py` and
`browse.py` re-exports it for backwards compatibility.

Regression test — import the deployable modules with the TUI stack blocked:

```python
script = ("import sys; sys.modules['textual'] = None;"
          "import karaoke.api, karaoke.ctrl_api; print('OK')")
result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                        text=True, env={"PYTHONPATH": str(src), "PATH": ""})
assert result.returncode == 0, result.stderr
```

**Always check `gh pr checks` before merging** — "it seems to work" locally is
exactly the condition under which this bug hides.

## Pitfall: `parents[N]` path indexing breaks in a container

`config._load_dotenv` did `Path(__file__).resolve().parents[3]`, assuming the
source layout `<repo>/src/karaoke/config.py`. In the image the package lives at
`/app/karaoke`, so the index raised `IndexError` at import and the pod entered
CrashLoopBackOff on first boot.

Guard any parent-index walk:

```python
parents = Path(__file__).resolve().parents
if len(parents) > 3:
    candidates.append(parents[3] / ".env")
```

## Getting the SQLite library into the cluster

`KARAOKE_DATA_DIR` is the hook — point it at a mounted volume (`/data`) and
`settings.local_db` follows.

- 1Gi PVC on kind's default `local-path` StorageClass.
- Deployment uses `strategy: Recreate` — SQLite on a RWO volume must never have
  two writers, and a rolling update would briefly bind the volume twice.
- Seed with a **consistent snapshot**, not a plain copy:
  `sqlite3 "$DB" ".backup '$TMP'"` then `kubectl cp`. The API serves a
  point-in-time snapshot; re-run the seed after local indexing.
- `/health` should probe the DB (`SELECT 1`) so a bad volume mount shows up as
  an unhealthy pod rather than as silently empty track listings.

## Don't recreate a cluster to add a port

Adding an `extraPortMapping` to `kind-cluster.yaml` only takes effect on a
freshly created cluster, and recreating destroys anything already deployed
there (e.g. the OpenSearch release). For an existing cluster use
`kubectl port-forward svc/<svc> 8080:8000` instead, and leave the mapping in the
config file for future fresh clusters.
