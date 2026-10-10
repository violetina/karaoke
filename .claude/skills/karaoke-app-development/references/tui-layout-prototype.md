# Textual TUI layout prototyping notes

Use this when turning a rough editable UI sketch into the first runnable `karaoke` TUI slice.

## Pattern that worked

1. Keep the existing focused TUI (`src/karaoke/browse.py`) stable while experimenting in a new module such as `src/karaoke/tui.py`.
2. Build a static Textual shell first: Header/Footer plus left settings, center library/lyrics, and right mood/visual panels.
3. Feed the prototype from SQLite (`localcache.connect()`); SQLite remains the operational source of truth. OpenSearch/vector features should stay optional and derived.
4. Reuse existing behavior instead of duplicating it:
   - `browse.open_song_url()` for Enter/open behavior.
   - `localcache` queries for library rows and approved lyrics.
   - `sentiment.mood_of()` for mood square/visual cues.
5. Put pure formatting helpers (`lyric_preview`, mood selection, first-line extraction) behind simple unit tests.
6. Smoke-test the actual Textual app headlessly with `App.run_test()` so import/layout regressions are caught without opening an interactive terminal.

## Useful verification commands

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/test_tui.py -q
PYTHONPATH=src .venv/bin/python - <<'PY'
from karaoke.tui import KaraokeTui
import asyncio

async def main():
    app = KaraokeTui()
    async with app.run_test(size=(120, 36)):
        assert app.query_one('#library')
        assert app.query_one('#mode-select')
        assert app.query_one('#mood-square')
    print('karaoke-tui headless smoke OK')

asyncio.run(main())
PY
make lint
PYTHONPATH=src .venv/bin/python -m pytest -q
```

## Pitfalls

- Do not wire playback and synchronization into the prototype all at once. First create editable visual structure, then move one behavior at a time from `browse.py` and `player.py` into widgets.
- Do not make the new prototype replace `karaoke-browse` until the user has iterated on the design. Add a separate `karaoke-tui` entrypoint / `make tui` target.
- Avoid using database values that may be NULL as Textual row keys. Maintain a parallel Python list and index it by `DataTable.cursor_row`.
