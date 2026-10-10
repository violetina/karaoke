# "Keep the vibe going" — whole-queue audio suggestions

Feature: append tracks that fit the *entire* current queue, not just the last
song. Lives in `karaoke/queue_suggest.py`, exposed as `POST /api/queue/suggest`,
`ApiClient.suggest_queue()`, and TUI key `G` (`action_suggest_queue`).

## Design that matters
- **Seed on every queue member**, pool each seed's nearest audio neighbours,
  and rank **breadth of fit first**: sort by `(seeds_matched, summed_score)`
  descending, so a track close to several queued songs beats a single-outlier
  match. This is what makes it "keep the vibe" rather than "more like the last one".
- **Similarity precedence = CLAP first, spectral fallback PER SEED** — same order
  as `karaoke.search.similar_main`. Use `search.sounds_like_track(tid, k)` (CLAP);
  if it returns `[]`, fall back to `search.similar_sounding(tid, k)` (spectral).
  Report which space answered so mixed results aren't presented as one cosine scale.
- Exclude tracks already in the queue; cap per artist (default 2); over-fetch
  per seed (`PER_SEED=20`) so dedup + cap still leave a full list.
- Needs the OpenSearch `karaoke-clap` / `karaoke-audio` indexes. If no seed has a
  vector it returns `[]` — the caller must say so, not silently add nothing.

## API/client/TUI wiring
- `api.py`: `QueueSuggestRequest{track_ids, limit, per_artist}` ->
  `SuggestionResponse{track_id, artist, title, score, seeds_matched, space, url}`.
  Endpoint is read-only (OpenSearch reads only) so it stays container-safe in the
  library API. `queue_suggest.playable_url(track_id, conn)` fills the play URL
  (YouTube-preferred, same ordering as browse).
- `ApiClient.suggest_queue()` uses `self.lib_url` + `_http_post`, with an
  in-process `queue_suggest.suggest_for_queue` fallback when HTTP is unreachable.
- TUI: `action_suggest_queue` collects `track_id`s from `self._queue`, runs the
  call on a `run_worker(..., thread=True)` worker, and appends via
  `self.call_from_thread(self._apply_suggestions, picks)`. All queue/table
  mutation happens in `_apply_suggestions` on the UI thread.

## Verifying live (not just unit-mocked)
Unit tests monkeypatch `search.sounds_like_track` / `similar_sounding` to test the
pooling/cap/fallback logic without OpenSearch. But ALSO verify end-to-end:
1. `systemctl --user restart karaoke-api.service` after editing API code — the
   running service holds the OLD module in memory; a new endpoint 404s until
   restart. (General lesson: any code change to `karaoke-api` / `karaoke-ctrl-api`
   only takes effect after a `systemctl --user restart`.)
2. Pick seeds that actually have CLAP vectors:
   `client().search(index=clap_vector.CLAP_INDEX, body={"size":6,"query":{"match_all":{}}})`.
3. `curl -X POST .../api/queue/suggest -d '{"track_ids":[A,B],"limit":5}'` and
   confirm `space:"clap"` and that cross-seed matches show `seeds_matched:2`
   ranked above `seeds_matched:1`. Seeds without CLAP correctly show `space:"spectral"`.

## Layering note (future work)
This is the **content** layer (query-by-example, zero new tables, works day one).
The **behavioural** layer the user floated — persist searches + kept queues to
learn co-occurrence ("in queues that contained X, what else showed up") — needs
new tables (`saved_queries`, `queue_snapshots`, `queue_items`) and only pays off
after usage history accumulates. `play_events` already logs `mode='query'`, so
raw search history may be mineable without a new table. Sequence content first.
