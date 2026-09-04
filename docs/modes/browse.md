# Browse mode

**Auto-selected** when nothing is playing (`detect.classify()` returns `browse`
for no metadata). The idle state: you drive the library list yourself and
opening a song launches it in the browser. This is the old "youtube mode".

## What it does

- Shows the library table (`H` toggles the overlay); pick a row and `Enter`
  opens it.
- `Enter` opens the track's best **browser-openable** source. When a track has
  both a Spotify and a YouTube/http source, browse deterministically prefers the
  web URL so `Enter` opens the song page rather than depending on the Spotify
  desktop app being the active target. Spotify URLs are only used when no web URL
  exists.
- If a cached track has no source URL at all, the TUI falls back to a YouTube
  **search** URL for the artist/title.
- Standard YouTube watch links are auto-upgraded to `music.youtube.com` for
  music-focused audio when opened.

## Sources that have words but no URL

Radio discovery and backfill resolve a track against LRCLIB by artist/title,
which fills in the *words* but records no *URL* — so the track looks complete but
`Enter` opens a search page and post-processing can't analyse it. Fill those in:

```bash
karaoke-find-sources --dry-run     # see what it would store
karaoke-find-sources --limit 50    # store them
```

It searches YouTube per track and stores the best match with the same verified
picker the backfill uses, so a wrong video isn't saved. See
[The TUI → Sourcing](../tui.md#sourcing).

## Debugging Enter/open

If a row appears but `Enter` doesn't visibly open it, see
[Workflow → Debugging browse Enter/open behavior](../workflow.md#debugging-browse-enteropen-behavior).
On every `Enter` the TUI logs the row, artist, title, source kind, URL and the
spawned `xdg-open` PID.

## Related

- [Detection flow](../flows.md#detection-flow)
- [The TUI](../tui.md)
