"""Tests for the local SQLite lyrics cache + play/discovery stats."""
from __future__ import annotations

from karaoke import localcache
from karaoke.lyrics import Lyrics


def _conn(tmp_path):
    return localcache.connect(tmp_path / "karaoke.db")


def test_put_and_get_cached_lyrics_roundtrip(tmp_path):
    c = _conn(tmp_path)
    ly = Lyrics(
        plain="line one\nline two",
        synced_raw="[00:01.00] line one\n[00:05.00] line two",
        source="lrclib",
        lines=[(1.0, "line one"), (5.0, "line two")],
    )
    localcache.put_cached_lyrics("R.E.M.", "Losing My Religion", ly, conn=c)

    got = localcache.get_cached_lyrics("R.E.M.", "Losing My Religion", conn=c)
    assert got is not None
    assert got.has_synced
    assert got.source == "lrclib"
    assert got.lines[0] == (1.0, "line one")


def test_cache_key_is_case_insensitive(tmp_path):
    c = _conn(tmp_path)
    ly = Lyrics(plain="x", synced_raw="[00:01.00] x", source="lrclib",
                lines=[(1.0, "x")])
    localcache.put_cached_lyrics("R.E.M.", "Losing My Religion", ly, conn=c)
    got = localcache.get_cached_lyrics("r.e.m.", "losing my religion", conn=c)
    assert got is not None and got.has_synced


def test_get_cached_lyrics_miss_returns_none(tmp_path):
    c = _conn(tmp_path)
    assert localcache.get_cached_lyrics("Nobody", "Nothing", conn=c) is None


def test_put_empty_lyrics_is_noop(tmp_path):
    c = _conn(tmp_path)
    localcache.put_cached_lyrics("A", "B", Lyrics(), conn=c)
    assert localcache.get_cached_lyrics("A", "B", conn=c) is None


def test_put_is_upsert(tmp_path):
    c = _conn(tmp_path)
    localcache.put_cached_lyrics(
        "A", "B", Lyrics(plain="old", source="lrclib"), conn=c)
    localcache.put_cached_lyrics(
        "A", "B",
        Lyrics(plain="new", synced_raw="[00:02.00] new", source="whisper",
               lines=[(2.0, "new")]),
        conn=c,
    )
    got = localcache.get_cached_lyrics("A", "B", conn=c)
    assert got is not None
    assert got.plain == "new"
    assert got.source == "whisper"
    assert got.has_synced


def test_summarize_counts_plays_and_discoveries(tmp_path):
    c = _conn(tmp_path)
    localcache.log_event("radio", "discover", artist="A", title="S1", conn=c)
    localcache.log_event("radio", "play", artist="A", title="S1",
                         source="lrclib", has_synced=True, conn=c)
    localcache.log_event("radio", "play", artist="A", title="S1",
                         source="lrclib", has_synced=True, conn=c)
    localcache.log_event("spotify", "play", artist="B", title="S2",
                         source="lrclib", has_synced=True, conn=c)
    localcache.log_event("radio", "cache_hit", artist="A", title="S1", conn=c)
    localcache.log_event("radio", "cache_miss", artist="A", title="S1", conn=c)

    s = localcache.summarize(conn=c)
    assert s.plays == 3
    assert s.discoveries == 1
    assert s.cache_hits == 1
    assert s.cache_misses == 1
    assert 0.49 < s.cache_hit_rate < 0.51
    assert s.distinct_tracks == 2
    assert s.distinct_artists == 2
    # Most-played track first.
    assert s.top_tracks[0] == ("A", "S1", 3)
    assert s.top_artists[0] == ("A", 3)
    assert dict(s.by_mode)["radio"] == 3


def test_summarize_empty(tmp_path):
    c = _conn(tmp_path)
    s = localcache.summarize(conn=c)
    assert s.total_events == 0
    assert s.plays == 0
    assert s.cache_hit_rate == 0.0
    assert s.top_tracks == []


def test_add_track_source_does_not_create_empty_lyrics(tmp_path):
    c = _conn(tmp_path)
    track_id = localcache.add_track_source(
        "Mr. Bungle",
        "Violenza Domestica",
        duration=210.0,
        url="https://www.youtube.com/watch?v=bXWHf2HH8jY",
        kind="youtube",
        conn=c,
    )

    assert track_id > 0
    assert localcache.get_cached_lyrics("Mr. Bungle", "Violenza Domestica", conn=c) is None
    found = localcache.find_track_by_url(
        "https://www.youtube.com/watch?v=bXWHf2HH8jY", c
    )
    assert found == (track_id, "Mr. Bungle", "Violenza Domestica")
    assert c.execute("SELECT count(*) FROM lyrics").fetchone()[0] == 0


def test_delete_empty_approved_lyrics_keeps_real_lyrics(tmp_path):
    c = _conn(tmp_path)
    localcache.add_track_and_lyrics("Empty", "Placeholder", Lyrics(), conn=c)
    localcache.add_track_and_lyrics("Real", "Song", Lyrics(plain="words", source="manual"), conn=c)
    # Simulate legacy bad rows from the earlier cache indexer.
    empty_id = localcache.find_track_id("Empty", "Placeholder", c)
    assert empty_id is not None
    c.execute(
        "INSERT INTO lyrics (track_id, kind, source, synced_lyrics, plain_lyrics) VALUES (?, 'approved', 'cache', '', '')",
        (empty_id,),
    )
    c.commit()

    assert localcache.delete_empty_approved_lyrics(conn=c) == 1
    assert c.execute("SELECT count(*) FROM lyrics").fetchone()[0] == 1
    got = localcache.get_cached_lyrics("Real", "Song", conn=c)
    assert got is not None
    assert got.plain == "words"


def test_log_event_ignores_blank_titles_in_top_lists(tmp_path):
    c = _conn(tmp_path)
    localcache.log_event("query", "play", artist="", title="", conn=c)
    s = localcache.summarize(conn=c)
    assert s.plays == 1
    assert s.distinct_tracks == 0
    assert s.top_tracks == []
