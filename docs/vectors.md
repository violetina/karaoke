# Vectors: what exists, and what is empty

Five vector fields are defined. **Four are populated and one index has never
been created.** Coverage varies enormously between them, which is the thing
worth knowing before trusting a similarity result.

`docs/vector-search-plan.md` is the older *plan* and describes proposed indexes
rather than what runs; read this page for the current state.

## The state, measured

| index | field | dims | docs | of 783 tracks | fed by |
|---|---|---|---|---|---|
| `tracks` | `lyrics_vector` | 384 | 678 | 87% | `karaoke-vector-index --rebuild` |
| `tracks-lines` | `line_vector` | 384 | 6984 | — (per line) | `--rebuild --lines` |
| `karaoke-audio` | `audio_vector` | **62** | 274 | 35% | record-mode analysis, `vectorize_cached.py` |
| `karaoke-clap` | `clap_vector` | **512** | 120 | 15% | `clap_index.py` |
| `tracks-notes` | `note_vector` | 384 | **index missing** | 0% | `--rebuild --notes`, never run |

SQLite remains the source of truth; all of these are derived, and all but one
are rebuildable.

## Two audio spaces, answering different questions

### `karaoke-audio` — 62 hand-built dimensions

Assembled from features librosa already computes during key/BPM analysis, so no
model and no download:

```
20  MFCC mean          timbre: the overall colour of the sound
20  MFCC std           how much that colour varies across the track
12  chroma mean        pitch-class profile: the harmonic fingerprint
 7  spectral contrast  dense/distorted vs sparse/clean
 3  scalars            energy, brightness, tempo
```

Each family is normalised separately before concatenation — without that the
raw MFCC magnitudes swamp everything else and the result is a timbre-only
vector wearing a harmony label.

**Its limit is measured, not suspected.** Across 3000 random pairs of unrelated
tracks the cosine ran:

```
min 0.777   p5 0.885   median 0.965   p95 0.986   max 0.999
```

Every vector sits in a narrow cone, so 0.99 is not "nearly identical" — it is
the top few percent. Mean-centring widens that spread fourteenfold **and barely
reorders the neighbours**: the cone was cosmetic, and the features themselves
put The Cranberries next to Macy Gray. Two of the five families barely vary
across the corpus (per-dimension standard deviation 0.0086 and 0.0103 against
0.024 for chroma), so roughly 40% of the vector is close to constant.

It remains useful for what it is good at — near-duplicate detection between two
captures of the same performance — and it is free, needing neither model nor
network.

Documents are `source: recording` (154, one per captured performance, keyed by
start time because the same song heard twice is two observations) or
`source: library` (120, one per track, the release itself). Comparing across
the two is not purely musical: a capture is taken from the sink monitor, so it
is a clean digital copy rather than a microphone recording, but it still passes
through whatever mixing and resampling the sink applied.

### `karaoke-clap` — 512 learned dimensions

CLAP is trained on audio paired with text, which buys two things the hand-built
vector cannot offer at any amount of tuning.

**A properly conditioned space.** Median similarity 0.779 with p5–p95 spanning
0.59–0.91, arrived at without any centring trick.

**Text queries over audio.** `"heavy distorted guitar rock"` returns Mastodon
and Dinosaur Jr.; `"electronic dance beat"` returns Modjo and Boy Harsher —
from a library the model has never seen, with no lyrics, tags or metadata
involved. This matters most for instrumentals: they have no words to embed, so
before this no query could reach them at all.

No new package — torch and transformers were already installed. The weights are
a ~600 MB download cached after first use, and embedding costs about three
seconds a track on CPU plus an ffmpeg transcode.

## Genre labels

Derived from the CLAP embedding by embedding candidate labels and taking the
nearest — zero-shot, so any taxonomy that can be phrased works. essentia's
Discogs-EffNet models are the conventional route and need TensorFlow, which
will not install here (no cp314 wheels, and the ebuild's `libclang` dependency
exists in no repo).

Stored in **both** places: `track_genre` in SQLite is the source of truth, and
`genre` / `genre_score` / `genre_runner_up` are mirrored onto the CLAP and
track documents so a search can filter without a join. 116 tracks are labelled;
4 matched nothing and are deliberately left unlabelled.

`k` in the TUI labels the sampled excerpt too, which is the only route for a
Spotify-only track — with no downloadable audio, the 45-second capture is the
sole copy that exists.

**Read the labels with their caveat.** Strong on distinctive material
(Portishead → trip hop, Mastodon and Melvins → heavy metal) and weak on rock
subgenres, where `pop` won 39 of 120 tracks and `punk rock` 29 — far beyond
what the library holds, because both sit close to a great deal of music. A thin
win on one of those is much weaker evidence than the same margin on a specific
label, which is what `GenreVerdict.confident` distinguishes. The runner-up is
stored because it is often the better answer.

Per-label calibration was tried and rejected: subtracting each label's mean
assumes a uniform prior over genres, and this library really is mostly rock, so
it penalised the correct majority label — fixing Modjo (`pop` → `house`) while
breaking Portishead (`trip hop` → `hip hop`) and Will Smith (`hip hop` →
`reggae`).

## Lyrics: well covered, with one trap

678 of 783 tracks have a `lyrics_vector`, so semantic lyric search works
broadly.

The trap: **a track with no words still gets a vector.** `_embedding_text`
falls back to `"{title} {artist} {album}"` when a track has no plain lyrics,
and that goes into the same field a lyric query searches. 74 of the indexed
documents are in that state, so a search for a half-remembered line can return
an instrumental because its *title* is semantically close — and nothing in the
result says which basis it matched on.

## Notes: built, never indexed

`track_notes` holds text about a track that is not its lyrics — artist
biographies, raw transcriptions — and `vector_index` supports `--notes`. The
index has simply never been created, so semantic search over notes returns
nothing because there is nothing to return.

## What is queryable, and what is not

| index | read by |
|---|---|
| `tracks` | `karaoke.search` (semantic + keyword), the API |
| `karaoke-clap` | `karaoke-sounds-like` (text), `karaoke-similar` (by example) |
| `karaoke-audio` | `karaoke-similar`, as the fallback where a track has no CLAP embedding |
| `tracks-lines` | nothing |
| `tracks-notes` | nothing (and it does not exist) |

`karaoke-similar` prefers CLAP and falls back to the spectral vector, naming
which space answered rather than presenting two incomparable scores on one
scale.

The TUI's own search box reads **none** of these — it scores SQLite rows
directly (see [Search and weights](search.md)), so the state of the vector
indexes changes nothing about what typing in the TUI returns.

## Rebuilding

```bash
karaoke-vector-index --rebuild                  # track docs (lyrics_vector)
karaoke-vector-index --rebuild --lines          # + line docs
karaoke-vector-index --rebuild --notes          # + note docs
python scripts/vectorize_cached.py              # spectral, from cached audio
python scripts/clap_index.py                    # CLAP, from cached audio
python scripts/label_genres.py                  # genres, from CLAP embeddings
```

**One index is not rebuildable.** A `source: recording` audio vector describes
a specific captured performance, and once retention deletes that audio it
cannot be recomputed. Unlike a lyric vector, it is closer to data than to a
derived index.
