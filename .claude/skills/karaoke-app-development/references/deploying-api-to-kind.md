# Deploying the karaoke FastAPI backend to the kind cluster

Reference for containerising and deploying `karaoke.api` to the local
`kind-karaoke` cluster (namespace `karaoke`). Manifests live under `deploy/k8s`
(kustomize); `Makefile` targets are `k8s-build/load/deploy/seed-db/status/logs/
port-forward/undeploy`.

## Split the API along the host/cluster boundary (critical design rule)
The original single `src/karaoke/api.py` mixed read-only library endpoints with
`POST /api/play`, which shells out to `xdg-open`/`playerctl`. That endpoint
**cannot work in a pod** — no display server, no media player. Do not ship a
permanently-500 endpoint. Split into two apps:

- **`karaoke.api`** — read-only **library API** (tracks, lyrics, stats, logs).
  No desktop dependency, so it is safe to containerise. `/health` should probe
  the SQLite DB so a bad volume mount surfaces as an unhealthy pod, not empty
  listings. Bind `0.0.0.0` (honour `KARAOKE_API_HOST`/`KARAOKE_API_PORT`).
- **`karaoke.ctrl_api`** — host-side **control API** owning `POST /api/play`
  (`open_song_url`). Binds loopback (`127.0.0.1`) only, since it spawns local
  processes. Runs on the desktop via `karaoke-ctrl-api` / `make ctrl-api`.

Guard tests worth keeping: assert `/api/play` is **absent (404)** from the
library app's routes, and assert the library module does not import
`open_song_url` — this prevents re-introducing a desktop dependency into the image.

## Pitfall: `config._load_dotenv` crashes on non-`src/` layouts (real portability bug)
`Settings.load()` runs at import time and calls `_load_dotenv()`, which used
`Path(__file__).resolve().parents[3]` unconditionally to find the project root.
That assumes the `repo/src/karaoke/config.py` checkout layout. In a container
(`/app/karaoke/config.py`) or site-packages/wheel install there are fewer than
4 parents, so it raises `IndexError: 3` and the pod CrashLoopBackOffs at import.
Fix: guard the index (`parents = Path(__file__).resolve().parents; if
len(parents) > 3: candidates.append(parents[3] / ".env")`). Keep the regression
test in `tests/test_config.py` that imports the package from a shallow
(2-level) copied layout in a subprocess.

## Image build: docker bridge has no DNS on this host
On Tina's machine the default docker bridge network cannot resolve `pypi.org`
during `docker build` (`Temporary failure in name resolution`), even though the
host and `docker run --network=host`/`--dns=1.1.1.1` both resolve fine. Build
with `docker build --network=host ...` (baked into `make k8s-build`). Keep the
image slim: install only `fastapi`, `uvicorn[standard]`, `requests`, `rich` —
the library API's import chain is just `config`/`lyrics`/`localcache`, so none
of the heavy ML deps (torch/whisper/librosa) are needed.

## Stateful DB: copy a consistent snapshot into a PVC
The pod reads `KARAOKE_DATA_DIR=/data/karaoke.db` from a `ReadWriteOnce` PVC
(kind's default `local-path` StorageClass). Use `Recreate` deploy strategy (not
RollingUpdate) so a rollout never binds the RWO volume twice, and never run >1
replica against SQLite. Seed with `scripts/seed_db.sh`: it makes a **consistent**
snapshot via `sqlite3 "$DB" ".backup '$TMP'"` (falls back to `cp`), `kubectl cp`s
it to `/data/karaoke.db`, then verifies row counts inside the pod. The API
serves a **point-in-time snapshot** — re-run `make k8s-seed-db` after local
indexing to refresh it.

## Do NOT recreate an existing cluster that hosts OpenSearch
The `kind-karaoke` cluster already runs the OpenSearch helm release. Recreating
it to add a NodePort→host port mapping would destroy that release. Instead:
- For host access to the API now, use `make k8s-port-forward`
  (`kubectl -n karaoke port-forward svc/karaoke-api 8080:8000`).
- Add the `30800→8080` mapping to `deploy/kind-cluster.yaml` for *future* fresh
  clusters only (extraPortMappings only take effect at cluster creation).
Per the architecture rule, OpenSearch is a *derived* index rebuildable from
SQLite via `karaoke-vector-index`, but there is no reason to pay that cost when
a port-forward suffices.

## CI (`.github/workflows/api.yml`)
Runs API/config tests → builds image → spins an ephemeral kind cluster via
`helm/kind-action` → `kubectl apply -k deploy/k8s` → `set image` to the CI tag →
`rollout status` → smoke-tests `/health`, `/api/tracks` (empty DB ⇒ `[]` is
valid), and asserts `/api/play` → 404. Install only the web deps in CI, not the
full project set. No `github.event.*` untrusted input is interpolated, so the
Actions-injection warning is a false positive there.
