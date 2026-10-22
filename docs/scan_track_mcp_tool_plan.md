# `scan_track` MCP tool — plan

**Status: done.** Implemented 2026-10-22 on PR #124 (`feat/mcp-scan-track`).

The short version: add one MCP tool that takes a track with no analysis, finds
it a playable source when it has none, and runs the existing post-processing
chain on it (key/BPM, chords, CLAP, lyric sync, search document). Everything it
needs already exists in `find_sources`, `postprocess_queue`,
`scripts/reprocess_tracks.py` and `sample_audio`. This is wiring, not new
analysis code.

## Why this exists

Seen live on 2026-10-22 with track **19203** (Carlos Santana — *Dance Sister
Dance (Baila Mi Hermana)*, *Moonflower*), playing in Chromium:

| Step | Result |
| --- | --- |
| `get_now_playing` | `library_match: null` — no key, BPM or energy |
| `suggest_next_tracks` (`flow`) | silently fell back to `harmonic`; ranked on BPM only |
| `suggest_next_tracks` (`acoustic`, `structural`) | empty — no CLAP vector, no chords |
| `get_track_chords` | `No v2 chord timeline stored yet ... (and audio is not cached on local disk)` |
| `reprocess_tracks.py --track-id 19203 --download --dry-run` | `no_audio_available` — the track has **no row in `sources`** |
| `make sample SECS=45 ...` | worked: F minor (conf 100%), 152 BPM, CLAP from the 45 s capture |

After the sample, `acoustic` and `flow` returned results. Chords are still
missing, because the sample path does not run harmony and there is no file to
run it on.

Getting there took a shell, three scripts and knowing which one applies. The DJ
(any MCP client) has no way to do it, so it has to fall back to BPM-only
suggestions without telling the user why.

## Tool contract

Add to `src/karaoke/mcp_server.py`, next to `get_track_chords`, following the
same style (`@dj_mcp.tool()`, Google-style docstring with `Args:`, returns a
JSON string via `json.dumps`).

```python
@dj_mcp.tool()
def scan_track(
    track_id: Optional[int] = None,
    artist: Optional[str] = None,
    title: Optional[str] = None,
    mode: str = "auto",          # "auto" | "queue" | "inline" | "sample"
    find_source: bool = True,
    sample_seconds: float = 45.0,
    dry_run: bool = False,
) -> str:
    """Analyse a track that is missing key/BPM, chords, CLAP or lyric sync.
    ...
    """
```

Track resolution: same as `get_track_chords` — `track_id`, else
`localcache.find_track_id(artist, title, conn)`. If neither resolves, return
`{"error": ...}`; do not create tracks.

### Steps

1. **What is missing.** `postprocess_queue.needs_postprocessing(track_id, conn)`
   plus `search.clap_vector_for(track_id) is None` (the same CLAP check
   `reprocess_tracks.process` uses). If nothing is missing, return
   `status: "complete"` and stop.
2. **Source.** If `find_source` and
   `not postprocess_queue.has_downloadable_source(track_id, conn)`, call
   `find_sources.resolve_and_save_source(track_id, conn)`. It uses the verified
   `select_best_source` picker and the duration/lyric-end plausibility gate, so
   a wrong video is not saved. Under `dry_run`, call `find_sources.find_for`
   directly and report the candidate without `localcache.add_track_source`.
3. **Analyse**, by `mode`:
   - `queue`: `postprocess_queue.publish_postprocess_task(artist, title, url,
     full=True)`. `full=True` is required — without it the chain does not
     download audio that only harmony needs (see `needs_audio` in
     `src/karaoke/tasks.py`). Returns immediately.
   - `inline`: run the per-track pipeline from `scripts/reprocess_tracks.py`
     (`audit_track(..., download=True)` then `process(...)`) for this one track,
     and return the `done` / `failed` step lists. Slow (download + CLAP + Whisper):
     only for clients that tolerate long calls.
   - `sample`: `sample_audio.sample_and_analyse(artist, title, seconds=sample_seconds)`.
     Only valid if `get_now_playing()` reports this same track as playing;
     otherwise return an error instead of recording the wrong song. Gives
     key/BPM/CLAP, **not** chords.
   - `auto` (default): `queue` if a downloadable source exists (after step 2);
     else `sample` if this track is the one playing now; else
     `status: "no_source"`.
4. **Return** JSON:

```json
{
  "track_id": 19203,
  "artist": "Carlos Santana",
  "title": "Dance Sister Dance (Baila Mi Hermana)",
  "missing_before": ["analysis", "chords", "clap", "vectors"],
  "source": {"url": "https://music.youtube.com/watch?v=...", "found_now": true},
  "mode_used": "queue",
  "status": "queued",
  "steps_done": [],
  "steps_failed": [],
  "note": "Call scan_track again (dry_run=true) to see what is still missing."
}
```

`status` is one of `complete`, `queued`, `analysed`, `sampled`, `no_source`,
`error`. Calling again with `dry_run=true` doubles as a progress check: it only
recomputes `missing_before` and the source.

### Refactor needed

`reprocess_tracks.audit_track` / `process` live in `scripts/`, which is not
importable from the package. For `inline` mode, move the per-track pieces
(`Candidate`, `candidates`, `decode`, `judge`, `audit_track`, `process`, and
their helpers) into `src/karaoke/reprocess.py` and make the script a thin CLI
over it. Keep the script's flags and output identical — `tests/test_reprocess_tracks.py`
must pass unchanged apart from the import path.

Do **not** shell out to `make` or the scripts from the MCP server.

## Guard rails

- `find_source` writes a row to `sources`. Nothing else in the tool deletes or
  overwrites data. `reprocess_tracks.process` already merges harmony rather than
  replacing it — keep that.
- `sample` records the desktop audio monitor. Never start it unless
  `get_now_playing()` resolves to the same `track_id`.
- `inline` must hold the same `postprocess_queue.claim(artist, title, conn)` the
  worker uses and `release` it in `finally`, so a queued job and an inline run
  never process the same song at once.
- CLAP needs torch/transformers: check `clap_vector.available()` and report
  `clap` under `steps_failed` with a reason rather than raising.
- Close the DB connection in `finally`, as the other tools do.

## Related fix (same PR, small)

`suggest_next_tracks` with `strategy="flow"` silently returned
`"strategy": "harmonic"` for track 19203, and `acoustic` / `structural` returned
an empty list with no reason. When the seed lacks the data a strategy needs, add
a `"note"` saying what is missing and suggest `scan_track`, so the DJ can tell
the user why rather than presenting BPM-only matches as real suggestions.

## Tests

In `tests/test_mcp_server.py` (reuse its `_no_live_ytmusic` fixture style; no
network, no RabbitMQ, no audio capture):

- `scan_track` is registered (`test_mcp_server_definition`).
- Complete track → `status: "complete"`, nothing called.
- No source, `find_sources.resolve_and_save_source` monkeypatched to return a URL
  → `publish_postprocess_task` called once with `full=True`.
- `dry_run=True` → `add_track_source` and `publish_postprocess_task` never called.
- `mode="sample"` while `get_now_playing` reports another track → error, and
  `sample_and_analyse` not called.
- No source found and track not playing → `status: "no_source"`.
- `suggest_next_tracks` on a seed with no CLAP vector returns a `note`.

## Docs

- Add `scan_track` to the tool list in `docs/mcp-server.md`.
- Add one line to the server `instructions` string in `mcp_server.py` so clients
  know to call `scan_track` when a track has no key/chords/CLAP.
- Mark this plan **done** at the top, with the PR number.
- `make docs-audit` and `make test` green.
