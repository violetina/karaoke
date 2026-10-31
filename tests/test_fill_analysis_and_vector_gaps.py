"""Regression test for the Postgres SQL in fill_analysis_and_vector_gaps.py.

The script previously built a SQLite-style `?,?,?` placeholder string and
passed a positional list to psycopg -- which raises
``the query has 0 placeholders but N parameters were passed`` because
psycopg (unlike sqlite3) counts literal `%s`/`?` occurrences in the SQL
text itself. Caught live via the Backstage gap-fill wiring; fixed by using
a single ``= ANY(%s)`` parameterized against the whole id list instead.
"""
from unittest.mock import MagicMock, patch

import scripts.fill_analysis_and_vector_gaps as gap_fill


class _FakeCursor:
    """Records every execute() call; returns canned fetchall() results in order."""

    def __init__(self, fetchall_results):
        self.calls = []
        self._results = list(fetchall_results)

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchall(self):
        return self._results.pop(0) if self._results else []


def _row(**kw):
    d = dict(kw)
    d_get = d.get

    class _Row(dict):
        def __getitem__(self, k):
            return dict.get(self, k)
    r = _Row(d)
    return r


def test_gap_fill_track_lookup_uses_any_not_sqlite_placeholders(tmp_path, monkeypatch):
    missing_analysis = [_row(track_id=1, artist="A", title="One", url="", kind="youtube")]
    missing_genre = [_row(track_id=2, artist="B", title="Two", url="", kind="youtube")]
    lookup_rows = [_row(track_id=1, artist="A", title="One"), _row(track_id=2, artist="B", title="Two")]

    cursor = _FakeCursor([missing_analysis, missing_genre, lookup_rows])
    fake_conn = MagicMock()
    fake_conn.cursor.return_value = cursor
    fake_conn.__enter__ = lambda s: fake_conn
    fake_conn.__exit__ = lambda s, *a: False

    monkeypatch.setattr(gap_fill, "Path", gap_fill.Path)  # keep real Path
    home_dir = tmp_path / "home"
    (home_dir / ".local" / "share" / "karaoke").mkdir(parents=True)

    with patch("karaoke.localcache.connect", return_value=fake_conn), \
         patch.object(gap_fill.Path, "home", return_value=home_dir), \
         patch("karaoke.autoclassify.run", return_value={}), \
         patch("karaoke.vector_index.rebuild_from_sqlite",
               return_value=MagicMock(indexed=0, line_docs=0)):
        gap_fill._run()

    # The third execute() call is the track_id lookup for the gap-fill playlist.
    lookup_sql, lookup_params = cursor.calls[2]
    assert "= ANY(%s)" in lookup_sql
    assert "?" not in lookup_sql
    assert lookup_params == ([1, 2],) or sorted(lookup_params[0]) == [1, 2]
