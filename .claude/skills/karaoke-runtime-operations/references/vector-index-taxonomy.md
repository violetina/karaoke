# Vector index taxonomy — three different things are called "vectors"

"Are the vectors built?" is ambiguous in this project and caused real confusion
mid-session ("bit confused now"). Always name *which* index you mean and report
per-index doc counts against the current `tracks` count, rather than saying
"vectors are done".

## The three indexes

| Index | Dims | What it answers | Built by |
|---|---|---|---|
| `tracks` | 384 | what the song is *about* (whole track) | `karaoke.vector_index` |
| `tracks-lines` | 384 | per-line lyric search | `karaoke.vector_index --lines` |
| `tracks-notes` | 384 | note/annotation search | `karaoke.vector_index --notes` |
| `karaoke-audio` | 62 | what it *sounds like*, model-free | `audio_vector.py` (recordings) |
| `karaoke-clap` | — | audio↔text shared space | `scripts/clap_index.py` |

**Lyric/semantic vectors** (`tracks`, `tracks-lines`, `tracks-notes`) are what
library search and the TUI actually use. This is the index to fix first.

**Spectral audio vectors** (`karaoke-audio`) are assembled from librosa features
already extracted during analysis — MFCC mean/std, chroma, spectral contrast,
plus energy/brightness/tempo scalars. No torch dependency. Historically only
populated as a side effect of recordings and autoclassify, *not* library-wide.

**CLAP embeddings** (`karaoke-clap`) share an embedding space with text. Roughly
3s per track on CPU plus an ffmpeg transcode, so a five-figure library is
**hours** — not the "few minutes" the script docstring claims, which was written
for a much smaller cache. Re-running replaces rather than accumulates.

Separately: key/BPM/genre analysis (`track_analysis`, `track_genre`) is produced
by **ingestion**, not by any vector command. A finished disk import means
analysis is done and says nothing about vector coverage.

## Reading true state

```bash
curl -s 'http://localhost:9200/_cat/indices?v&h=index,docs.count,docs.deleted,store.size' | sort
```

Observed after an interrupted rebuild, against ~17,302 tracks in SQLite:

```
tracks            15,675     <- stale, ~1,600 tracks missing
tracks-lines     234,837
tracks-notes           5
karaoke-audio        343     <- effectively not built library-wide
karaoke-clap         223     <- effectively not built library-wide
```

`docs.count` well below the SQLite `tracks` count means stale, not broken. Verify
which vector field an index actually carries via its mapping (`lyrics_vector`,
`line_vector`, `note_vector`) before assuming an index is the one you want.

## Ordering when several are stale

Rebuild lyric vectors first:

1. it is the index the UI depends on;
2. it is by far the cheaper job (bulk `_bulk` path, minutes not hours);
3. the recurring cron will attempt it anyway — better to run it once
   deliberately than have it fire mid-task.

```
PYTHONPATH=src .venv/bin/python -m karaoke.vector_index --rebuild --lines --notes --threads 4
```

Then CLAP separately, budgeting hours, and only when "sounds like" search is
actually wanted.

## Sequencing against ingestion

Do not rebuild while a disk import is running — the result is stale the moment
ingestion continues, and the two compete for CPU and SQLite. Finish or stop
ingestion, then rebuild once. See
`references/interactive-starvation-and-throttle-diagnosis.md` for the cron guard
that enforces this automatically.
