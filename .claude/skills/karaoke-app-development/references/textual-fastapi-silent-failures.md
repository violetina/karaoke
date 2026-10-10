# Silent failures: Textual + FastAPI + player routing

Class of bug this session kept hitting: things that fail *silently* — no
exception, no log — leaving the UI blank, unplayable, or serving the wrong app.
Each has a cheap, repeatable check.

## FastAPI `response_model` silently strips undeclared fields
`@app.get("/api/tracks", response_model=list[TrackResponse])` filters every
returned dict down to the Pydantic model's declared fields. If `list_tracks()`
starts returning new columns (`key`, `bpm`, `energy`, `brightness`, `genre`)
but `TrackResponse` doesn't declare them, they are **dropped from the HTTP
response with no error**. The in-process/SQLite path still has them, so the bug
only shows over HTTP (e.g. `ApiClient` in HTTP mode → browse list has no
analysis columns).
- Fix: add the field to the response model too, not just the SELECT + dict.
- Verify: `curl -s :8000/api/tracks?limit=1 | python -m json.tool` and confirm
  the new keys are actually present. Do this after ANY change to what an
  endpoint returns.

## `python -m textual serve ...` launches the demo, not your app
`textual serve "python -m karaoke.tui"` (and `python -m textual serve ...`)
does **not** reliably serve your app — it can launch Textual's built-in
welcome/demo ("What is Textual?", keyboard-warriors screen). Symptom: `make
web`/`make webtui` shows the demo, not karaoke.
- Fix: call the API directly. `textual-serve` is a separate installed package.
  ```python
  from textual_serve.server import Server
  Server(f"{sys.executable} -m karaoke.tui", title="Karaoke TUI",
         host="0.0.0.0", port=8001).serve()
  ```
  (`scripts/web_serve.py` does this; PORT/HOST are env-overridable. Note the
  served port is 8001, not the library API's 8080/8000.)
- Verify: `curl -I http://127.0.0.1:8001/` → `HTTP/1.1 200 OK`; the process log
  should say `Serving '.../python -m karaoke.tui'`.

## Duplicate widget `id` crashes the Textual mount → blank panel
Two widgets with the same `id=` (e.g. a `#mood-select` in both the sidebar and
the library overlay) break `compose()`/mount. Symptom seen: the library
DataTable rendered but stayed **empty**, and the overlay looked wrong.
- Rule: every `id` is unique across the whole app, not just per container.
- Verify live without a terminal: headless mount and count.
  ```python
  async with app.run_test(size=(160,50)) as pilot:
      await pilot.pause()
      assert len(app.query("#mood-select")) == 1
      assert app.query_one("#library").row_count > 0
  ```

## Duplicate method name silently shadows the earlier one
Defining `on_data_table_row_selected` twice in `KaraokeTui` means the second
definition wins and the first never runs. Symptom: selecting a row in the `H`
library overlay did nothing while the queue table worked (the queue handler,
defined later, shadowed the library handler).
- Fix: one dispatcher that branches on `event.data_table.id`
  (`queue` → play index, else → `action_select()`).
- Watch for this whenever two tables/inputs need the same Textual message
  handler — merge, don't redefine.

## `open_song_url` must preserve a direct video ID, not rewrite to search
With `prefer_audio=True`, converting a direct watch link
(`youtube.com/watch?v=VID`) into a `music.youtube.com/search?q=artist+title`
URL lands on a **search results page that never auto-plays**. Symptom: pressing
Enter in browse "does nothing" (opens a list, no playback).
- Rule: if `extract_youtube_id(url)` yields an ID, navigate
  `music.youtube.com/watch?v=VID` (plays immediately). Only fall back to
  `search?q=...` when there is no ID and only artist/title are known.

## `ensure_schema()` before joining `track_analysis`/`track_genre`
`load_songs()` / `list_tracks()` `LEFT JOIN track_analysis` (and
`track_genre`) throw `no such table` on a fresh or temp DB (unit tests build
raw `tracks`/`sources` fixtures). Call `track_analysis.ensure_schema(conn)`
(and rely on `localcache` genre schema) at the top of any code path that joins
these, so the query never fails on un-migrated databases.

## Textual binding keys must be unique (there's a test for it)
`test_binding_keys_are_unique` fails the whole suite on a duplicate key.
`[` / `]` (`left_square_bracket`/`right_square_bracket`) are already bound to
seek. The mood slider used `-` / `+` (`minus`/`equals_sign`) instead. Check the
existing `BINDINGS` list before adding a key.

## Reviewing the TUI visually
Screenshots the user drops during TUI work land in `~/Pictures/Screenshots/`
(newest by mtime). Load the most recent with vision to see exactly what they
see — faster than reasoning about layout blind.
