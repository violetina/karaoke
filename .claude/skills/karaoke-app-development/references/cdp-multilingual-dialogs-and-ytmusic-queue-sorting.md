# CDP Multilingual Interstitial Dismissal, Non-blocking Navigation, and YT Music Queue Sorting

## 1. Non-blocking Player Opening in Textual UI
When a song is opened from a TUI list or table (e.g., in Browse overlay or search results), `open_song_url(...)` communicates with Google Chrome over the Chrome DevTools Protocol (CDP).
- **Pitfall:** Invoking `open_song_url(...)` synchronously on Textual's main event thread freezes the UI whenever the kiosk WebSocket times out or Chrome takes several seconds to navigate. During this freeze, subsequent keypresses (e.g., `H` to re-open browse mode) are silently dropped.
- **Fix:** Always dispatch `open_song_url` inside a background worker thread and close overlays immediately:
  ```python
  self._hide_browse()
  self.notify(f"Opening {artist} - {title}…")

  def _bg() -> None:
      try:
          pid = open_song_url(url, kind, artist=artist, title=title, prefer_audio=True)
          suffix = f" (pid {pid})" if pid is not None else ""
          self.call_from_thread(self.notify, f"Opened {artist} - {title}{suffix}")
      except Exception as exc:
          self.call_from_thread(self.notify, f"Open failed: {exc}", severity="error")

  self.run_worker(_bg, thread=True)
  ```

## 2. Multilingual Parental Advisory & Sensitive Content Dismissal over CDP
YouTube Music displays modal interstitials for sensitive or explicit tracks (e.g., "The following content may contain sensitive topics...").
In non-English system locales (such as Dutch `nl-NL`), action buttons are localized:
- **English:** "I understand and wish to proceed", "Proceed", "Exit app", "Play anyway", "Confirm", "Dismiss", "Continue"
- **Dutch:** "Ik begrijp het en wil doorgaan", "Begrijp het", "Doorgaan", "Verdergaan", "App afsluiten", "App sluiten", "Toch afspelen", "Toch bekijken", "Afspelen", "Bevestigen", "Akkoord", "Sluiten", "Weergeven", "Bekijken"

### Multi-Pass Dismiss Snippet
Because some tracks trigger chained prompts (e.g., Sensitive content warning -> "Exit app" -> confirmation), the dismissal script must execute multiple passes (e.g., up to 3 passes) and unpause the `<video>` element if it was stalled:
```javascript
(() => {
  let clickedCount = 0;
  const keywords = [
    'understand', 'proceed', 'exit app', 'play anyway', 'confirm', 'dismiss', 'continue', 'got it', 'accept',
    'begrijp', 'doorgaan', 'verdergaan', 'app afsluiten', 'app sluiten', 'toch afspelen', 'toch bekijken',
    'afspelen', 'bevestigen', 'akkoord', 'sluiten', 'weergeven', 'bekijken', 'begrepen', 'ik begrijp het',
    'openen', 'ja'
  ];

  function tryClick() {
    let clickedThisRound = false;
    const selectors = [
      'button[aria-label*="proceed" i]', 'button[aria-label*="understand" i]',
      'button[aria-label*="doorgaan" i]', 'button[aria-label*="begrijp" i]',
      'button[aria-label*="sluiten" i]', 'button[aria-label*="afsluiten" i]',
      'yt-button-renderer#confirm-button button', '#confirm-button button',
      'ytmusic-dialog-renderer button', 'tp-yt-paper-dialog button',
      '[role="dialog"] button', 'ytmusic-you-there-renderer button',
      'button.yt-spec-button-shape-next--filled',
      'button.yt-spec-button-shape-next--call-to-action'
    ];
    for (const sel of selectors) {
      const elms = document.querySelectorAll(sel);
      for (const el of elms) {
        if (el && el.offsetParent !== null) {
          const txt = (el.textContent || '').trim().toLowerCase();
          const aria = (el.getAttribute('aria-label') || '').toLowerCase();
          if (keywords.some(k => txt.includes(k) || aria.includes(k))) {
            el.click();
            clickedThisRound = true;
            clickedCount++;
          }
        }
      }
    }
    return clickedThisRound;
  }

  for (let i = 0; i < 3; i++) {
    if (!tryClick()) break;
  }

  const v = document.querySelector('video');
  if (clickedCount > 0 && v && v.paused) {
    v.play().catch(() => {});
  }
  return clickedCount;
})()
```

## 3. Automated YouTube Music Authentication via Live Chrome CDP
Instead of forcing users to open DevTools and manually copy request headers:
1. Query Chrome on port 9222 via CDP `Network.getCookies` for `https://music.youtube.com`.
2. Extract the `__Secure-3PAPISID` or `SAPISID` cookie.
3. Compute the `SAPISIDHASH` authorization header:
   ```python
   import hashlib, time
   def get_authorization(auth_str: str) -> str:
       ts = str(int(time.time()))
       h = hashlib.sha1((ts + " " + auth_str).encode("utf-8")).hexdigest()
       return f"SAPISIDHASH {ts}_{h}"
   ```
4. Save the generated headers dictionary directly to `~/.config/karaoke/ytmusic_auth.json` to initialize `ytmusicapi.YTMusic(auth=filepath)` without manual interaction.

## 4. Reactive Queue Sorting by Musical Key, BPM, and Energy
When `#sort-select` changes in the sidebar (e.g., selecting `key`):
1. `_all_queue_rows` and `_apply_mood_filter` must query `a.detected_key AS key`, `a.bpm`, `a.energy`, `g.genre`, and `t.play_count` from SQLite.
2. Queue rows must be processed through a shared `_sort_rows(rows)` method so changing the sort selector immediately re-sorts `self._queue` rather than leaving it in alphanumeric database order.
3. The queue table displays the musical key in its own column and annotates the border title with `[sort: <mode>]`.
