# Vectors: what exists, and what is empty

Four vector fields are defined. **Three are populated and one index has never
been created.** Coverage varies enormously between them, which is the thing
worth knowing before trusting a similarity result.

`docs/vector-search-plan.md` is the older *plan* for this and describes
proposed indexes rather than what runs; read this page for the current state.

## The state, measured

| index | field | dims | docs | of 763 tracks | fed by |
|---|---|---|---|---|---|
| `tracks` | `lyrics_vector` | 384 | 678 | 89% | `karaoke-vector-index --rebuild` |
| `tracks-lines` | `line_vector` | 384 | 6984 | — (per line) | `--rebuild --lines` |
| `karaoke-audio` | `audio_vector` | **62** | **133** | **17%** | record-mode analysis only |
| `tracks-notes` | `note_vector` | 384 | **index missing** | 0% | `--rebuild --notes`, never run |

SQLite remains the source of truth; every one of these is derived and
rebuildable, with one exception noted below.

## Sound: the honest position

**Audio vectors exist only as a side effect of record mode.** Every one of the
133 documents carries `source: recording`, because `_index_audio_vector` is
called from `recording_worker` and from nowhere else. There is no path that
vectorises a library track from its downloaded audio, even where that audio is
sitting in the cache.

So "sounds like" search works over **the 17% of tracks that happened to be
captured and analysed**, and silently returns nothing useful for the rest. That
is the single biggest gap in the vector story, and it matters most for exactly
the tracks that need it: an instrumental has no lyrics to embed, so sound is
the *only* thing that could describe it.

### What a sound vector is made of

62 dimensions, assembled from features librosa already computes during
key/BPM analysis rather than from an audio embedding network — no torch, no
model download:

```
20  MFCC mean          timbre: the overall colour of the sound
20  MFCC std           how much that colour varies across the track
12  chroma mean        pitch-class profile: the harmonic fingerprint
 7  spectral contrast  peak-to-valley structure: dense/distorted vs sparse/clean
 3  scalars            energy, brightness, tempo (tempo normalised against 220 bpm)
```

Each family is normalised **separately** before concatenation. Without that the
raw MFCC magnitudes swamp everything else and the result is a timbre-only
vector wearing a harmony label. The assembled vector is unit length, so cosine
similarity is a plain dot product and two takes of the same song at different
volumes still land near each other.

Requires librosa from the isolated audio venv (`make install-audio`). Every
entry point returns None rather than raising when it is unavailable.

### What a sound document holds

```
track_id, recording_id, artist, title, recorded_at,
duration_s, detected_key, bpm, source, audio_vector[62]
```

`recording_id` is why this one is **not fully rebuildable**: the vector
describes a specific captured performance, and once retention deletes that
audio it cannot be recomputed. Unlike a lyric vector, it is closer to data than
to a derived index.

## Lyrics: well covered, with one trap

678 of 763 tracks have a `lyrics_vector`, so semantic lyric search works
broadly.

The trap: **a track with no words still gets a vector.** `_embedding_text`
falls back to `"{title} {artist} {album}"` when a track has no plain lyrics,
and that goes into the same field a lyric query searches. 74 of the indexed
documents are in that state, so a search for a half-remembered line can return
an instrumental because its *title* is semantically close — and nothing in the
result says which basis it matched on.

`lyrics_source` is `"none"` for exactly those documents, so a caller can filter
them today; naming the basis explicitly (`vector_basis: lyrics | metadata`)
would be clearer than requiring that knowledge.

## Notes: built, never indexed

`track_notes` holds text about a track that is not its lyrics — artist
biographies, raw transcriptions — and `vector_index` supports `--notes`. The
index has simply never been created, so semantic search over notes returns
nothing because there is nothing to return.

Notes are separate documents rather than another vector on the track doc: a
track can hold both a biography and a transcription of itself, they should be
findable apart, and folding 1400 characters of prose into a track's lyric
embedding would drag it toward every prose query.

## Rebuilding

```bash
karaoke-vector-index --rebuild                  # track docs (lyrics_vector)
karaoke-vector-index --rebuild --lines          # + line docs
karaoke-vector-index --rebuild --notes          # + note docs
karaoke-vector-index --dry-run --no-embed       # shape check, no writes
```

Audio vectors are **not** covered by this. They are written during record-mode
analysis, so the only way to add one is to analyse a recording containing that
track.

## What no search path queries

Neither `karaoke.search` nor the TUI's search box reads `tracks-lines`,
`tracks-notes` or `karaoke-audio`. Those three exist for experiments and for
features not yet wired up — so indexing more into them changes nothing that a
user can see until something queries them.
