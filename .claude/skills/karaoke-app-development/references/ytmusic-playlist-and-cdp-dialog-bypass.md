# YouTube Music Playlists & Chrome CDP Interstitial Bypass

## Architecture Overview
- **Kiosk Chrome Window:** Runs as a user systemd service (`karaoke-kiosk.service`) with `--remote-debugging-port=9222` and isolated profile `~/.config/google-chrome-kiosk`.
- **Live DOM Control & Modal Dismissal (CDP):** `src/karaoke/player_open.py` communicates with Chrome via WebSocket JSON-RPC on port 9222.
- **Account-Level Background Playlist Management (`ytmusicapi`):** `src/karaoke/ytmusic_client.py` and `src/karaoke/ytmusic_playlist.py` handle offline library import, playlist creation, and syncing synced karaoke tracks to the user's YouTube Music account.

## Pitfall: Multilingual Parental Advisory & "Exit App" Modal Interstitials
When navigating to explicit, sensitive, or age-gated songs (e.g. Slayer - *Raining Blood*), YouTube Music overlays interstitial prompts and pauses playback:
- In English: *"The following content may contain sensitive topics..."* with buttons like `"I understand and wish to proceed"`, `"Proceed"`, `"Exit app"`, `"Play anyway"`.
- In Dutch (`nl-NL`): *"Gevoelige content"* with buttons like `"Ik begrijp het en wil doorgaan"`, `"App afsluiten" / "App sluiten"`, `"Doorgaan"`, `"Toch afspelen"`, `"Bevestigen"`.

### Solution & Multi-pass CDP Dismissal
1. **Multilingual Keyword Matching:** Match text and `aria-label` attributes for both English and Dutch terms (`begrijp`, `doorgaan`, `verdergaan`, `app afsluiten`, `app sluiten`, `toch afspelen`, `toch bekijken`, `afspelen`, `bevestigen`, `akkoord`, `sluiten`, `weergeven`, `bekijken`, `begrepen`, `ik begrijp het`, `openen`, `proceed`, `understand`, `exit app`, `play anyway`, `confirm`, `dismiss`).
2. **Multi-Pass Clicking (Nested Prompts):** Certain tracks show chained prompts (e.g., initial warning -> "Exit app" -> "Confirm"). Execute up to 3 consecutive detection/click passes in the injected JavaScript evaluation to clear all chained prompts in one go.
3. **Video Resume:** If the video element was paused by the prompt, call `v.play()` immediately after dismissing.
4. **Invocation Points:** Run the dismiss logic in `try_chrome_cdp_navigate()` after navigation and in `browser_playback()` when the player is paused or in `readyState == 0`.

## Admin TUI Progress Reporting & Bracket Escaping
- **Worker Thread Pipeline:** Run long tasks (folder scans, single recording analysis) as background workers (`self.run_worker(..., thread=True)`) holding `_acquire_bg_lock()`.
- **Textual/Rich Markup Escaping:** File paths containing brackets (e.g. `Die_Antwoord -2010 - 5 [EP]/01. Enter The Ninja.mp3`) will cause Rich markup parse errors if not escaped. Always escape `[` as `\[` before sending path text to `RichLog` or `Static` widgets.
- **Explicit Button Labels:** Always append keyboard shortcut cues in button labels (e.g. `Scan Folder (s)`, `Toggle Record (O)`) so shortcuts are visible without needing mouse scrolling.
