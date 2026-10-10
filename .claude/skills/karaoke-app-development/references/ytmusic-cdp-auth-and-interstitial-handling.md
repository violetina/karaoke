# YouTube Music CDP Authentication, Playlist Sync, & Interstitial Handling

## 1. Automatic Authentication Extraction via Chrome CDP

Rather than requiring manual header copy-pasting from browser DevTools, `ytmusicapi` authentication headers can be generated automatically from the active Chrome kiosk session listening on remote debugging port `9222`:

```python
from karaoke import player_open
from ytmusicapi.auth.browser import get_authorization, initialize_headers
import json
from pathlib import Path

# 1. Fetch live cookies from Chrome over CDP
cookie_resp = player_open._cdp_send(
    "Network.getCookies",
    {"urls": ["https://music.youtube.com", "https://www.youtube.com"]}
)
cookies = cookie_resp.get("result", {}).get("cookies", []) if cookie_resp else []
cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)

# 2. Extract SAPISID / __Secure-3PAPISID to compute SAPISIDHASH authorization
sapisid = None
for c in cookies:
    if c.get("name") in ("__Secure-3PAPISID", "SAPISID", "APISID"):
        sapisid = c.get("value")
        break

origin = "https://music.youtube.com"
auth_header = get_authorization(f"{sapisid} {origin}") if sapisid else ""

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

dest = Path("~/.config/karaoke/ytmusic_auth.json").expanduser()
dest.parent.mkdir(parents=True, exist_ok=True)
with open(dest, "w") as f:
    json.dump(user_headers, f, indent=4)
```

## 2. Multilingual Parental Advisory / Sensitive Content Auto-Dismissal

On non-English locales (e.g., Dutch `nl-NL`), YouTube Music displays sensitive content warnings and prompts in the host language. In kiosk mode, this blocks autoplay until dismissed.

### Key Dutch & English Button Keywords:
- **Dutch:** `begrijp`, `doorgaan`, `verdergaan`, `app afsluiten`, `app sluiten`, `toch afspelen`, `toch bekijken`, `afspelen`, `bevestigen`, `akkoord`, `sluiten`, `weergeven`, `bekijken`, `begrepen`, `ik begrijp het`
- **English:** `understand`, `proceed`, `exit app`, `play anyway`, `confirm`, `dismiss`, `continue`, `got it`, `accept`

### Multi-Pass CDP Dismiss Pattern:
Because interstitials often have chained/nested modals (e.g. *Sensitive Content warning -> Exit app / Open in browser -> Confirm*), evaluate the dismisser in up to **3 consecutive passes** in JavaScript:

```javascript
(() => {
  let clickedCount = 0;
  const keywords = [
    'understand', 'proceed', 'exit app', 'play anyway', 'confirm', 'dismiss', 'continue', 'got it', 'accept',
    'begrijp', 'doorgaan', 'verdergaan', 'app afsluiten', 'app sluiten', 'toch afspelen', 'toch bekijken',
    'afspelen', 'bevestigen', 'akkoord', 'sluiten', 'weergeven', 'bekijken', 'begrepen', 'ik begrijp het'
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
      for (const el of document.querySelectorAll(sel)) {
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

## 3. Kiosk Browser Lifecycle & User Data Directory

- Kiosk Chrome runs under `karaoke-kiosk.service` with profile `%h/.config/google-chrome-kiosk` and port `9222`.
- `launch_kiosk_browser()` and `restart_kiosk_browser()` must target `systemctl --user start/restart karaoke-kiosk.service` first, ensuring the exact same profile directory is used so cookies and logins persist across launches without falling back to unexpected `xdg-open` desktop browser windows.
