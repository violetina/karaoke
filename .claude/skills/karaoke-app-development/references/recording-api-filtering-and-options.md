# Recording API Filters and Options

Query parameters and mutation endpoints for record-mode sessions on the Library API (`karaoke.api`) and Control API (`karaoke.ctrl_api`).

## Library API (`karaoke.api`)

### `GET /api/recordings`
Query parameters for filtering, search and pagination:
- `status` (string, optional): Filter by status (`recording`, `complete`, `analysed`, `discarded`). Accepts comma-separated values (e.g. `status=complete,analysed`).
- `source` (string, optional): Substring filter on audio source (e.g. `source=alsa` or `source=monitor`).
- `has_marks` (boolean, optional): Filter sessions with (`true`) or without (`false`) identification marks.
- `identified_only` (boolean, optional): Filter sessions with at least one identified track (`true`) or zero (`false`).
- `keep_audio` (boolean, optional): Filter by `keep_audio` flag (`true`/`false`).
- `since` (float, optional): Filter sessions started at or after Unix timestamp.
- `until` (float, optional): Filter sessions started at or before Unix timestamp.
- `q` (string, optional): Case-insensitive keyword search in `note` or `source`.
- `limit` (int, default 100, 1..1000) & `offset` (int, default 0): Pagination parameters.

### `GET /api/recordings/{recording_id}`
Query parameters:
- `confident_only` (boolean, default false): When true, includes only tracks with corroborated/confident boundary estimates in the `tracks` array.
- `min_marks` (int, optional): Minimum mark count for a track segment to be included.

### `PATCH /api/recordings/{recording_id}`
JSON Body:
- `note` (string, optional): Update user note/description for the recording session.
- `keep_audio` (boolean, optional): Update retention flag (`keep_audio`).

## Control API (`karaoke.ctrl_api`)

### `POST /api/record/start`
JSON Body:
- `source` (string, optional): PipeWire source (defaults to playing sink monitor).
- `keep_audio` (boolean, default false): Persist audio after session ends.
- `note` (string, optional): Attach an initial description/note to the session row on start.
