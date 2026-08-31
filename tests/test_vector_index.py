"""Tests for SQLite-derived OpenSearch vector indexing."""
from __future__ import annotations

from pathlib import Path

from karaoke import localcache, vector_index
from karaoke.lyrics import Lyrics


class FakeIndices:
    def __init__(self):
        self.created = []
        self.refreshed = []

    def exists(self, index):
        return index in self.created

    def create(self, index, body):
        self.created.append(index)

    def refresh(self, index):
        self.refreshed.append(index)


class FakeOpenSearch:
    def __init__(self):
        self.indices = FakeIndices()
        self.docs = []

    def index(self, index, id, body):
        self.docs.append((index, id, body))


def _seed_db(path: Path) -> None:
    conn = localcache.connect(path)
    try:
        ly = Lyrics(
            plain="first line\nsecond line",
            synced_raw="[00:01.00] first line\n[00:04.00] second line",
            source="whisper",
        )
        localcache.add_track_and_lyrics(
            "Tom Waits",
            "Army Ants",
            ly,
            album="Real Gone",
            duration=210.0,
            url="https://www.youtube.com/watch?v=_3tkup9b-iM",
            conn=conn,
        )
    finally:
        conn.close()


def test_build_track_doc_from_sqlite(tmp_path):
    db = tmp_path / "karaoke.db"
    _seed_db(db)
    conn = localcache.connect(db)
    try:
        rows = list(vector_index.iter_track_rows(conn))
    finally:
        conn.close()

    assert len(rows) == 1
    doc = vector_index.build_track_doc(rows[0], embed=False)
    assert doc["track_id"] == 1
    assert doc["artist"] == "Tom Waits"
    assert doc["title"] == "Army Ants"
    assert doc["source"] == "sqlite"
    assert doc["source_kind"] == "youtube"
    assert doc["source_url"] == "https://www.youtube.com/watch?v=_3tkup9b-iM"
    assert doc["has_synced"] is True
    assert doc["lyrics_source"] == "whisper"
    assert "lyrics_vector" not in doc


def test_rebuild_from_sqlite_indexes_tracks_and_lines(tmp_path):
    db = tmp_path / "karaoke.db"
    _seed_db(db)
    fake = FakeOpenSearch()

    stats = vector_index.rebuild_from_sqlite(
        db_path=str(db),
        embed=False,
        include_lines=True,
        os_client=fake,
    )

    assert stats.seen == 1
    assert stats.indexed == 1
    assert stats.errors == 0
    assert stats.line_docs == 2
    indexes = [item[0] for item in fake.docs]
    assert indexes == ["tracks", "tracks-lines", "tracks-lines"]
    assert fake.docs[0][1] == "sqlite:1"
    assert fake.docs[1][1] == "sqlite-line:1:0"
    assert fake.docs[1][2]["duration_s"] == 3.0
    assert fake.docs[2][2]["duration_s"] is None


def test_vector_index_main_dry_run_no_cluster(tmp_path, capsys):
    db = tmp_path / "karaoke.db"
    _seed_db(db)

    rc = vector_index.vector_index_main([
        "--dry-run",
        "--no-embed",
        "--lines",
        "--db",
        str(db),
    ])

    assert rc == 0
    out = capsys.readouterr().out
    assert "dry-run: seen=1" in out
    assert "line_docs=2" in out
