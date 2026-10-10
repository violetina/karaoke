# YouTube Music, Queue, and Large-Library Operations

## YouTube Music authentication

`ytmusicapi` browser authentication requires at least `cookie` and `x-goog-authuser`; a partial copy of Chrome headers containing only `sec-ch-*` and user-agent fields fails. The practical setup flow is `ytmusicapi.setup(filepath=...)`, which parses pasted request headers.

For a kiosk Chrome already launched with CDP on port 9222, authentication can be derived without manual header copying:

1. Call CDP `Network.getCookies` for `https://music.youtube.com` and `https://www.youtube.com`.
2. Build the cookie string from the returned cookies.
3. Find `__Secure-3PAPISID` (or `SAPISID`/`APISID`).
4. Build `authorization` with ytmusicapi's `get_authorization(f"{sapisid} https://music.youtube.com")`.
5. Include `x-goog-authuser: 0`, `x-origin`, cookie, authorization, and `initialize_headers()` output.
6. Save the resulting JSON with restrictive permissions to `~/.config/karaoke/ytmusic_auth.json`.

Never print cookie values, authorization hashes, or the auth file contents. Test authenticated access with `YTMusic(auth=path).get_library_playlists()`.

## Persistent playlists versus live playback queue

`ytmusicapi` supports persistent account playlist operations (`get_library_playlists`, `get_playlist`, `create_playlist`, `add_playlist_items`, `remove_playlist_items`, `edit_playlist`) but does not expose a reliable live-player queue replacement/clear API.

YouTube Music's live queue is a separate client-side/temporary queue, often represented by a `TL...` playlist ID. It can contain unrelated autoplay/radio tracks even when the local Karaoke queue is correct. CDP inspection of `ytmusic-player-queue` exposes queue items and per-item menu endpoints such as "play next", "add to queue", and "remove from queue", but selectors and localized labels are brittle.

Recommended authority model:

- When Follow/Play-once mode is enabled, the local Karaoke queue is authoritative.
- Open each next track by direct kiosk navigation through CDP rather than relying on YT Music's temporary queue.
- Treat a persistent playlist such as `violetina-current-playlist` as a recovery/export copy, not the real-time queue controller.
- Disable or bypass YT Music autoplay while local-follow mode is active.
- Provide an explicit queue reset/clear action only after verifying localized DOM selectors; avoid assuming an `ytmusicapi` method exists.

The `o` binding historically toggled `_play_once`: on, the local queue advances; off, next/auto behavior falls back to the media player's own queue. If users describe this as "follow", label the control accordingly to avoid ambiguity.

## Large local collection ingestion

`folder_scan.scan_and_ingest_folder()` is currently sequential: it loops one file at a time, reads tags, fingerprints missing metadata, analyzes audio, resolves YouTube/Spotify sources, writes SQLite, and optionally creates CLAP/audio vectors. It is not automatically a Celery/RabbitMQ fan-out.

For collections with tens of thousands of files:

- Run the scan as a tracked background process with unbuffered output and a dedicated logfile.
- Emit per-file progress (`index/total`, stage, artist/title, source result, errors).
- Expect source lookups and CLAP/audio analysis to dominate runtime.
- Do not interpret a normal-running process as stalled solely because one long file takes time.
- `database is locked` errors can occur when Celery, a TUI, or a vector refresh writes concurrently. The current per-file exception handling continues, but those files need a retry pass after the main run; they are not successfully ingested.
- Count canonical tracks separately from scanned files because local source insertion may merge duplicate performances, releases, or artist-name variants.

## Startup and search scaling

Do not load a whole large collection into the initial Textual table or wildcard queue. Keep the initial browse sample bounded (500 is a practical cap), use a random SQL sample for wildcard startup, then shuffle after mood/genre filtering. Typed searches may still scan the full SQLite library and need indexes or a search-specific cache if the database grows substantially.

## Vector refresh safety

OpenSearch is derived from SQLite. A recurring vector refresh can safely re-run the idempotent rebuild command and should use the existing `vector_rebuild` lock. If a collection scan holds SQLite or another rebuild is active, skip/retry rather than competing. A script-only cron job should capture stdout/stderr, use a bounded timeout, and report non-zero exit status. A successful lock skip should be distinguishable from a completed rebuild in future improvements.

## Kiosk/CDP troubleshooting

When a second `H`/browse action appears to do nothing, inspect the karaoke logfile for repeated CDP WebSocket `TimeoutError` traces. Blocking CDP calls on the Textual UI thread can freeze input and drop subsequent keypresses. Browser navigation/opening belongs in `run_worker(..., thread=True)`, with UI updates routed through `call_from_thread`.

For localized YT Music content warnings, inspect the live DOM before adding selectors. Button text may be Dutch (`Doorgaan`, `Begrijp`, `Afspelen`, `Sluiten`) and chained dialogs may require multiple passes. Do not broad-match harmless controls such as generic "yes" or every visible filled button; target dialog scope and explicit warning actions.
