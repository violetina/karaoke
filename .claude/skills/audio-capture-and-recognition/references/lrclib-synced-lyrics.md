# LRCLIB — free synced (timestamped) lyrics API

Once a song is identified, LRCLIB (https://lrclib.net) fetches its lyrics for free
with **no API key** — including **synced** lyrics (LRC format, millisecond
timestamps), which is what enables karaoke-style line highlighting. Verified
working 2026-08.

## Endpoints

- `GET /api/get?artist_name=&track_name=&album_name=&duration=` — exact-ish match.
  Best hit rate when you pass `duration` (integer seconds) and album.
- `GET /api/search?artist_name=&track_name=` — fallback when `/get` returns 404.
  Returns an array; prefer the first element with a non-empty `syncedLyrics`.

Response fields of interest: `plainLyrics`, `syncedLyrics`, `duration`,
`instrumental`, `albumName`.

```bash
curl -s "https://lrclib.net/api/get?artist_name=R.E.M.&track_name=Losing+My+Religion" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(bool(d.get('syncedLyrics')))"
```

## LRC parsing — the gotchas that actually bite

LRC lines look like `[mm:ss.xx] text`. Rules that break naive parsers:
- A single line can carry **multiple timestamps** (repeated chorus:
  `[00:10.00][01:10.00] chorus`) — expand each into its own (time, text) entry.
- The fractional part is **centiseconds when 2 digits, milliseconds when 3**:
  `[00:01.5]` = 1.5s and `[00:01.500]` = 1.5s. Normalize by right-padding to 3
  digits then treating as ms.
- Skip metadata-only lines (`[ar: ...]`, `[ti: ...]`) and stamps with empty text.
- **Sort the final list by time** — do not assume file order.

Active-line lookup for a player: index of the latest line whose timestamp is
`<= elapsed`; return -1 before the first stamp (intro). Unit-test this pure
function without audio.

## Usage patterns proven in practice

- **Cache the raw LRC** in your datastore keyed by artist+title so repeat plays
  are offline. Write-through on first fetch.
- **Be polite**: ~0.3s delay between requests when bulk-scanning a library.
- **Fallback when LRCLIB has nothing**: faster-whisper with
  `word_timestamps=True` transcribes a *local* audio file into approximate synced
  lines. Only works on files you have the audio for — NOT streaming-only /
  metadata-only tracks (e.g. Spotify library entries, whose API forbids audio
  download).

## Building semantic lyric search on top

A karaoke + "find the song by what its lyrics mean" platform was built on this:
scanner → LRCLIB → sentence-transformers embeddings → OpenSearch kNN → terminal
player. The vector-search infrastructure is documented in the
`opensearch-vector-search` skill.
