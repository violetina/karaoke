# YouTube Music Playlist Sync & Kiosk Modal Bypass

## 1. YouTube Music API Integration (`ytmusicapi`)
- Use `ytmusicapi` for background playlist sync, playlist CRUD, and importing tracks to SQLite.
- **Authentication Resolution:**
  1. `KARAOKE_YTMUSIC_AUTH` or `YTMUSIC_AUTH` env vars.
  2. `~/.config/karaoke/ytmusic_auth.json` or `~/.local/share/karaoke/ytmusic_auth.json`.
  3. `~/.config/karaoke/browser.json` / `oauth.json`.
  4. Unauthenticated fallback for public queries.
- **CLI Commands:** `karaoke-ytmusic-playlist` (`--list`, `--sync`, `--import-playlist <id>`, `--setup-auth`).

## 2. Kiosk Browser Parental Advisory & Modal Bypass via CDP
- YouTube / YouTube Music in kiosk mode frequently pauses on sensitive content/age-gate interstitials ("I understand and wish to proceed", "Proceed", "Exit app", "Play anyway").
- **Resolution:** Evaluate `_DISMISS_DIALOGS_JS` over Chrome CDP (port `9222`):
  - Query warning confirmation selectors (`button[aria-label*="proceed" i]`, `button[aria-label*="understand" i]`, `yt-button-renderer#confirm-button button`, `tp-yt-paper-dialog button`, `.yt-spec-button-shape-next--call-to-action`).
  - Text matching fallback on buttons for `"understand"`, `"proceed"`, `"exit app"`, `"play anyway"`.
  - Automatically call `v.play()` if the video element was paused by the dialog.
  - Run dismissal on navigation (`try_chrome_cdp_navigate`) and periodically during playback polling (`browser_playback()`).

## 3. In-TUI Long-Running Pipeline Progress & Textual Markup
- Long pipeline operations (folder scanning, audio backfill, batch vector rebuilds) should run natively in Textual background threads (`self.run_worker(..., thread=True)`).
- Progress callbacks should emit structured events (`found`, `item_start`, `analysis_start/done`, `clap_start/done`, `source_start/done`, `ingest_start`, `item_done`, `done`) and write to the event log.
- **Rich Markup Escaping:** Paths containing square brackets (e.g. `Die_Antwoord -2010 - 5 [EP]/`) will be parsed as markup tags if written directly to `RichLog` or `Static` with markup enabled. Always escape `[` with `\[` (e.g. `str(text).replace("[", r"\[")`) before passing to Rich.
