# End-to-End Recording Processing API Testing & Fixture Patterns

When testing recording management and audio processing APIs over FastAPI `TestClient` or `ApiClient`:

## 1. Pytest Fixture Order
Place database fixtures **first** in the test signature:
```python
def test_e2e_recording_lifecycle(test_db_and_recording, lib_client, ctrl_client):
    ...
```
This ensures monkeypatches on `localcache.connect` apply before Starlette's `TestClient` mounts or triggers app handlers.

## 2. None Query Parameter Stripping in TestClient
Starlette `TestClient` treats `None` dictionary values in `params` as the literal string `"None"` (e.g. `?status=None`), causing FastAPI to parse `status="None"` and SQL queries to mismatch (`WHERE status = 'None'`).

When mocking `ApiClient._http_get` to delegate to `TestClient`, strip `None` entries from query parameters first:
```python
clean_params = {k: v for k, v in (params or {}).items() if v is not None}
resp = lib_client.get(path, params=clean_params)
```

## 3. Recording Processing API Lifecycle Flow
- **List / Search**: `GET /api/recordings?q=query&limit=10`
- **Session Detail**: `GET /api/recordings/{id}?confident_only=true`
- **Metadata Update**: `PATCH /api/recordings/{id}` (`note`, `keep_audio`)
- **Live Capture**: `POST /api/record/start`, `GET /api/record/status`, `POST /api/record/stop`
- **Decompile / Analyse**: `POST /api/recordings/{id}/analyse?keep=true`
- **Track Audio & Page**: `GET /recordings/{id}/tracks/{idx}`, `GET /api/recordings/{id}/tracks/{idx}/audio`
- **Discard Audio**: `DELETE /api/recordings/{id}/audio`

## 4. Live Verification Script
Use `scripts/e2e_test_recordings_api.py` to verify the full lifecycle against live running services (`karaoke-api` on `:8000` and `karaoke-ctrl-api` on `:8765`).
