# Vectors: what exists, and what is empty

Five vector fields are defined, in six indexes (counts as of 2026-11-12, library of 19,168 tracks).
Coverage is **not** 100% for the audio indexes: they need the audio file, so a track whose audio
is gone or never downloaded can sit without a vector until the post-processing chain reaches it.

`docs/vector-search-plan.md` is the older *plan* and describes proposed indexes
rather than what runs; read this page for the current state.

## Current Index Stats

| index | field | dims | docs | library coverage | fed by |
|---|---|---|---|---|---|
| `tracks` | `lyrics_vector` | 384 | **19,168** | 100% (every track, even without lyrics, see below) | `karaoke.vector_index`, chain step `rebuild_vectors` |
| `tracks-lines` | `line_vector` | 384 | **297,167** | per synced line: 8,632 tracks have timed lyrics | `--rebuild --lines` |
| `tracks-notes` | `note_vector` | 384 | **6** | per note | `--rebuild --notes` |
| `karaoke-audio` | `audio_vector` | 62 | **381** | 260 captured recordings + 121 library tracks | `recordings`, `vectorize_cached.py` |
| `karaoke-clap` | `clap_vector` | 512 | **18,904** | 98.6% (264 tracks without one) | chain step `embed_clap` |
| `karaoke-progression` | `progression_vector` | chord/key movement | **18,939** | 98.8% (229 tracks without one) | chain step `analyze_harmony` |

The `tracks` documents also carry the flat audio-analysis fields (Mood v2, Chords v2 and
the CLAP genre verdict); see [Search fields written outside the embedding step](#search-fields-written-outside-the-embedding-step).

## Keeping the indexes in step with PostgreSQL

PostgreSQL is the source of truth; every index is derived. The live write paths are:

| writer | writes |
|---|---|
| chain step `rebuild_vectors` (`vector_index.index_track`) | the whole `tracks` document (+ lines) |
| chain step `embed_clap` | `karaoke-clap` vector, then `vector_index.update_audio_fields` for mood, chords and genre |
| `scripts/label_genres.py` | `track_genre` rows, mirrored through `vector_index.genre_fields` (the same helper as the chain) |
| `scripts/reconcile_index.py` | brings all of the above back in line after batches, merges or renames |

`scripts/reconcile_index.py` (report only; add `--apply`) deletes docs whose track no longer
exists, rebuilds tracks missing from `tracks` (targeted by id, with lines), and partial-updates
documents whose artist/title/album or Mood/Chords/genre fields differ from PostgreSQL, removing
fields the database no longer backs. Tracks missing a CLAP or progression doc need audio: queue
them with `scripts/enqueue_mood_chords.py` and the Celery workers. A blank genre verdict produces
no genre field at all, and a blank `audio_genre*` string counts as absent.

Never call `rebuild_from_db()` without `track_ids=[...]` after a subset batch: a bare call
re-embeds the whole library. A targeted call reads rows through `iter_track_rows`, the same
joins as a full rebuild (source, lyrics, genre, mood, chords).

### Backups

Audio vectors cannot be regenerated once the audio is gone, so they are copied out:

```bash
python scripts/vector_backup.py backup          # clap, audio, progression -> Postgres vector_backup
python scripts/vector_backup.py verify
python scripts/vector_backup.py export backups/vectors-YYYYMMDD.jsonl.gz   # portable copy
python scripts/backup_opensearch_to_postgres.py --indices tracks,tracks-notes   # opensearch_snapshots
```

Both backups only upsert. Documents deleted from OpenSearch (merged or removed tracks) stay in
the backup, so a restore would bring them back: `verify` reports them as `only_in_backup`.
Prune deliberately, not automatically, because those vectors are irreplaceable.

## Gap-Fill & Vector Sampler Pipeline

The current route for filling gaps is the Celery chain: `scripts/enqueue_mood_chords.py --limit N`
publishes only the steps each track is missing (mood, chords, and `clap` when the track has no
vector in `karaoke-clap`). Older standalone scripts predate it
(`fill_analysis_and_vector_gaps.py`, `fill_library_gaps.py`, `fill_local_gaps.py`,
`batch_enrich_chords_and_vectors.py`, `batch_process_chords_vectors.py`, `backfill_*.py`);
prefer the chain unless you need a sequential run with a per-track CSV report.

PostgreSQL remains the source of truth; all of these are derived, and all but the audio
vectors are rebuildable.

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

Documents are `source: recording` (260, one per captured performance, keyed by
start time because the same song heard twice is two observations) or
`source: library` (121, one per track, the release itself). Comparing across
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

Stored in **both** places: `track_genre` in PostgreSQL is the source of truth, and
the verdict is mirrored onto the track document (`genre`, `genre_score`, `genre_runner_up`,
`genre_runner_up_score` and the `audio_genre*` copies) and the CLAP document (`genre`,
`genre_score`, `genre_runner_up`) so a search can filter without a join. 15,396 tracks are
labelled; about 3,500 CLAP-embedded tracks matched no label and are deliberately left
without a row or field. Chain, `label_genres.py` and `reconcile_index.py` all build the
fields through `vector_index.genre_fields`.

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

10,788 of 19,168 tracks have lyrics (8,774 of them timed) and a genuine `lyrics_vector`, so semantic lyric search covers a little over half the library.

The trap: **a track with no words still gets a vector.** `_embedding_text`
falls back to `"{title} {artist} {album}"` when a track has no plain lyrics,
and that goes into the same field a lyric query searches. about 8,400 of the indexed
documents are in that state, so a search for a half-remembered line can return
an instrumental because its *title* is semantically close — and nothing in the
result says which basis it matched on.

## Notes: built and fully indexed

`track_notes` holds text about a track that is not its lyrics — artist
biographies, raw transcriptions — and is now fully indexed under the `tracks-notes`
index using the command `--notes`.

## Search fields written outside the embedding step

Mood v2 (`audio_mood`, `audio_feeling`, `mood_arc`, `mood_share_*`, ...) and Chords v2
(`harmony_style`, `key_mode`, `blues_score`, `ii_v_i`, ...) are flattened by
`vector_index.audio_analysis_fields` from `track_moods` / `track_chords` at the current version
only; the mood timeline is made after the search document, so `update_audio_fields` adds them.
The release `year` (integer, from `track_year`; see [post-processing](howto-post-processing.md)) is written the same way and is absent when unknown.
Current coverage in `tracks`: genre 15,396, audio mood 16,718, harmony 16,797.

## What is queryable, and what is not

| index | read by |
|---|---|
| `tracks` | `karaoke.search` (semantic + keyword), the API |
| `karaoke-clap` | `karaoke-sounds-like` (text), `karaoke-similar` (by example) |
| `karaoke-audio` | `karaoke-similar`, as the fallback where a track has no CLAP embedding |
| `tracks-lines` | nothing |
| `tracks-notes` | nothing |

`karaoke-similar` prefers CLAP and falls back to the spectral vector, naming
which space answered rather than presenting two incomparable scores on one
scale.

The TUI's own search box reads **none** of these — it scores PostgreSQL rows
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
