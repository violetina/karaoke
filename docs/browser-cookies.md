# Browser Cookies & Authentication (`--cookies-from-browser`)

When fetching YouTube audio, metadata, or caption tracks, YouTube may occasionally rate-limit unauthenticated requests (returning HTTP 429 errors or blocking caption payloads). Karaoke leverages `yt-dlp`'s cookie extraction capabilities via the `--cookies-from-browser` and `--cookies` flags to authenticate requests using your browser session.

---

## 1. How `--cookies-from-browser` Works

Instead of manual credential configuration, yt-dlp can safely read session cookies directly from your local browser's cookie database. 

This unlocks:
- **Bypassing Rate Limits (HTTP 429)**: Avoids automated blocks when fetching captions or metadata.
- **Accessing Protected Content**: Age-restricted videos, private/unlisted tracks, and member-only content.
- **Higher Audio Bitrates**: Unlocks YouTube Music Premium audio streams when logged into a Premium account.

---

## 2. Supported Browsers & Syntax

Pass the name of your browser to `--cookies-from-browser`:

```bash
# Use Firefox cookies
karaoke-stage-captions --cookies-from-browser firefox https://youtu.be/...

# Use Chrome cookies
karaoke-youtube --cookies-from-browser chrome https://youtu.be/...
```

### Advanced Profile & Container Syntax
yt-dlp supports specifying custom browser profiles and Firefox containers using the syntax `BROWSER[:PROFILE][::CONTAINER]`:

- **Firefox Profile**: `firefox:default` or `firefox:WorkProfile`
- **Firefox Container**: `firefox::Personal` or `firefox:default::Work`
- **Chromium Profile**: `chrome:Profile 1`

---

## 3. Using an Exported `cookies.txt` File (`--cookies`)

If you prefer not to allow yt-dlp to read your browser store directly, you can export your YouTube cookies to a standard Netscape-formatted `cookies.txt` file (using browser extensions like *Get cookies.txt*):

```bash
karaoke-youtube --cookies cookies.txt https://youtu.be/...
```

---

## 4. Security & Privacy Note

- **Read-Only**: Karaoke and yt-dlp only *read* the cookie database to attach authentication headers to requests. They never modify or write cookies back to your browser.
- **Local Only**: Cookies are never transmitted anywhere except directly to YouTube's official endpoints during the media/caption fetch.
