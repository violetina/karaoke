# Next-song suggestion: what runs, and how it is weighted

There are **two different "what should play next" algorithms** in this
project, built for different jobs. Both rank candidates by audio/mood fit,
but they answer different questions and are tuned differently.

| | lives in | question it answers | used by |
|---|---|---|---|
| **DJ booth suggest** | `karaoke.dj_suggest` | "given the song playing now, what's the single best next track under this strategy?" | MCP `suggest_next_tracks`, the TUI DJ booth, `dj_chat.cmd_suggest` |
| **Queue autopilot** | `karaoke.queue_suggest` | "the queue is almost empty — what N tracks keep the *whole queue's* vibe going?" | `collection.autopilot_refill` ("Keep going" toggle), `POST /api/queue/suggest` |

---

## DJ booth suggest (`dj_suggest.py`)

### It is tuned against real radio DJ behaviour, not guessed

The module used to rank by key and tempo alone: a Camelot-compatible key
within 12% BPM, nearest BPM first. Measured against what radio DJs actually
played next — 295 consecutive song pairs pulled from the project's own radio
sessions, checking where the *true* next song ranked among 200 random library
tracks — that scored no better than chance (median rank 121 of 201). So did
key, tempo, or chord motion alone. What actually predicted the next song:

| signal | top-10 | median rank | MRR |
|---|---|---|---|
| old `/suggest` (harmonic only) | 6.8% | 121 | 0.050 |
| timbre (CLAP) | 46.9% | 15 | 0.271 |
| mood flow (end → start) | 19.7% | 32 | 0.085 |
| timbre + 0.1 × mood flow | 46.3% | 14 | 0.305 |
| + one song per act | 51.7% | 8 | **0.347** |

(Weights tuned on half the pairs, reported on the other half. Radio DJs
repeated the same act in only 4 of 147 real transitions — hence the
one-song-per-act rule below.)

So **every** strategy ranks candidates by `fit`, and the strategies differ in
what they additionally *require* (compatible key/tempo for a harmonic mix,
a specific intensity direction for an energy move, shared chord motion for a
structural match) — not in what predicts a good next song, which is sound +
mood regardless of strategy.

### The core fit score

```
fit = timbre + MOOD_WEIGHT * mood_flow
```

| term | weight | what it is |
|---|---|---|
| `timbre` | 1.0 (implicit) | cosine similarity between the two tracks' CLAP embeddings — a learned sound/genre/timbre vector |
| `mood_flow` | `MOOD_WEIGHT = 0.1` | how naturally the candidate's *opening* mood follows the seed's *closing* mood (see below) |

`mood_flow(seed, cand) = exp(-hypot(Δvalence, Δtanh(arousal)) / 0.6)`, read
from the first and last mood segment of each track (`track_moods.segments`):
close emotional distance between "how the seed ends" and "how the candidate
begins" scores near 1.0; a jarring mood jump decays toward 0.

### Per-strategy formulas

| strategy | formula | hard requirements (returns `None` to exclude if unmet) |
|---|---|---|
| `flow` (default) | `fit` | none — pure sound + mood handover |
| `harmonic` | `fit + 0.05 * camelot_score` | `camelot_score >= 0.8` (compatible key) AND tempo within `HARMONIC_TEMPO = 0.08` (8%, widened to `WIDE_TEMPO = 0.12` / 12% if too few candidates qualify) |
| `energy_up` / `cool_down` | `fit + 0.5 * direction * arousal_step` | mood/tempo must move the requested direction: arousal step > 0.05 in that direction, or (no mood data) BPM moves >= 3 that way and never drops > 10 BPM against an "up" request |
| `mood` | `0.5 * timbre + mood_flow` | none — leans harder on the mood handover than `flow` does, half as much on raw sound |
| `structural` | `cosine(chord_motion) + 0.3 * timbre + style_bonus` | both tracks need an indexed chord-motion vector; `style_bonus = 0.1` if `harmony_style` matches |

`camelot_score(a, b)`: `1.0` same key, `0.8` relative major/minor or one
Camelot step, `0.4` two steps, `0.0` otherwise.

`tempo_ratio(a, b)`: relative log-distance, checking half-time and
double-time too (`min(|ln(b/a)|, |ln(b/2a)|, |ln(2b/a)|)`) — a candidate at
double tempo of the seed still mixes.

### Candidate pool and the one-act rule

- Candidates come from `search.sounds_like_track` (CLAP k-NN, `POOL = 150`)
  when the seed has a CLAP vector; `structural` additionally pulls chord-motion
  neighbours; if fewer than 20 candidates come back and the seed has a BPM, a
  plain tempo-band SQL query tops the pool up (the old key+tempo-only logic,
  now just a fallback source of *candidates*, not the ranking itself).
- **One song per act, and never the seed's own act**, directly reflecting the
  "4 of 147" empirical finding above — "same sound" reliably surfaces the same
  artist's other tracks (sound-similarity search for a Foo Fighters song pulls
  more Foo Fighters), and playing the same act back-to-back is poor DJing by
  the data. If too few other acts qualify to fill `limit`, the seed's own act
  fills the *remaining* slots after everything else, never ahead of it.
  `artist_key()` normalizes "The Jimi Hendrix Experience" and "Jimi Hendrix"
  (or anything joined with `feat.`/`&`/`x`/etc.) to the same act so a featured
  collaborator doesn't dodge the rule.

---

## Queue autopilot ("Keep going") (`queue_suggest.py`)

Different job: not "the one best next song for the track now playing", but
"the whole queue is running low — append tracks that fit **everything already
queued**", so a long autopilot/follow session doesn't visibly drift toward
whatever the very last track happened to be.

- **Seeds on every track already in the queue** (its last 10, in
  `autopilot_refill`), not just the current one. Each seed contributes its
  `PER_SEED = 20` nearest neighbours by sound (CLAP, falling back per-seed to
  the spectral vector, then to same-genre tracks if no vector index is
  available at all — the same precedence `search.similar_main` uses).
- **Score = summed similarity across every seed a candidate matched.**
  Candidates are pooled by `track_id` and ranked by `(seeds_matched,
  summed_score)` — a track that's a *decent* neighbour of five different
  queue members outranks one that's an outstanding match to only one. This,
  not a single top-similarity score, is what makes the result track the
  queue's shared character instead of lurching toward one outlier song.
- **`PER_ARTIST = 2`** cap, same rationale as the DJ booth's one-act rule
  (fewer outright duplicate-sounding runs), just a softer cap since this
  fills a whole batch rather than picking one "best" track.
- No mood/key/tempo terms at all — this is query-by-example on sound alone,
  so it needs no lyrics or mood-segment data and works for instrumentals too.

### When it actually fires

`collection.autopilot_refill(conn, threshold=AUTOPILOT_THRESHOLD, add=AUTOPILOT_ADD)`,
polled by the queue-follower service every few seconds:

- `AUTOPILOT_THRESHOLD = 5`: only refills once the queue has 5 or fewer
  unplayed tracks left (`len(rows) - 1 - current_index <= 5`) — it does not
  top up a queue that already has runway.
- `AUTOPILOT_ADD = 5`: appends up to 5 new tracks per refill.
- No-op (and reports why) if autopilot is off, there's no active queue, there
  is still runway left, the seed tracks have no vector, or nothing new
  qualifies.

Toggled via `POST /api/queue/autopilot {"enabled": true|false}`, surfaced in
the dashboard as the "🔁 Keep going" button in both the Queue panel (acting on
whatever's currently playing) and the Playlists panel (so starting a playlist
from that list doesn't require switching panels to turn it on first) — both
buttons read and write the same shared autopilot flag on the active queue.
