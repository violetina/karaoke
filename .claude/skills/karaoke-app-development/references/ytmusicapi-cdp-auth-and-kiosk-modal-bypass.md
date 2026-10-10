# YouTube Music API (`ytmusicapi`), Automated CDP Cookie Extraction, & Multilingual Modal Bypass

## 1. Zero-Friction `ytmusicapi` Authentication via Chrome CDP

### The Problem
`ytmusicapi` requires authenticated headers containing both `cookie` (with `SAPISID` / `__Secure-3PAPISID`) and an `authorization` header formatted as `SAPISIDHASH <timestamp>_<sha1(timestamp + " " + sapisid + " " + origin)>`.
Asking users to manually open DevTools, locate `/browse` in the Network tab, and copy request headers is error-prone (users often copy the initial document navigation headers, which lack `cookie` or `x-goog-authuser`).

### The Solution: Direct CDP Extraction
Because the karaoke kiosk Chrome window runs with `--remote-debugging-port=9222` and the user's persistent profile (`~/.config/google-chrome-kiosk`), Python can query CDP `Network.getCookies` directly, compute `SAPISIDHASH`, and generate `ytmusic_auth.json` automatically:

```python
from karaoke import player_open
from ytmusicapi.auth.browser import get_authorization, initialize_headers
import json
from pathlib import Path

def extract_auth_from_chrome_cdp(dest_path: Path | None = None) -> Path | None:
    cookie_resp = player_open._cdp_send("Network.getCookies", {"urls": ["https://music.youtube.com", "https://www.youtube.com"]})
    cookies = cookie_resp.get("result", {}).get("cookies", []) if cookie_resp else []
    if not cookies:
        return None

    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    sapisid = None
    for c in cookies:
        if c.get("name") in ("__Secure-3PAPISID", "SAPISID", "APISID"):
            sapisid = c.get("value")
            break

    origin = "https://music.youtube.com"
    auth_header = get_authorization(f"{sapisid} {origin}") if sapisid else ""

    dest = (dest_path or Path("~/.config/karaoke/ytmusic_auth.json")).expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)

    user_headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
        "Content-Type": "application/json",
        "x-origin": origin,
        "x-goog-authuser": "0",
        "authorization": auth_header,
        "cookie": cookie_str,
    }
    user_headers.update(initialize_headers())

    with open(dest, "w") as f:
        json.dump(user_headers, f, indent=4)
    return dest
```

---

## 2. Multilingual Parental Advisory & Sensitive Content Modal Dismissal

### The Issue
Explicit tracks (e.g. Slayer - *Raining Blood*) trigger YouTube / YouTube Music interstitial warning dialogs. In localized profiles (e.g. Dutch `nl-NL`), buttons have labels like:
- `"Ik begrijp het en wil doorgaan"` / `"Begrijp het"`
- `"App afsluiten"` / `"App sluiten"`
- `"Doorgaan"` / `"Verdergaan"`
- `"Toch afspelen"` / `"Toch bekijken"`

Furthermore, interstitials can be nested/chained (e.g. Sensitive content prompt -> App close confirmation prompt), requiring multiple clicks.

### Multi-Pass CDP Dismiss Pattern
Inject a JavaScript evaluator that runs up to 3 passes, checks both generic selectors and multilingual keywords, and unpauses the `<video>` element if playback stalled:

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

    const allBtns = document.querySelectorAll('button, .yt-spec-button-shape-next, a.yt-spec-button-shape-next');
    for (const b of allBtns) {
      if (b && b.offsetParent !== null) {
        const txt = (b.textContent || '').trim().toLowerCase();
        if (keywords.some(k => txt.includes(k))) {
          b.click();
          clickedThisRound = true;
          clickedCount++;
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

---

## 3. In-App Background Workers & Rich Markup Escaping

### In-App Workers for Real-time Feedback
Avoid fire-and-forget HTTP calls to backend daemons when the user is in a TUI. Instead:
1. Acquire cross-process / in-process lock (`_acquire_bg_lock()`).
2. Run task on a worker thread (`self.run_worker(_bg, thread=True)`).
3. Thread-safe progress callbacks via `self.call_from_thread(...)` updating `RichLog` and job status widgets.
4. Auto-rebuild OpenSearch vector indices upon task completion.

### Escaping Rich Markup in File Paths
File paths from local music folders (e.g. `.../Die_Antwoord -2010 - 5 [EP]/01. Enter The Ninja.mp3`) contain square brackets like `[EP]`. Textual/Rich treats `[` as markup tags unless escaped:
```python
def markup_text(value: Any) -> str:
    """Escape dynamic text written to RichLog/Static with markup enabled."""
    return str(value).replace("[", r"\[")
```
