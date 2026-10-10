# `get_synced()` Signature & FastAPI Backend Development

## `get_synced()` Signature Requirements

In `src/karaoke/player.py`, `get_synced()` expects a `SongRef` dataclass instance as its first positional parameter:

```python
def get_synced(
    ref: SongRef,
    *,
    use_cache: bool = True,
    transcribe: bool = False,
    force_transcribe: bool = False,
    lyrics_file: Optional[str] = None,
    stats_mode: Optional[str] = None,
) -> Lyrics:
```

### Pitfall
Calling `get_synced(artist, title)` or `get_synced(pb.artist, pb.title)` with bare strings causes a `TypeError` when `get_synced` attempts to access `ref.artist` or `ref.title`.

### Fix
Construct a `SongRef` instance before calling `get_synced`:

```python
ref = SongRef(
    artist=pb.artist,
    title=pb.title,
    duration=pb.duration_ms / 1000.0 if pb.duration_ms else None,
    source="spotify",
)
ly = get_synced(ref, use_cache=use_cache, stats_mode="spotify")
```

---

## FastAPI Backend Architecture for `karaoke`

The FastAPI backend (`src/karaoke/api.py`) exposes SQLite library data, stats, logs, and playback actions over REST.

### Endpoints
- `GET /health` / `GET /api/health`: Health status check.
- `GET /api/tracks`: List tracks in SQLite library with optional `?q=` search query.
- `GET /api/tracks/{track_id}`: Track details, sources, and approved lyrics.
- `POST /api/play`: Launch media player/browser using `open_song_url(url, kind)`.
- `GET /api/stats`: Local cache play and discovery statistics.
- `GET /api/logs`: Recent log lines from `karaoke.log`.

### Dependency & Script setup
In `pyproject.toml`:
```toml
[project.optional-dependencies]
api = ["fastapi>=0.110.0", "uvicorn[standard]>=0.28.0"]

[project.scripts]
karaoke-api = "karaoke.api:main"
```

In `Makefile`:
```makefile
api: ## Launch the FastAPI backend server for TUI/library
	$(PYTHON) -m karaoke.api
```

### Unit Testing FastAPI with `monkeypatch`
When testing endpoints that use `localcache.connect()`, capture a reference to the real function first to avoid infinite recursion:

```python
from karaoke import localcache

_real_connect = localcache.connect

def test_list_tracks(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    conn = _real_connect(db_path)
    monkeypatch.setattr("karaoke.api.localcache.connect", lambda db_p=None: _real_connect(db_p or db_path))
    ...
```
