# YouTube Music Playlist Sync, CDP Multilingual Interstitial Bypass, and Folder Scan Progress

Lessons and reference patterns for YouTube Music integration, Chrome CDP modal handling, and live TUI progress routing.

## 1. YouTube Music Library & Playlist Sync (`ytmusicapi`)

### Architecture
- **CLI / Backend Utility:** `karaoke-ytmusic-playlist` (`src/karaoke/ytmusic_playlist.py` & `src/karaoke/ytmusic_client.py`).
- **Authentication Discovery:**
  The `YTMusicClient` resolves authentication in order:
  1. Env vars `KARAOKE_YTMUSIC_AUTH` or `YTMUSIC_AUTH`
  2. `~/.config/karaoke/ytmusic_auth.json` or `~/.local/share/karaoke/ytmusic_auth.json`
  3. `~/.config/karaoke/browser.json` or `oauth.json`
  4. Unauthenticated fallback (can search songs and fetch public playlists).

### Browser Header Extraction & Setup Flow
Running `karaoke-ytmusic-playlist --setup-auth` prompts the user interactively using `ytmusicapi.setup(filepath=...)`.
To extract headers from Chrome/Firefox:
1. Open `https://music.youtube.com` (logged in).
2. Press `F12` / `Ctrl+Shift+I` -> Network tab (Netwerk).
3. Filter by `browse` and click any song/playlist.
4. Right-click the request -> Copy -> Copy request headers (Dutch: "Aanvraagheaders kopiëren" / "Copy as cURL (POSIX)").
5. Paste directly into the interactive prompt.

### Playlist Synchronization
- **Exporting Karaoke Tracks:** `karaoke-ytmusic-playlist --sync [--name "Playlist Name"]` filters SQLite tracks with approved synced lyrics, extracts YouTube video IDs (or searches for missing ones), and syncs them to the user's account without duplicates.
- **Importing Playlists:** `karaoke-ytmusic-playlist --import-playlist <PLAYLIST_ID>` downloads tracks into SQLite as `youtube_music` sources and queries LRCLIB for synced lyrics.

---

## 2. CDP Multilingual Parental Advisory & Modal Auto-Dismissal

### Why Interstitials Stall Kiosk Playback
When navigating to explicit or sensitive tracks (e.g. Slayer - *Raining Blood*), YouTube Music overlays a sensitive content warning modal:
- In English: *"The following content may contain sensitive topics..."* with buttons like **"I understand and wish to proceed"**, **"Proceed"**, **"Exit app"**, or **"Play anyway"**.
- In Dutch (`nl-NL`): *"Gevoelige content"* with buttons like **"Ik begrijp het en wil doorgaan"**, **"App afsluiten" / "App sluiten"**, **"Doorgaan"**, or **"Toch afspelen"**.
- Often, these are chained (e.g., clicking "App afsluiten" prompts a second confirmation).

### The Automated Solution (`src/karaoke/player_open.py`)
In `dismiss_kiosk_dialogs()`:
1. First sends `Page.handleJavaScriptDialog` (`accept: True`).
2. Evaluates `_DISMISS_DIALOGS_JS` using `Runtime.evaluate`:
   - Checks selectors (`button[aria-label*="proceed" i]`, `yt-button-renderer#confirm-button button`, `ytmusic-dialog-renderer button`, `tp-yt-paper-dialog button`, `[role="dialog"] button`, `button.yt-spec-button-shape-next--filled`).
   - Checks multilingual text keywords:
     - Dutch: `begrijp`, `doorgaan`, `verdergaan`, `app afsluiten`, `app sluiten`, `toch afspelen`, `toch bekijken`, `afspelen`, `bevestigen`, `akkoord`, `sluiten`, `weergeven`, `bekijken`, `begrepen`, `ik begrijp het`, `openen`.
     - English: `understand`, `proceed`, `exit app`, `play anyway`, `confirm`, `dismiss`, `continue`, `got it`, `accept`.
   - Runs a **3-round loop** in JavaScript to clear chained/nested prompts in a single evaluation.
   - If the `<video>` element was paused by the prompt, triggers `video.play()`.
3. Runs automatically during `try_chrome_cdp_navigate()` and during idle polling in `browser_playback()`.

---

## 3. Admin TUI Folder Ingestion Progress & Rich Markup Escaping

### In-TUI Native Background Worker
Instead of fire-and-forget background tasks in external APIs (which run silently without UI feedback), run folder scans directly in Textual worker threads:
1. Acquire `_acquire_bg_lock()`.
2. Pass a `ProgressCallback` (`def progress(event: str, payload: dict)`) into `folder_scan.scan_and_ingest_folder(...)`.
3. Emits events: `found`, `item_start`, `tagged`, `analysis_start`, `analysis_done`, `clap_start`, `clap_done`, `source_start`, `source_done`, `ingest_start`, `item_done`, `skip`, `error`, `done`.
4. Updates `#event-log` RichLog and top `#job-status` badge via `self.call_from_thread(...)`.

### Pitfall: Rich/Textual Markup Parsing on File Paths
Paths or metadata containing square brackets (e.g. `Die_Antwoord -2010 - 5 [EP]/01. Enter The Ninja.mp3`) will be parsed as Rich markup tags (like `[EP]`) and crash or get silently eaten.
- **Fix:** Escape all dynamic text written to RichLog or Static widgets with markup enabled:
  ```python
  def markup_text(value: Any) -> str:
      return str(value).replace("[", r"\[")
  ```
