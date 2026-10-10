# YouTube Music Split, Video-ID Matching, and TUI Caption Auto-loading

This reference documents key architectural decisions, pitfalls, and debugging workflows established for YouTube/YouTube Music metadata resolution, database deduplication, and automated background caption staging.

## 1. Browser MPRIS & YouTube Music Player Split

Browsers (like Chromium) exposing YouTube or YouTube Music tabs via MPRIS often provide unreliable or incomplete metadata:
* `xesam:artist` is frequently blank `""`.
* `xesam:url` is often omitted entirely depending on the browser version or host system.
* `xesam:title` contains site-specific branding suffixes, e.g. `"Cup Runneth Over | YouTube Music"` or `"Marie Floating over the Backyard - YouTube"`.

### The Split Detection & Title-only Fallback
To solve this in `src/karaoke/playerctl.py` and `src/karaoke/detect.py`:
1. **Player Branding Extraction:** Check if the browser title contains `"youtube music"` or the URL contains `"music.youtube.com"`. If so, rewrite the player name to `"YouTube Music"` instead of `"chromium"`. This changes TUI feedback dynamically to track the exact media platform.
2. **Title-only Fallback with Approved Priority:** When `xesam:artist` is blank, a strict lookup for `(artist, title) -> ("", "Title")` fails. When this happens, fall back to searching `tracks` by `lower(title) = lower(?)` only, sorting the matching results to **prioritize records that already have approved synced lyrics**, and resolve the canonical artist name from the matched record.

---

## 2. YouTube Video-ID Interchangeability

Because standard YouTube watch URLs (`youtube.com/watch?v=123`) and YouTube Music URLs (`music.youtube.com/watch?v=123`) share the **exact same 11-character video IDs**, they are completely interchangeable for database mapping.

### Video-ID Matching Utility
To prevent duplicate lookups or mismatch gaps:
1. Extract the 11-character video ID using a robust regex helper:
   ```python
   # re.search(r"(?:v=|/v/|/embed/|youtu\.be/|/watch\?v=)([^&\s\?]+)", url)
   ```
2. When looking up a track by URL in `localcache.find_track_by_url()`, if an exact URL match misses, extract its video ID, and perform a fallback query:
   ```sql
   SELECT t.track_id, t.artist, t.title
   FROM tracks t JOIN sources s ON t.track_id = s.track_id
   WHERE s.url LIKE ? AND s.kind IN ('youtube', 'youtube_music')
   LIMIT 1
   ```
   with parameter `f"%{video_id}%"`. This seamlessly bridges YouTube and YouTube Music urls for local sync.

---

## 3. The `GROUP BY` Source Selection Pitfall

When a song contains both a Spotify source URL and a YouTube/YT Music source URL (common after merging or backfilling), a naive SQLite query joining `tracks` with `sources` and grouping by `track_id` can create a serious TUI bug:
* **The Bug:** `GROUP BY t.track_id` will select an arbitrary source (often the `spotify` URI). Pressing Enter in Browse mode then commands `playerctl open <spotify-uri>`, which silently fails or no-ops if Spotify is closed.
* **The Fix:** Replace the naive join with a deterministic subquery that selects exactly one `source_id` per track, prioritizing web-openable/music-centric media:
  ```sql
  SELECT t.artist, t.title, s.url, s.kind
  FROM tracks t
  LEFT JOIN sources s ON s.source_id = (
      SELECT s2.source_id FROM sources s2
      WHERE s2.track_id = t.track_id
      ORDER BY
          CASE
              WHEN s2.kind = 'youtube_music' THEN 0
              WHEN s2.kind = 'youtube' THEN 1
              WHEN s2.url LIKE 'http%' THEN 2
              WHEN s2.kind = 'spotify' THEN 3
              ELSE 4
          END,
          s2.source_id
      LIMIT 1
  )
  ORDER BY t.artist, t.title
  ```
Apply this to both `browse.py` and `tui.py` to ensure Enter in Browse/List mode always opens the YouTube/YT Music webpage.

---

## 4. Background Caption Auto-loading in Textual

To automatically retrieve and load scrolling lyrics during live playback:
1. When `resolve_lyrics()` returns `None` or an untimed lyrics block for an active YouTube/YT Music scan-mode URL, dispatch a background worker in `tui.py`.
2. To avoid freezing the TUI during the network/yt-dlp fetch, use **Textual's background thread worker API**:
   ```python
   self.run_worker(
       lambda u=det.url, v=vid: self._background_autoload_captions(u, v),
       exclusive=False,
       thread=True,
   )
   ```
3. Inside the worker thread (`_background_autoload_captions`), run the blocking staging call `stage_youtube_captions(url)`.
4. **Timing-Gated Auto-approval:** Since only timed captions (LRC) are worth auto-loading into a live playing timeline, check the result:
   ```python
   # Only auto-approve/whitelist when caption source_kind has timing:
   is_synced = "_synced" in result.source_kind or "_enhanced" in result.source_kind
   ```
   If it is synced and has lines, call `staging.whitelist_staged()`, set `self._sync_key = None` (to force a reload), and use `self.call_from_thread(self._poll_detection)` to safely marshal the UI update back onto the main Textual event loop.
5. Track attempted video IDs in a set (`self._autoloaded`) to guarantee each URL is only attempted once per session.

---

## 5. Global Browser Cookie Fallback

Rather than requiring manual `--cookies-from-browser` flags on every CLI call:
1. Support a global environment setting `KARAOKE_COOKIES_FROM_BROWSER=firefox` (loaded via `.env`).
2. Update the internal `_cookie_opts` helper in `youtube.py` to check for this variable:
   ```python
   cfb = cookies_from_browser or os.environ.get("KARAOKE_COOKIES_FROM_BROWSER")
   ```
3. All background downloads, staging commands, backfill searches, and timing upgrades will automatically authenticate and unlock Premium-quality audio/manual captions.
