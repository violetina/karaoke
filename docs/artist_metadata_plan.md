# Artist identity and metadata — plan

**Status: steps 1–3 and 5 merged (#139–#141), step 4 in progress.** Written 2026-10-25.

The short version: the library has no artist entity. An artist is whatever
string the track came with, so "Bonnie "Prince" Bill", "Bonnie 'Prince' Bill"
and "Bonnie Prince Billy" are three acts, "Gorillaz, Fatoumata Diawara" is a
fourth, and the only artist facts are the broad genres in `artist_genres`.
This plan gives every track a link to real artists, identified by MusicBrainz
ID (MBID), and caches what MusicBrainz and Wikidata know about them.

## Why this exists

Found while reviewing smart playlists (PRs #136–#138):

- **Name variants slip past "two songs per act".** Spellings and
  "X & his orchestra" forms count as separate acts, so Folk and Soul leaned
  on a handful of artists.
- **Genre coverage was thin.** `classify_artists.py` had been broken since the
  Postgres migration (17 Sept), so only the 218 seed-list artists had genres
  out of ~3,600 artist strings.
- **Name lookups collide.** Searching MusicBrainz by name alone classified
  "Phoenix" (the French band) as a classical artist with the same name.
- **Equal weights.** Every artist genre has weight 1.0, so one "blues rock"
  tag put The Doors in Blues. MusicBrainz genres carry vote counts; the
  current tables drop them.

## Sources (probed 2026-10-25)

| Source | What it gives | Access | Use |
| --- | --- | --- | --- |
| **MusicBrainz** | MBID, type, country, life span, **aliases**, **genres with vote counts**, members and collaborations, links to Wikidata, Discogs, AllMusic, Last.fm, Bandcamp | free, **1 request/s** per IP, User-Agent with contact required | identity spine |
| **Wikidata** | genres, country, labels, members, influences, instruments, Commons image, IDs on Spotify, Discogs, Apple Music, AllMusic, Last.fm | free; SPARQL can fetch hundreds of artists by MBID (property P434) in one query | structured facts, cross-links |
| ListenBrainz | similar artists | free, no key | DJ and graph links |
| Last.fm | similar artists, listener tags | free API key | optional |
| Discogs | fine-grained styles (strong for electronic and jazz) | token, 60 requests/min | optional |
| Wikipedia | short bio | free | optional |
| Spotify | artist genres, related artists | cut for new apps, tight quotas | not used |

One lookup for Bonnie "Prince" Billy returned six aliases covering every
spelling in the library, genres folk 3 / americana 2 / singer-songwriter 2 /
country 1, "is person: Will Oldham", and Wikidata Q709464. Wikidata added
Drag City and Domino as labels and the artist's Spotify, Discogs, Apple Music
and Last.fm IDs.

## Design

### 1. Identity through a track, not a name

For each distinct artist string, search MusicBrainz for **one of its
recordings**: the cleaned title (no "(feat. …)", "- Remaster", "[Live]") and
the first credited name. Accept a recording only when the title matches and
the credited names match the string. Prefer the one whose length matches the
track's duration. Its artist credit gives every artist on the song with an
MBID, and the join phrases ("feat.", "&", ",") say who is main and who is a
guest.

A recording ties the name to music that is actually in the library, which is
what "Phoenix" lacked. Only when no recording matches does it fall back to a
name search, and then only on an exact name or alias match that is
**unambiguous**. Two artists called "Phoenix" means unresolved, not a guess.

Resolution is per artist string, not per track. The ~3,600 strings plus one
artist fetch per new MBID come to about 7–8k requests, roughly 2–2½ hours at
1 request/s. All tracks with the same string share the result.

### 2. Storage (Postgres, `src/karaoke/artist_identity.py`)

- `artists`: one row per MBID. Name, sort name, type, country, begin/end,
  disambiguation, Wikidata QID, MusicBrainz genres **with vote counts**, the
  raw response (for later steps, without re-fetching), and when it was fetched.
- `artist_aliases`: every normalised spelling (name, sort name, aliases), each
  pointing to an MBID. This is what makes "Bonnie Prince Billy" and "Bonnie
  'Prince' Bill" one act.
- `track_artists`: track → MBIDs in credit order, with role (`main` or
  `featured`), the name as credited, the join phrase, the recording MBID, and
  how it was resolved (`recording` or `name`).
- `artist_lookups`: one row per artist string tried: `resolved`, `unmatched`
  (MusicBrainz has nothing convincing; not retried unless asked) or `failed`
  (MusicBrainz unreachable; retried next run).

`scripts/resolve_artists.py` runs it: biggest artist strings first,
`--limit`, `--dry-run`, `--retry-unmatched`.

### 3. Wikidata enrichment (`src/karaoke/artist_wikidata.py`, done)

Each resolved artist's Wikidata item comes from MusicBrainz's link, or else
from a batched SPARQL query for the item whose MusicBrainz ID (P434) is the
artist's. The entity API then fetches 50 items per request, and one more pass
labels everything they reference. That keeps the whole library to a few
hundred requests, a matter of minutes.

`artist_wikidata` keeps, per MBID:
- the item: QID, label, description
- labelled item lists: kind (person, group), genres, countries (origin,
  falling back to citizenship), record labels, members, bands, influences,
  instruments
- dates: formed or born, dissolved or died, at Wikidata's precision
- the Commons image file name (not downloaded)
- IDs on Spotify, Discogs, AllMusic, Last.fm, Apple Music, Deezer and YouTube
- the English and Dutch Wikipedia titles

Preferred statements win, and deprecated ones never count. An artist with no
item gets a row with an empty QID, so re-runs skip it until `--refresh`.
`scripts/enrich_artists_wikidata.py` runs it after `resolve_artists.py`.

Pseudonyms share an item: Bonnie "Prince" Billy's item is Will Oldham's.

### 4. OpenSearch index (`src/karaoke/artist_index.py`)

`karaoke-artists` holds one document per resolved artist, rebuilt from
Postgres, which stays the source of truth:

- **Search:** name and every alias in an accent-folded `search_as_you_type`
  field, plus fuzzy matching on name and aliases. "bony prins bily" finds
  Bonnie "Prince" Billy, "einsturzende" finds Einstürzende Neubauten.
- **Facets:** main genre and strong broad families (the rule smart playlists
  use), voted genres, country (MusicBrainz code and Wikidata names), kind,
  record labels, instruments, and `began_decade`. That is the decade an
  artist was *born* (people) or *formed* (groups), not when they were active:
  Aphex Twin is 1970s.
- **Links:** members, bands and influences by name, with the MBID when that
  artist is in the library too. Collaborators are artists credited together
  on a library track, up to 15, most shared tracks first.
- **Library:** the artist strings that resolve to the artist, and track counts
  as main and as guest.

A rebuild fills a fresh `karaoke-artists-<timestamp>` index and moves the
alias onto it in one step, then deletes the old one. `make artist-index`
rebuilds it, `make artist-index Q="frank z"` searches it, and `make artists`
runs the whole chain (resolve, Wikidata, index).

Similar artists (ListenBrainz or Last.fm) are not fetched yet; collaborations
and members are the links available now.

### 5. Use it

Done:

- **Genres from votes.** `resolve_artists.py` ends by rewriting
  `artist_genres` for every resolved string (`--genres-only` does just that;
  `make artists` runs it again after Wikidata). The source is its main
  credited artists' MusicBrainz genres (curated genres, else genre-like tags),
  weighted by votes, keeping genres with at least a quarter of the top vote.
  Without votes, Wikidata's genres count one each. Without either, the
  name-searched classifier's guess is dropped, since it was often another
  artist of the same name ("Liminal", an Australian psych band, came out
  latin); the curated seed list stays. Guests do not lend a song their genre. Every reader (search,
  graph, TUI filter, smart playlists) picks this up without changes.
- **Weighted main genre.** `genre_ids` and `genre_options` sum weights
  instead of counting tags, and drop a family with half or less of an
  artist's strongest.
- **Acts by MBID.** Smart playlists count "two songs per act" and "not the
  same act twice in a row" by first credited MBID.

- **New songs.** Post-processing has an artist step: a newly ingested song is
  linked to its artists, a new artist string is resolved through that song,
  and new artists get Wikidata and an index document. The CLAP step labels a
  genre too, so a new song by an unknown artist is never genre-less. See
  `docs/howto-post-processing.md`.

Next: the graph view and the DJ can link artists by members, collaborations,
influences and similarity.

## Open questions

- **Junk artist strings.** Album titles and compilations sit in the artist
  field ("M.Davis & J.Coltrane - The Complete Columbia Recordings…",
  "Romantic Collection", "GUITAR IDOLS"). They should end `unmatched`. Fixing
  them is a data clean-up, not this job.
- **Pseudonyms.** Wikidata lists three MBIDs for Will Oldham (Palace Music,
  Bonnie "Prince" Billy, his own name). Whether they count as one act for the
  per-act cap is a later choice. This plan keeps MusicBrainz's split.
- **Refresh policy.** MusicBrainz genres change slowly. A yearly re-fetch, or
  on demand, is enough.
- **Licensing.** Wikidata and MusicBrainz core data are CC0. MusicBrainz tags
  and genres are CC BY-NC-SA, which is fine for this personal library.
  Commons images carry their own licenses and are not downloaded by these
  steps.
