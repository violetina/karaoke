# Search: what runs, and how it is scored

There are **two different searches** in this project and they answer different
questions. Knowing which one is running explains most surprises.

| | where | index | answers |
|---|---|---|---|
| **Library search** | the TUI's `/` box | SQLite | "which track do I mean?" |
| **Semantic search** | `karaoke.search`, the API | OpenSearch | "which song goes like this?" |

The TUI's search box is the first one. It never touches OpenSearch, so the
vector index being empty or stale changes nothing about what you see when you
type in the TUI.

---

## Library search (the TUI box)

`karaoke.librarysearch.search()` reads every candidate track out of SQLite and
scores it in Python. No index, no server: the library is small enough that a
full scan is faster than anything that would need keeping in sync.

```mermaid
flowchart TD
    A["/ query"] --> B[SELECT tracks + approved lyrics]
    B --> B1{"duration <= ALBUM_UPLOAD_SECONDS?"}
    B1 -- no --> X[dropped: a full-album upload<br/>is not something you can play]
    B1 -- yes --> C[score_row]
    C --> D[title  x 1.0]
    C --> E[album  x 0.6]
    C --> F[artist x 0.4]
    C --> G[lyrics x 0.2]
    G --> G1{"words are a Whisper guess?"}
    G1 -- yes --> G2[x 0.25 further,<br/>reported as 'lyrics?']
    D & E & F & G --> H[sum the fields that matched]
    H --> I[sort by score, then artist, then title]
```

### The field weights

```
W_TITLE   1.0     what you almost always mean
W_ALBUM   0.6     narrower than an artist, so a hit is more deliberate
W_ARTIST  0.4     broad: matches everything they recorded
W_LYRICS  0.2     the fallback for "I only remember a line of it"
```

### How well a field matched

Position counts as well as presence, so searching `river` does not rank
*Riverside* and *The River* identically:

```
EXACT    1.0     the field is exactly the query
PREFIX   0.75    starts with the query, at a word boundary
WORD     0.6     the query appears as a whole word
PARTIAL  0.45    starts with the query mid-word ("Riverside" for "river")
CONTAINS 0.35    appears somewhere, inside a word
```

A track's score is the **sum** of every field that matched, not the best one. A
track matching on both title and artist is a better answer than one matching on
either alone, and should outrank it.

### Lyrics are matched differently

Only **whole-word** matches count in lyrics. Substring matching in a body of
text would make `love` match *glove* and *clover*, and the lyrics field would
then match nearly everything at its low weight rather than nothing.

So a lyric hit is always worth `0.6 x 0.2 = 0.12` — deliberately below any
title, album or artist match.

### Whisper's guesses score lower

Lyrics whose *words* are Whisper's own guess keep only a quarter of their
weight, and the matched field is reported as `lyrics?` rather than `lyrics`.

Two sources mean this and they are easy to confuse:

- `whisper` and `whisper_synced` — **the words are a guess**. Demoted.
- `whisper_aligned` — **the words are real**, from LRCLIB or a lyrics panel,
  and only the *timings* came from Whisper. Not demoted.

Demoted, not excluded: some tracks have no other text at all, and removing them
would make those songs unfindable by their words entirely.

### A worked example

```
query "skeletons"

  1.12  The Sound - Skeletons              title, lyrics
        title EXACT 1.0 x W_TITLE 1.0            = 1.00
        lyrics WORD  0.6 x W_LYRICS 0.2          = 0.12

  0.60  Mogwai - Kids Will Be Skeletons     title
        title WORD   0.6 x W_TITLE 1.0           = 0.60
```

---

## Known limitation: multi-word queries

**A query is matched as one whole string, not as separate words.** There is no
per-term handling anywhere in `field_score`, so a query only matches if the
entire phrase appears in a single field.

The consequence is easy to hit:

```
"glory box"                -> 1 hit    the whole phrase is the title
"portishead glory"         -> 0 hits   artist and title, so no single field has it
"dinosaur start choppin"   -> 0 hits   likewise
"sonic youth dirty"        -> 0 hits   likewise
```

Typing an artist *and* a title — the most natural way to search for a specific
song — cannot work today. Nor can typing title words in a different order from
the stored title.

This is a design gap rather than a bug in the scoring: the weights and tiers do
what they say, but they are only ever applied to one string. Fixing it means
scoring per term and combining, which changes what "EXACT" means and needs the
tiers re-thought rather than reused.

Tracked as an issue; see the repository issue list.

---

## Semantic search (OpenSearch)

`karaoke.search` queries the `tracks` index and answers a different question:
*which song goes something like this*. It embeds the query and compares it to
each track's `lyrics_vector` (kNN), or runs a BM25 `multi_match` for keywords.

Keyword search demotes transcribed lyrics with a `boosting` query rather than
a filter, for the same reason the library search does: they are the only text
some tracks have.

### Two things worth knowing about the vectors

**A track with no words still has a lyric vector.** `_embedding_text` falls
back to `"{title} {artist} {album}"` when a track has no plain lyrics, and that
goes into the same field a lyric query searches. 74 of the indexed tracks are
in that state, so a search for a half-remembered line can return an
instrumental because its *title* is semantically close.

**Audio vectors cover very little.** The timbre index (`karaoke-audio`) holds
far fewer documents than the library has tracks, and most came from recording
samples rather than the library, so mood search over instrumentals is running
on almost nothing.

---

## What is *not* searched

- **Track notes** (artist biographies, raw transcriptions) live in
  `track_notes` and index into `<index>-notes`, which no search path queries
  yet.
- **Line-level lyrics** are indexed into `<index>-lines` for timing
  experiments, not used by either search above.

---

## Reading a result

The TUI shows which fields contributed to a hit. That is the quickest way to
understand a surprising ranking:

- `title` — matched the song name
- `artist`, `album` — matched, at lower weight
- `lyrics` — matched real words in the lyric body
- `lyrics?` — matched words that are a Whisper guess, scored at a quarter
