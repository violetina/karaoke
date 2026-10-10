---
name: karaoke-app-development
description: Use when developing the `karaoke` terminal application.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [karaoke, python, playerctl, mpris, whisper, lyrics]
    related_skills: [media:karaoke-lyrics-platform]
---

# `karaoke` Application Development Workflow

This skill documents the development, testing, and debugging workflow for the local `karaoke` terminal application.

## Architecture: SQLite vs OpenSearch
- **SQLite (`~/.local/share/karaoke/karaoke.db`)** is the operational source of truth. It handles tracks, source URLs, lyrics, stats, and backfill gaps. Playback and UI must rely exclusively on SQLite to ensure offline capability.
- **OpenSearch** is an optional, *derived* index used only for semantic/vector search and machine learning features. It is rebuilt from SQLite using the `karaoke-vector-index` command.

## Git Worktree Workflow
To keep the `main` branch clean and stable, this project strictly uses Git worktrees for feature development:
- `/home/tina/karaoke` tracks `main` (stable, testing, e2e). **Do not develop new features directly here.**
- Use sibling worktrees for development (e.g., `/home/tina/karaoke-tui` for `feat/tui`, `/home/tina/karaoke-datamodel` for `feat/data-model-refactor`).
- **Environment:** Symlink the virtual environment from the main repo to the worktree (`ln -s /home/tina/karaoke/.venv /home/tina/karaoke-<feature>/.venv`) so `source .venv/bin/activate` works consistently. Because the shared venv may have an editable install pointing at `/home/tina/karaoke/src`, run worktree commands with `PYTHONPATH=src` or use Makefile targets that do so; otherwise tests can accidentally import code from `main` instead of the worktree.
- **Flow:** `cd` to the worktree -> implement -> test with `PYTHONPATH=src python -m pytest` or Makefile targets -> commit -> `gh pr create` -> merge -> `cd ~/karaoke` -> `git pull`.

### Worktree Lifecycle, Rebasing, & PR Merges
For a detailed guide on handling merge cosmetic failures, tracking down post-PR snapshot drift, and robust cleanup commands, consult the companion guides:
- **`references/git-worktree-pr-merge-lifecycle.md`** — Handles the `gh pr merge` checkout conflict and local-branch deletion.
- **`references/git-worktree-merge-verification.md`** — Verifies branch contents and state.
- **`references/admin-tui-and-backend-service-pitfalls.md`** — Guidelines and pitfalls for dedicated admin TUI development, including external process launching and Textual App.exit() overrides.
- **`references/search-multifold-and-source-resolution.md`** — Design patterns and code architecture for tokenized multi-word search coverage, cross-field fallbacks, dynamic YouTube source healing, and Chrome CDP parameter quote-escaping.

#### Pitfall: Rebasing Legacy Worktrees (Squash-Merged Parents)
If a legacy feature worktree has been left un-updated, and its older base commits have since been squash-merged into `main` under a different hash, a standard `git rebase main` will try to re-apply those commits and hit conflicts.
- **Fix:** Avoid rebasing the old branch. Create a clean branch from `origin/main` and cherry-pick only the unique, non-merged commits to completely bypass the conflict:
  ```bash
  git checkout -b feat/feature-update origin/main
  git cherry-pick <unique-sha-1> <unique-sha-2>
  ```

#### Pitfall: Complex Merge Conflicts and User Preference
During rebasing, if merge conflicts are complex or time-consuming, the user prefers to `git reset --hard origin/main` the worktree and restart development from a clean `main` branch. Avoid spending excessive time on manual conflict resolution if a clean start is faster.

#### Pitfall: `patch` Tool Limitations and Alternatives
The `patch` tool is highly sensitive to whitespace and escaping. For complex code modifications or resolving merge conflicts, prefer reading the entire file with `read_file` and then writing the modified content back with `write_file` to ensure correct application and avoid repeated failures.

#### Clarification: Admin TUI and Existing TUI
The work on the "admin TUI" involves integrating administrative features (like monitoring Celery workers and queues) into the existing `karaoke` TUI application, rather than creating a separate, standalone admin TUI application.


## Core Workflow for Adding Lyrics

When a song lacks lyrics, they can be staged and added using either the end-to-end CLI workflow or the FastAPI Staging API.

### A. CLI Workflow (Local/Terminal)

1.  **Stage from a Source:** Attempt to stage lyrics from a source like YouTube captions:
    ```bash
    karaoke-stage youtube "https://youtube.com/watch?v=..."
    ```
2.  **Review the Staged Lyrics:** Always inspect the staged content. YouTube auto-captions can be low quality or completely wrong (e.g., just `[Music]`).
    ```bash
    karaoke-stage show <ID>
    ```
3.  **Approve or Reject:**
    - If the lyrics are good, approve them into the local cache as plain lyrics:
      ```bash
      karaoke-stage approve <ID>
      ```
    - If they are bad, reject them to keep the cache clean:
      ```bash
      karaoke-stage reject <ID>
      ```
4.  **Find Lyrics Manually (if staging fails):** If no good source is available for staging, find the plain text lyrics from a reliable web source (e.g., Genius, AZLyrics).
5.  **Upgrade to Synced Lyrics:** Use the `--lyrics-file` feature to align an audio file with the plain text, generating synced lyrics.
    - First, download the audio:
      ```bash
      karaoke-yt --download "https://youtube.com/watch?v=..."
      ```
    - Then, force transcription using your text file:
      ```bash
      karaoke -f ~/.local/share/karaoke/youtube/<VIDEO_ID>.webm --force-transcribe --lyrics-file /path/to/lyrics.txt
      ```
6.  **Verify:** Run the `karaoke` command with `--print` to confirm the synced LRC lyrics are now in the cache.

### A2. TUI Auto-load (scan mode)

In scan mode, when the TUI syncs a YouTube/YT Music tab with no cached synced lyrics, `KaraokeTui._background_autoload_captions` (dispatched via `run_worker(..., thread=True)`) auto-stages the video's captions and, if they carry real timing (`caption_is_synced()` on the `source_kind` — `_synced`/`_enhanced`), auto-approves them and re-polls so the timeline loads immediately. Untimed captions stay in the staging queue. All UI mutations from the worker thread MUST go through `self.call_from_thread(...)`. Each video ID is attempted once per session (`self._autoloaded`).

### B. REST Staging API Workflow (Asynchronous/Multi-Client)

For TUIs, web interfaces, or remote clients, use the Staging API running on the library backend (`karaoke.api` at port 8000):

1.  **Stage from YouTube:**
    - Request: `POST /api/staging/youtube` with `{"url": "https://youtube.com/watch?v=..."}`
    - Response: `202 Accepted` immediately, with the YouTube caption-staging job dispatched to the background task supervisor.
2.  **Stage from Whisper Transcription:**
    - Request: `POST /api/staging/whisper` with `{"file_path": "/path/to/audio.mp3", "artist": "ArtistName", "title": "SongTitle"}`
    - Response: `202 Accepted` immediately, with transcription and staging dispatched to the background task supervisor.
3.  **Database Storage:**
    - Both tasks store the resulting candidates inside the `staged_lyrics` table in `staged` status, where they are reviewable via `karaoke-stage` or DB queries, and can be approved into the canonical lyrics cache.

## Pitfalls and Debugging

- **Textual TUI Development:**
  - **Web Mode (`textual-serve`) Sizing Delays:** In browser mode, widget `content_size` often reports `0` briefly due to layout latency. Background workers (like cover art generation) checking `cols < 4` will silently abort. (See `references/textual-serve-web-mode.md`).
  - **Blocking the UI:** Never use `subprocess.run()` for long-running or detached external processes (like `xdg-open`). It will freeze the TUI. Use `subprocess.Popen(..., stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)` instead.
  - **DataTable Keys:** When backing a `DataTable` with database rows that might have `None` values (e.g., `LEFT JOIN` results), avoid using those values as Textual `RowKey`s (which causes `DuplicateKey` crashes). Instead, maintain a parallel Python list (`self._song_data`) and use `table.cursor_row` as an index to retrieve the data on selection.
  - **DataTable Row Selection Interception:** Textual's `DataTable` consumes `Enter` key presses internally, posting a `DataTable.RowSelected` event rather than letting them bubble up to app-level key bindings. Always implement `on_data_table_row_selected(self, event: DataTable.RowSelected)` to explicitly forward row selections to your target handler (e.g., `self.action_select()`).
  - **Scrollable Static Panels:** Fixed-height widgets (like `#lyrics`) do not enable overflow scrolling by default, causing long plain-text lyrics or previews to truncate. Always configure `overflow-y: auto;` or `overflow-y: scroll;` in the CSS style of text panels to keep them scrollable.
  - **Admin TUI Background Pipelines:** Long-running operations (like folder scanning and individual recording decompilation/analysis triggered by hitting Enter on the `recordings-table` list) must run natively in the Admin TUI's background worker threads (`self.run_worker(..., thread=True)`). They must hold `_acquire_bg_lock()` to lock concurrent operations, use `_mark_task_started()` / `_mark_task_finished()` to log task status in the log column, and invoke UI updates on completion via `self.call_from_thread(...)` to ensure the interface stays responsive and freeze-free.
  - **NoMatches in Interval Pollers:** Interval or background polling tasks (like `_poll_detection`) querying specific layout widgets via `query_one` will crash with `NoMatches` when secondary screens or dialogs (e.g., `StatsScreen` or a help screen) are pushed over the main/default screen. **Fix:** Wrap `query_one` calls in polling loops with a `try ... except Exception:` block or check presence first using `self.query` to avoid uncaught screen-lookup crashes.
  - **Logging:** Use `karaoke.logger` (writing to `~/.local/share/karaoke/logs/karaoke.log`) for TUI debugging rather than `print()`, which corrupts the terminal display. Log levels (err, info, full) can be cycled to reveal background task details.

- **`playerctl` Metadata Issues:**
  - **Stale Browser Metadata:** Browsers often report stale or truncated metadata via MPRIS. **Do not trust MPRIS artist/title for browsers.** Instead, extract the `url` from MPRIS, and use it as the primary key to look up canonical metadata in the `sources`/`tracks` SQLite tables.
  - **Ambiguity:** If multiple media players are active, `playerctl` may send commands to the wrong one, resulting in "No player could handle this command". The `karaoke-player` script has a `--player <name>` flag to target a specific instance (e.g., `firefox.instance_1234`). Use `playerctl --list-all` to find the correct name.
  - **Incorrect Title Parsing:** The application relies on `playerctl` metadata. If the metadata is wrong (e.g., showing a different song title than the video's actual title), all downstream lookups will fail. The application has `--artist` and `--title` override flags for debugging this, but the root cause is external to the app.

- **Browser CDP & Kiosk Playback:**
  - **CDP Navigation and Single Quotes:** When navigating Google Chrome via CDP evaluation blocks (`Runtime.evaluate`), search or video URLs containing single quotes (e.g. `'` in artist/title search queries) can break the enclosing string literal (e.g. `window.location.href = '{url}';`) and cause JavaScript syntax errors. Always replace single quotes with escaped forms (`url.replace("'", "\\'")`) before evaluating javascript strings.
  - **Kiosk Spawning & Decoupled Lifecycle:** Kiosk Chrome runs under a systemd user unit (`karaoke-kiosk.service`) and is completely decoupled from TUI startup (such as `make tui`). To restart the kiosk player, call the Control API (`POST /api/players/window/restart`) or use the Admin TUI shortcut (`K`), which attempts a clean `systemctl --user restart karaoke-kiosk.service` or manually kills Chrome processes using port `9222` and spawns a fresh background browser process.

- **YouTube / yt-dlp Requirements:**
  - **JS Challenges (EJS remote components):** Modern YouTube requires solving JS **signature / `n` challenges** to expose real audio/video formats. If unsolved, yt-dlp only sees image storyboards and fails with **"Requested format is not available"** (this silently breaks ALL downloads, caption probing, and post-processing — not just one video). Two things are needed: (1) a JS runtime like `deno` on `PATH` (`curl -fsSL https://deno.land/install.sh | sh`), AND (2) yt-dlp's **EJS remote-component solver scripts enabled** via `--remote-components ejs:github` (API opt: `remote_components=["ejs:github"]`). This project wires it in through `youtube._ejs_opts()` (applied in `fetch_metadata` and `upgrade_track`), env-overridable via `KARAOKE_YTDLP_REMOTE_COMPONENTS` (default `ejs:github`; empty disables). Debug manually with `yt-dlp --list-formats <url> --remote-components ejs:github` — if audio formats appear, the fix is working. Also keep yt-dlp current (`pip install -U yt-dlp`).
  - **Batch Downloads:** Always ensure `*.webm` (or target audio formats) are in `.gitignore` *before* testing batch download scripts (like backfills or indexers) to avoid accidentally committing massive files to the repository.

- **Missing YouTube Captions:** Many videos, especially instrumental ones or older uploads, have no subtitles or auto-captions. `karaoke-stage` will report "no YouTube captions found". In this case, you must fall back to finding lyrics manually. Use `yt-dlp --list-subs <URL>` to confirm the available captions before attempting to stage them.

- **Folder Scan Engine & Metadata Fallbacks:** Local files frequently lack ID3 tags or carry generic placeholders. In addition to Video ID queries, Recording Session matching, and Shazam fingerprint fallbacks, a 5th-tier Filename-based Parser is implemented. When tags and fingerprints are missing, it splits the file stem on common separators (` - ` or `-`), cleans track-number prefixes (e.g. `07 `, `(01) `), and extracts fallback `artist` and `title` values to successfully scan untagged files with standard naming conventions.

- **Search Optimizations & Wildcard Workflow:**
  - **Smart Tokenized Fallbacks:** Library search handles multi-word query phrases gracefully by splitting queries into lowercase tokens, filtering out stop words, performing boundary-based matching, and falling back to a global cross-field match query over the combined track metadata (`artist + title + album`).
  - **Wildcard `*` Query:** Entering `*` in the search input wildcard-fetches all SQLite tracks containing approved lyrics. Combined with active genre and mood filters, this allows users to browse and narrow down the entire playable database in real time.
  - **OpenSearch Synonym Expansion:** Full-text BM25 search in OpenSearch leverage synonym-backed standard analyzers (`synonym_analyzer`) across six emotion/genre mappings (Rock, Melancholy, Joyful, Calm, Hip-Hop, Electronic) to resolve query vocabulary mismatches (e.g., matching `"sad"` to `"depressed"`).

- **Database Duplicate Tracks, Missing Sources, and YouTube Music Integration:**
  - **Duplicate Tracks:** Differences between Spotify simple artists and multi-artist lyric credits (e.g. `"Inpatient"` vs `"Inpatient, Ren & Chris Webby"`) create disjoint duplicates in SQLite, breaking lyric sync in Spotify mode. **Mitigation:** Run `make db-cleanup` to group, fuzzy-match, consolidate, and merge these automatically. Dedup is **fuzzy-title + duration-guarded**: two tracks merge only when the artist is compatible AND the cleaned titles match AND the durations are within `DURATION_TOLERANCE_S` (~4s, "same length == same song"). A *different length* is treated as a distinct version (edit/live/extended/remix) and is **allowed to coexist**; when either duration is unknown it only merges on a near-identical title (`TITLE_SIMILARITY_NO_DURATION`). This guards against real traps like artist/title-swapped rows (three different Linkin Park songs all stored with `title='Linkin Park'` but different durations). Merging keeps one canonical track that accumulates multiple video sources. Use `python scripts/db_cleanup.py --dry-run` to preview, `--dedup-only` to skip source/cache healing.
  - **Browser Title-Only Scan (YT Music Fallback):** Browser players (YT Music, YouTube tabs) often report an empty artist string (`xesam:artist = ""`) and only a formatted title. `resolve_lyrics` employs a fallback to query `tracks` by title-only, prioritizing matching tracks that carry approved synced lyrics.
  - **YouTube / YT Music Interchangeable Lookup:** Standard YouTube and YouTube Music use different host URLs but share the **exact same 11-character video ID**. `find_track_by_url()` extracts this video ID so that `youtube` and `youtube_music` source records are seamlessly interchangeable for offline lyric lookup.
  - **Default Browser Cookies Fallback & Missing `.env`**: Set `KARAOKE_COOKIES_FROM_BROWSER=firefox` (or `chrome`) in `.env` to make all YouTube staging, backfill, and timing upgrades authenticate with your browser session by default, bypassing 429 rate limits and unlocking Premium-quality audio. Note that the systemd unit files (under `deploy/systemd/*.service`) also define `Environment=KARAOKE_COOKIES_FROM_BROWSER=firefox` (or `chrome`) and must be updated to match the user's active browser to prevent Celery/background workers from running unauthenticated or failing with 429/age gates. If no `.env` file exists at the project root and systemd is unset, this fallback defaults to `None`, making all background/autoload requests anonymous.
  - **Stale `site-packages` Shadowing Editable Installs:** If TUI or CLI code changes are silently ignored during execution, check if a stale, static copy of the package exists inside `.venv/lib/python3.14/site-packages/karaoke` (which clobbers the editable layout in `src/`). See `references/stale-site-packages-shadowing.md` for diagnosis and resolution steps.
  - **Deterministic Source Priority**: When a track has both Spotify and YouTube sources, lists and TUI tables order sources (`youtube_music > youtube > http > spotify`) so Enter always opens a browser-openable link instead of depending on a running Spotify playerctl target.
  - **Automatic YT-to-YT Music Upgrades**: Starting a standard YouTube watch link in list/browse mode automatically upgrades and launches the URL as a `music.youtube.com` link for superior, music-focused audio playback.
  - **Recording analysis vector indexing**: `make recording-analyse ID=...` runs under `.venv-audio`. If it stores key/BPM but the status line lacks `+vector`, the audio venv likely lacks the OpenSearch stack. When `make audio-check` says the main `.venv` has the DSP stack, re-run with `PYTHONPATH=src .venv/bin/python -m karaoke.recording_worker --analyse ID --keep` to persist the recording audio vector too.

  ## Celery-based Post-Processing Workflow

  The post-processing pipeline for tasks like audio analysis, word-level lyric timing, and vector rebuilding is orchestrated using Celery with RabbitMQ as the broker. This enables robust, retryable background tasks with visibility via the Flower dashboard.

  ### Key Components:
  - **`karaoke-mq-forward.service`**: Systemd service to port-forward RabbitMQ from the Kind cluster to `localhost:5672` (AMQP) and `localhost:15672` (management UI).
  - **`karaoke-celery-worker.service`**: Systemd service running the Celery worker, consuming tasks from the `karaoke-postprocess-celery` queue.
  - **`karaoke-kiosk.service`**: Systemd service managing the Google Chrome kiosk window running YT Music, decoupled from `make tui`.
  - **`karaoke-celery-flower.service`**: Systemd service running the Flower dashboard at `http://127.0.0.1:5555`, providing real-time monitoring of tasks and workers.
  - **`karaoke.tasks.postprocess_track`**: The main Celery orchestrator task that takes an initial payload (`artist`, `title`, `url`) and chains together the following sub-tasks:
  - **`karaoke.tasks.resolve_track_id`**: Resolves the `track_id` and the most suitable YouTube URL, and determines which post-processing steps are pending.
    - **Automatic YouTube Source Resolution**: When postprocessing Spotify tracks or tracks with plain lyrics but no downloadable YouTube URL, the Celery pipeline automatically searches YouTube, registers it as a `youtube` source in SQLite, and proceeds with the download and Whisper lyric alignment. If the track itself is missing completely from SQLite, Celery auto-creates the track and source record to prevent the task from failing.
    - **Postprocess Gating Safety**: Tracks with plain lyrics needing sync (`"sync"` or `"timings"`) are safe to enqueue even if they lack a pre-downloaded source because of the automatic YouTube search resolution, while tracks with only `"analysis"` pending and no lyrics should still be skipped to avoid unnecessary searches.
    - **Genre Selector & Sorting Layout**: Place the genre filter (`#genre-select`) above the mood preset selector (`#mood-select`) and default the sort order to `"key"`. Populating filters dynamically from `track_genre` avoids hardcoding.
    - **Polling Query Guards**: Always wrap interval `self.query_one` lookups in a polite `try/except` guard to avoid `NoMatches` crashes during overlay transitions.
    - **Wildcard Event Routing**: Argo Events AMQP EventSource must listen to routing key `task.#` (not just `task.succeeded`) to capture all worker updates (started, retrying, failed) into the event ledger.
    - **Dedicated Source Resolution**: Use `scripts/fill_missing_sources.py` with optional `--limit` and `--force-all` flags to batch-resolve and fill missing YouTube link mappings in the local database.
  - **`karaoke.tasks.download_audio`**: Downloads the audio file using `yt-dlp` if not already cached.
  - **`karaoke.tasks.analyze_audio`**: Runs musical analysis (key, BPM, energy) and saves the results to the `track_analysis` table.
  - **`karaoke.tasks.upgrade_timings`**: Upgrades line-level synced lyrics to Enhanced LRC using YouTube json3 captions if available.
  - **`karaoke.tasks.sync_lyrics`**: Aligns plain lyrics to the downloaded audio using Whisper for word-level timings.
  - **`karaoke.tasks.rebuild_vectors`**: Rebuilds audio and lyric vectors (e.g., for OpenSearch indexing) if needed.

  ### Persistence and Retries:
  - **Task Results**: Celery task results are persisted by default to `~/.local/share/karaoke/celery-results.sqlite` using an SQLite backend, ensuring that completed/failed task metadata survives worker restarts.
  - **Retries**: Each sub-task is configured with `autoretry_for`, `retry_backoff`, `retry_jitter`, and `max_retries` to handle transient failures gracefully. For more details, refer to `docs/howto-post-processing.md`.
