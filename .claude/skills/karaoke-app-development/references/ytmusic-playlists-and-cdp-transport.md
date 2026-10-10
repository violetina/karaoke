# YouTube Music playlist control + CDP transport + kiosk UX

Covers the `ytmusicapi` integration, CDP-based transport controls, consent-modal
auto-dismiss, and the folder-scan progress pattern added to the karaoke app.

## 1. ytmusicapi auth WITHOUT manually copying browser headers

The user could not find/produce request headers via DevTools (the `ytmusicapi`
browser-header flow is fiddly and often missing `cookie` / `x-goog-authuser`).
The reliable path is to lift the already-authenticated session straight out of
the running kiosk Chrome over CDP:

```python
from ytmusicapi.auth.browser import get_authorization, initialize_headers
from karaoke import player_open

resp = player_open._cdp_send("Network.getCookies",
    {"urls": ["https://music.youtube.com", "https://www.youtube.com"]})
cookies = resp["result"]["cookies"]
cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
sapisid = next((c["value"] for c in cookies
                if c["name"] in ("__Secure-3PAPISID", "SAPISID", "APISID")), None)
headers = {
    "User-Agent": "Mozilla/5.0 ... Chrome/131.0.0.0 Safari/537.36",
    "Accept": "*/*", "Content-Type": "application/json",
    "x-origin": "https://music.youtube.com", "x-goog-authuser": "0",
    "authorization": get_authorization(f"{sapisid} https://music.youtube.com"),
    "cookie": cookie_str,
}
headers.update(initialize_headers())
# json.dump(headers, open("~/.config/karaoke/ytmusic_auth.json","w"))
YTMusic(auth=str(auth_file))   # now authenticated
```

Key facts:
- `YTMusic(auth=<dict-or-json-string of raw browser headers>)` FAILS with
  "oauth JSON provided ... but oauth_credentials not provided" unless the headers
  include a valid `authorization: SAPISIDHASH ...` value. That's why we compute it
  ourselves with `get_authorization(f"{SAPISID} {origin}")` — a time-based SHA1 of
  `__Secure-3PAPISID`.
- `is_browser()` in ytmusicapi requires both `authorization` and `cookie`.
- Implemented as `ytmusic_client.extract_auth_from_chrome_cdp()`, auto-invoked by
  `YTMusicClient()` when no auth file is configured. Requires the kiosk browser
  (systemd `karaoke-kiosk.service`, port 9222) to be running and logged in.
- `karaoke-ytmusic-playlist --list/--sync/--import-playlist/--setup-auth` is the CLI.
  `ytmusic_playlist.karaoke_tracks()` selects synced-lyric tracks joined to a
  YouTube/`youtube_music` source and resolves missing video IDs via
  `YTMusic.search(..., filter="songs")` with a `videos` fallback.

## 2. CDP transport controls (put them on the header/transport bar)

`player_open` exposes CDP helpers that click the live YT Music DOM (video element
first, then localized aria-label button fallbacks):
`cdp_play_pause()`, `cdp_next_track()`, `cdp_previous_track()`,
`cdp_toggle_av(mode=None)` (Song/Audio vs Video via `ytmusic-av-toggle
.song-button/.video-button`), `cdp_toggle_shuffle()`, `cdp_toggle_repeat()`.
In the TUI these back a `#transport-bar` of Buttons in `#main` under `#now-playing`
plus key bindings (space/n/p/V). Hide `#transport-bar` in `.-narrow`, `.-short`,
and `.-focus` screen classes or the layout tests that assert lyric-share %
regress.

## 3. Auto-dismiss consent / parental-advisory modals over CDP

Age-gated / "sensitive content" tracks stall playback behind a modal that needed
TWO manual clicks (e.g. "Exit app" then proceed). The browser locale matters: on
this box Chrome is Dutch (`nl-NL`), so English-only selectors missed it.
`player_open._DISMISS_DIALOGS_JS` matches a keyword set spanning EN+NL
(`understand/proceed/exit app/play anyway` + `begrijp/doorgaan/verdergaan/
app afsluiten/toch afspelen/sluiten/...`) and runs up to 3 passes to clear
chained/nested dialogs. Called from `try_chrome_cdp_navigate` and passively from
`browser_playback()` when the video is paused or `readyState==0`. Extend the
keyword list rather than adding new branches.

## 4. Kiosk launch prefers systemd

`launch_kiosk_browser()` tries `systemctl --user start karaoke-kiosk.service`
first, then falls back to spawning `/usr/bin/google-chrome-stable --app=<url>
--remote-debugging-port=9222 --user-data-dir=~/.config/google-chrome-kiosk`.
If a song "opens in its own window", the cause is the kiosk service being
inactive so the code fell back to `xdg-open` (an unmanaged tab) — start the
service.

## 5. folder_scan progress callback + background-run pattern

`folder_scan.scan_and_ingest_folder(..., progress=cb)` emits events
(`found/item_start/tagged/analysis_start/analysis_done/clap_start/clap_done/
source_start/source_done/ingest_start/item_done/skip/error/done`) with
`index/total/name/artist/title/key/bpm/genre` payloads. The Admin TUI renders
these live in its events log; scans run in a `run_worker(..., thread=True)` under
`_acquire_bg_lock()`. IMPORTANT: a full-library CLAP scan (esp. long post-rock
tracks) far exceeds a 300s foreground exec timeout — run it as a background
terminal process with `notify_on_complete=true`, or scope to a subset (e.g. by
symlinking only today's cached files into a temp dir and scanning that). The
per-file progress payload does not echo key/bpm/genre back into `item_done` in
all paths; trust the final `classified`/`sourced` counters + SQLite row counts,
not the inline log, to confirm analysis landed.

## Pitfall: only 'audio-on-disk' tracks can get CLAP/audio vectors

Text/lyric vectors (`tracks`, `tracks-lines`) reach ~100% because they embed
metadata+lyrics from SQLite. CLAP (`karaoke-clap`) and spectral (`karaoke-audio`)
vectors need a real audio file (local file or a cached `~/.local/share/karaoke/
youtube/*.webm`); streaming-only URL tracks stay uncovered until their audio is
downloaded/sampled. The gap is expected, not a bug. It fills via `k` sampling,
Celery post-processing downloads, and folder/cache scans.
