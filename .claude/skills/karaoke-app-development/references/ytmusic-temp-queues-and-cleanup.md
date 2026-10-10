# YouTube Music temp queues and duplicate cleanup

Session learnings for the karaoke app's YouTube Music integration.

## Temp playlist playback from TUI search/queue

Goal: take the current TUI search/queue, play it as a real YouTube Music playlist, and keep the local karaoke queue/lyrics following browser playback.

Working pattern:

1. Build candidates only from queue rows that already have YouTube/YouTube Music video IDs.
   - Do not resolve missing rows by YT Music search for this flow; it can silently add the wrong recording/version.
   - Deduplicate by video ID while preserving queue order.
2. Use one private temp playlist per day, named like `Karaoke Temp Queue YYYY-MM-DD`.
   - Avoid creating a new playlist for every search.
   - Overwrite that day's playlist on each queue handoff.
   - Keep only the latest two daily temp playlists and delete older temp playlists.
3. To overwrite, fetch the raw playlist and remove existing tracks using entries that include both `videoId` and `setVideoId`.
   - `ytmusicapi.remove_playlist_items()` needs the raw playlist track objects, not the simplified `{videoId,title,...}` shape.
4. Add the current queue with `add_playlist_items(..., duplicates=True)` after clearing/creating the playlist.
   - Passing a long `video_ids` list to `create_playlist()` can leave only the first item in the playable queue in practice; explicit add after creation/removal was more reliable.
5. Open the playlist URL as `https://music.youtube.com/watch?v=<first_video_id>&list=<playlist_id>`.
6. Switch local queue follow into mirror mode:
   - YouTube Music owns next/previous playback.
   - TUI should not force-open the next local URL on track end.
   - Detect browser/MPRIS URL changes, extract the video ID, and move the local queue highlight/lyrics to the matching row.

## Duplicate YouTube Music playlist cleanup

Safe cleanup workflow:

1. List authenticated library playlists with `YTMusicClient.get_library_playlists(limit=1000)`.
2. Group by normalized title:
   - lowercase, trim, collapse whitespace
   - optionally drop obvious copy suffixes like `(copy)` or `- copy`
3. Before deleting, compare track contents where feasible:
   - For playlists up to about 500 tracks, fetch each playlist with `get_playlist_tracks()` and compare sets of `videoId`.
   - Treat equal sets as exact duplicate-content playlists.
   - Treat strict subsets or same-title/different-count playlists as risky; ask before deleting.
   - Very large playlists (`Favorite Songs` etc.) should be reported, not auto-deleted, unless specifically confirmed.
4. Ask the user to confirm the exact deletion list.
5. Delete only confirmed duplicate playlist IDs with `ytmusicapi.delete_playlist()` / `YTMusicClient.delete_playlist()`.
6. Re-list duplicate-title groups afterward to verify the count dropped and no risky groups were removed.

Never print or save auth headers/cookies/tokens; if referenced in notes, redact as `[REDACTED]`.
