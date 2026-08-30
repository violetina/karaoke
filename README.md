# karaoke-000

A local karaoke + lyric-search platform.

- **Scan** a music library (`~/Music`) and/or import your **Spotify** library as metadata.
- **Index** track metadata + lyrics into **OpenSearch** (with kNN vector search) on a local **kind** cluster.
- **Fetch** synced (timestamped) lyrics from **LRCLIB** (free, no key), cached in OpenSearch.
- **Karaoke** CLI: identify the current song (file / text / live via `songrec`), then render time-synced highlighted lyrics.
- **lyricsearch**: semantic "find the song that goes '...'" via lyric embeddings (sentence-transformers).
- **Whisper fallback**: for local files with no LRCLIB match, transcribe to approximate synced lyrics with faster-whisper.

## Source model (hybrid)

- `~/Music` = audio + cache source. Karaoke playback and Whisper transcription run here.
- Spotify (your account) = optional metadata seed (liked songs / playlists). Spotify's API does **not** allow audio download, so no Whisper for Spotify-only tracks; lyrics come from LRCLIB.
- All lyrics cached in OpenSearch → repeat plays are offline.

## Layout

```
src/karaoke-000/
├── karaoke/        # the package
├── tests/          # pytest
deploy/             # kind + opensearch + scanner Job manifests
```

## Safety

The scanner/deploy scripts **only** talk to the `kind-karaoke` kube-context. They abort if the current
context is not `kind-*`, so nothing can touch the AKS work clusters.

See the implementation plan: `~/.hermes/plans/2026-08-29_173500-local-karaoke-platform.md`.

## Dev quickstart

```bash
python3.14 -m venv .venv && . .venv/bin/activate
pip install -e "src/karaoke-000[dev]"
pytest src/karaoke-000/tests -v
```

## Usage

```bash
# Index a music library (tags -> LRCLIB lyrics -> embed -> OpenSearch)
music-index --dir ~/Music

# Semantic search: find a song by what its lyrics mean
lyricsearch "religion faith losing my belief"
lyricsearch --keyword "teen spirit"          # keyword mode

# Karaoke: synced, highlighted lyrics in the terminal
karaoke "R.E.M. - Losing My Religion"        # by name (uses cache/LRCLIB)
karaoke --file song.mp3                       # from a local file's tags
karaoke --file song.mp3 --transcribe          # Whisper fallback if no LRCLIB lyrics
karaoke --file song.mp3 --force-transcribe    # always transcribe (skip cache + LRCLIB)
karaoke --listen                              # identify room audio via mic (songrec), sync once
karaoke --radio                               # CONTINUOUS: follow live radio, re-lock as songs change
karaoke --output                              # identify audio playing on this machine
karaoke --print "Queen - Bohemian Rhapsody"  # just print the LRC, no live player
karaoke --spotify                             # lock lyrics to the LIVE Spotify position
```

### Live audio sync (mic / room)

`karaoke --listen` (mic) or `--output` (this machine's audio) identify the song
via songrec AND read its Shazam `offset` (position in the track at match time).
The player anchors that offset to a monotonic clock, so lyrics scroll in sync
with whatever is playing in the room — no keypress, no Spotify needed. Offsets
from several matches are clustered (median of the tightest group) to reject the
occasional chorus-repeat outlier. Use `--offset <secs>` to nudge for latency if
lines run early/late.

**Live nudge controls** (in `--radio`, `--listen`, `--output` players): the
song-recognition step listens for ~10s before returning a position, which can
leave the highlighted line running a couple of lines behind the audio. Correct
it on the fly without restarting:

| Key | Action |
|-----|--------|
| `b` | lyrics are **behind** → jump the highlight **forward** one line |
| `v` | lyrics are **ahead** → step the highlight **back** one line |
| `0` | reset the nudge to the `--offset` baseline |
| `q` | quit |

Each press snaps the highlight exactly one line and the accumulated nudge is
shown in the footer (e.g. `nudge +6.2s`), so it sticks for the rest of the song.
`--offset <secs>` still sets the starting bias if you already know the lag.

### Spotify position sync

`karaoke --spotify` reads the track currently playing on Spotify and scrolls the
lyrics locked to the **real playback position** (polls progress + interpolates),
so lines advance in time with the audio — no keypress needed. It reuses the OAuth
credentials Hermes already stored in `~/.hermes/auth.json` (no separate login).
Spotify's API forbids audio download, so these tracks use LRCLIB lyrics only
(no Whisper); metadata can be indexed for search via the Spotify importer.

