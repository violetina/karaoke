# Control API: players, folder scan, audio cut

The host-side **control API** (`karaoke.ctrl_api`, default `http://localhost:8765`)
exposes everything that needs the desktop session — MPRIS players (`playerctl`),
`ffmpeg`, and `songrec`. It is the counterpart to the cluster-deployable
read-only [library API](api.md). Every action the TUI performs is available here
over HTTP, so a future web UI drives the same backend with no new server code.

Start it with `make ctrl-api` (or the `karaoke-ctrl-api` systemd unit). It binds
loopback only, since it spawns local processes.

## Player controls (MPRIS)

```
GET  /api/players                sessions: all names, which are playing, the active one
GET  /api/players/current        current track + status + position (?player=<mpris_name>)
POST /api/players/play-pause     toggle play/pause      body: {"player": "<name>"?}
POST /api/players/pause          pause (never resumes)  body: {"player": "<name>"?}
POST /api/players/next           next track             body: {"player": "<name>"?}
POST /api/players/previous       previous track         body: {"player": "<name>"?}
POST /api/players/seek           relative seek          body: {"offset_s": 5.0, "player": "<name>"?}
```

`player` is optional everywhere: omit it and playerctl targets the player that is
actually **Playing** (see `playing_player()`), which avoids a paused kiosk tab
shadowing the real source. A control that no player can service returns **409**
rather than a silent success.

`GET /api/players/current` returns the cleaned metadata (browser titles
normalised, YouTube/YT Music recognised), playback `status`, `position_s`, and
`art_url`.

## Folder scan (ingest a music library)

```
POST /api/scan/folder
  body: {
    "dir": "/home/tina/Music",
    "use_fingerprint": true,     # songrec/Shazam when tags are missing or wrong
    "classify_audio": true,      # key/BPM + CLAP embedding + zero-shot genre
    "resolve_streaming": true,   # resolve YouTube + Spotify links
    "dry_run": false,
    "limit": null
  }
```

A **dry run** returns the enrichment preview synchronously (`status: "preview"`
plus per-file `items`). A real ingest can run long over a large library, so it is
dispatched to the background (`status: "accepted"`); poll the library API's
`/api/tracks` for the new rows. The same logic backs the `karaoke-folder-scan`
CLI and `make folder-scan DIR=...`.

Pipeline per file (`karaoke.folder_scan.scan_and_ingest_folder`):

1. **Tags** via mutagen (`tags.extract_tags`).
2. **Fingerprint** via `songrec` when the tags are missing/weak
   (`identify.identify_file_fingerprint` — 10s ffmpeg slice → Shazam).
3. **Classify** key/BPM/energy (`analyze.analyze_audio`), CLAP embedding
   (`clap_vector.embed_audio`), zero-shot genre (`genre.classify`).
4. **Resolve** YouTube (`youtube.search` + `source_select.select_best_source`)
   and Spotify (`SpotifyClient.search_track`).
5. **Ingest** into SQLite (`tracks`, `sources`, `lyrics`, `track_analysis`,
   `track_genre`).

## Audio cut

```
POST /api/audio/cut
  body: {"file_path": "/path/in.webm", "start_s": 30.0, "duration_s": 45.0,
         "output_path": "/tmp/out.wav"?}
```

Cuts `[start_s, start_s+duration_s)` out of a file with ffmpeg (stereo, 44.1kHz)
and returns the `output_path`. Omit `output_path` for a temp file. Missing input
is **400**, a missing ffmpeg is **503**.

## Sample & record (existing)

```
POST   /api/sample                       key/BPM from a short live excerpt (sync)
POST   /api/record/start                 begin unattended capture (body: source?, keep_audio?, note?)
POST   /api/record/stop                  stop one or all captures
GET    /api/record/status                captures running in this process
POST   /api/recordings/{id}/analyse      decompile a recording into the DB
DELETE /api/recordings/{id}/audio        drop audio, keep markers
```

See [Record mode](modes/record.md) for the capture/decompile detail.

## Related

- [Library API](api.md) — read-only tracks, lyrics, stats, recordings inspection
- [Folder scan CLI](makefile-targets.md) — `make folder-scan DIR=...`
