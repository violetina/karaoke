# TUI genre filtering and song-change/postprocess flow

Session learning from adding a genre filter and tightening song-change handling.

## Genre filter pattern

- The TUI library already joins `track_genre` in `_load_tracks`; use that `genre` field for immediate library filtering.
- Build the genre selector dynamically from SQLite, not a hard-coded taxonomy:
  - query `SELECT DISTINCT genre FROM track_genre WHERE genre IS NOT NULL AND length(trim(genre)) > 0 ORDER BY lower(genre)`
  - prepend `("All Genres", "all")`
  - keep stored values as the select values; use title-cased labels only for display.
- Place the genre selector directly below the mood preset selector so the visual model is: text search -> mood energy/preset -> genre -> sort.
- Genre filtering should combine with mood filtering, not replace it.
- Apply the same predicate to both paths:
  - `load_songs()` library rows already carrying `genre`
  - active search/queue rows, falling back to a `track_genre` lookup when search hits do not include `genre`.
- Add a queue-focused regression test. It catches the easiest miss: filtering search results but forgetting the already-active queue.
- Update `docs/tui.md` whenever adding user-visible TUI controls.

## Song-change / postprocess event flow

When song changes are involved, reason through the whole pipeline instead of patching only the visible UI:

1. detect a new player/browser/mic song
2. resolve whether the track is already known
3. normal lyric lookup path if lyrics exist
4. check missing postprocess steps (`analysis`, `timings`, `sync`, vectors)
5. enqueue needed work only when there is enough source material
6. catch Celery/Argo/platform events
7. on success, force a detection/lyric re-resolve so the running TUI reloads synced lyrics
8. update UI labels/subtitles and rely on existing notify flow

Practical fixes from this session:
- Spotify/VLC/browser tracks may have lyrics but no downloadable YouTube URL. If they need sync/timings, the postprocess path can search YouTube, save the source, then continue.
- Avoid blind YouTube searches for `Unknown - Unknown`.
- If CDP navigation falls back to JavaScript evaluation, escape single quotes in URLs before embedding them in a JS string; otherwise titles/searches with apostrophes can break navigation and spawn external error tabs.
