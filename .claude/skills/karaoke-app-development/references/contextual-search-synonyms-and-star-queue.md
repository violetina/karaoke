# Contextual Search, Synonyms, and Star Queue Search

Session lessons from improving karaoke search strictness and contextual matching.

## OpenSearch synonym analyzer for keyword search

When keyword/contextual search feels too strict, add synonym expansion at the OpenSearch analyzer layer rather than hand-expanding user queries in every caller.

Recommended pattern:
- Put a reusable `synonym_settings()` helper in `src/karaoke/osclient.py`.
- Attach its `analysis` block at `settings.analysis`, not nested inside `settings.index.analysis`.
- Apply `analyzer` and `search_analyzer` to searchable text fields:
  - track index: `title`, `artist`, `album`, `plain_lyrics`
  - line index: `artist`, `title`, `text`, `context`
  - note index: `artist`, `title`, `album`, `text`
- Keep synonym groups compact and music/vibe-oriented. Good durable groups include:
  - `sad, depressed, melancholy, blue, sorrow, gloom, tearful, crying, weeping, lonely, dark, tender`
  - `happy, glad, joyful, upbeat, cheerful, bright, high energy, energetic`
  - `chill, mellow, relax, relaxing, calm, slow, down, ambient`
  - `rock, stoner rock, psychedelic rock, punk rock, post-punk, grunge, hard rock, heavy metal, metal`
  - `hip hop, rap, trap, r and b, rnb, soul, funk`
  - `techno, electronic, electronica, electro, synth, synthesizer, beats, drum and bass, dnb, dance, house, rave, club, party, EDM`

## Reindex requirement

Analyzer changes do not retrofit existing text fields. After changing analyzer/mapping code, recreate only derived OpenSearch indexes and rebuild from SQLite:

```bash
curl -s -XDELETE 'http://localhost:9200/tracks,tracks-lines,tracks-notes?ignore_unavailable=true'
PYTHONPATH=src .venv/bin/python -m karaoke.vector_index --rebuild --lines --notes
```

SQLite remains the source of truth; this deletes only rebuildable derived OpenSearch indexes.

Verify the analyzer before trusting it:

```bash
curl -s 'http://localhost:9200/tracks/_analyze' \
  -H 'Content-Type: application/json' \
  -d '{"analyzer":"synonym_analyzer","text":"melancholy"}'
```

Expected: tokens include the original word plus `SYNONYM` tokens such as `sad`, `depressed`, `blue`, `sorrow`, and `gloom`.

## `*` search means show-all queue

User expectation: the TUI search input should accept `*` as an explicit "show all" query so the queue can be populated and then narrowed by filters.

Implementation pattern:
- In `KaraokeTui._background_search`, special-case `query.strip() == "*"`.
- Return all playable, non-album-upload tracks with approved lyrics via a helper such as `_all_queue_rows(conn)`.
- Include `track_id`, `artist`, `title`, `url`, `genre`, and `energy` in those rows so existing mood/genre queue filtering can run without extra DB lookups.
- Call `_apply_mood_filter(rows, conn)` after collecting all rows so Genre and Mood selectors narrow the queue live.
- Ensure `track_analysis.ensure_schema(conn)` before joining `track_analysis`; test databases may not have that table yet.
- Document the workflow in `docs/tui.md`: choose a genre such as `pop`, search `*`, and the queue becomes all pop tracks.

## Hybrid search plan (Option B)

For the next step, prefer hybrid retrieval over more synonym expansion. Add a `hybrid_search()` in `src/karaoke/search.py` that combines `keyword_search()` and `semantic_search()` using Reciprocal Rank Fusion (RRF), because OpenSearch keyword `_score` and vector cosine scores are not directly comparable.

Keep it opt-in first (`--hybrid`) so exact/local TUI search remains offline and predictable until live validation confirms the ranking is better.
