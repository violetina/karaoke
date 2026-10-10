# YouTube / yt-dlp authenticated extraction troubleshooting

Use this when Karaoke YouTube downloads fail with:

```text
ERROR: [youtube] <id>: Requested format is not available. Use --list-formats for a list of available formats
```

Validated diagnosis pattern:

1. Inspect actual formats before changing format strings:

   ```bash
   python -m yt_dlp -F "https://www.youtube.com/watch?v=<id>"
   ```

   If audio-only formats such as `139`, `140`, or `251` appear, the stream exists; do not blindly cycle through `format` values.

2. Compare anonymous vs cookie-authenticated extraction:

   ```python
   from yt_dlp import YoutubeDL
   url = "https://www.youtube.com/watch?v=<id>"

   def probe(label, opts):
       base = {
           "quiet": True,
           "no_warnings": True,
           "noplaylist": True,
           "skip_download": True,
           "format": "bestaudio/best",
       }
       base.update(opts)
       with YoutubeDL(base) as ydl:
           info = ydl.extract_info(url, download=False)
       print(label, info.get("format_id"), info.get("ext"))

   probe("anonymous", {})
   probe("firefox", {"cookiesfrombrowser": ("firefox", None, None, None)})
   ```

3. If authenticated extraction fails while anonymous succeeds, check for a JavaScript runtime:

   ```bash
   command -v deno
   deno --version
   ```

   yt-dlp may need Deno for YouTube player JavaScript / PO-token handling. On Gentoo, install it with:

   ```bash
   sudo emerge deno-bin
   ```

Validated code pattern:

- Keep the normal download format as `bestaudio/best`.
- Avoid hard-coding browser profiles like `firefox:default`; Firefox profiles are often hashed names. `firefox` lets yt-dlp discover the profile.
- If cookie-authenticated extraction fails with cookie/profile/lock/PO-token/sign-in/login/authentication errors, or even `Requested format is not available`, retry once without cookies.
- The retry must create a fresh `YoutubeDL` instance. Mutating `ydl.params` on the old object is insufficient because `YoutubeDL.cookiejar` is a `cached_property`.
- Suppress first-attempt yt-dlp stderr noise with a quiet logger if fallback succeeds, so the CLI does not show a false fatal `ERROR:` before the successful retry.

Validated command after fix:

```bash
karaoke --youtube "https://www.youtube.com/watch?v=<id>" --download --print
```

For debugging playlist URLs, strip `&list=...` first; once fixed, the playlist-context URL can work too when `noplaylist=True` is set.
