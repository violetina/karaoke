"""End-to-end: Enhanced LRC survives staging -> approval -> cache -> renderer.

Word timings are only useful if they make it all the way from the caption
fetcher to the highlight. That path crosses stage_sources, staging, localcache
and player, so it is covered here rather than in any single module's tests.
"""
from __future__ import annotations

import json

import pytest

from karaoke import localcache
from karaoke.caption_sync import json3_to_enhanced_lrc
from karaoke.lyrics import Lyrics, parse_enhanced_lrc
from karaoke.player import timeline_from_lyrics
from karaoke.staging import approve_staged, ensure_schema, stage_lyrics

_real_connect = localcache.connect

ENHANCED = (
    "[00:21.92]<00:21.92>Heat <00:22.00>up <00:22.22>here.\n"
    "[01:30.00]<01:30.00>after <01:30.40>riff"
)


@pytest.fixture()
def conn(tmp_path):
    c = _real_connect(tmp_path / "karaoke.db")
    ensure_schema(c)
    yield c
    c.close()


def _stage(conn, synced: str, kind: str = "youtube_caption_manual_en_enhanced"):
    lines, _, _ = parse_enhanced_lrc(synced)
    return stage_lyrics(
        "Test Artist", "Test Song",
        Lyrics(plain="\n".join(t for _, t in lines), synced_raw=synced, lines=lines),
        source_kind=kind, source_url="https://youtu.be/x",
        confidence=0.8, conn=conn,
    )


def test_word_tags_survive_approval_into_cache(conn):
    approve_staged(_stage(conn, ENHANCED), conn=conn)
    cached = localcache.get_cached_lyrics("Test Artist", "Test Song", conn=conn)
    assert cached is not None
    assert "<00:22.00>" in cached.synced_raw


def test_approved_cache_renders_with_real_word_timings(conn):
    approve_staged(_stage(conn, ENHANCED), conn=conn)
    cached = localcache.get_cached_lyrics("Test Artist", "Test Song", conn=conn)
    tl = timeline_from_lyrics(cached)

    assert tl.word_times[0] == pytest.approx([21.92, 22.0, 22.22])
    # Real timings drive the highlight, not interpolation across the 68s gap.
    assert tl.word_index(22.05) == 1
    assert tl.lines[0][1].split()[tl.word_index(22.05)] == "up"


def test_plain_text_is_stored_without_word_tags(conn):
    """The plain copy must stay human-readable for non-karaoke display."""
    approve_staged(_stage(conn, ENHANCED), conn=conn)
    cached = localcache.get_cached_lyrics("Test Artist", "Test Song", conn=conn)
    assert "<" not in cached.plain
    assert "Heat up here." in cached.plain


def test_gap_after_enhanced_line_is_flagged_as_rest(conn):
    approve_staged(_stage(conn, ENHANCED), conn=conn)
    tl = timeline_from_lyrics(
        localcache.get_cached_lyrics("Test Artist", "Test Song", conn=conn)
    )
    assert tl.in_gap(50.0) is True
    assert tl.in_gap(22.1) is False


def test_full_json3_to_render_pipeline(conn):
    """json3 caption payload -> Enhanced LRC -> cache -> word highlight."""
    payload = json.dumps({"events": [
        {"tStartMs": 21920, "segs": [
            {"utf8": "Heat", "tOffsetMs": 0},
            {"utf8": " up", "tOffsetMs": 80},
            {"utf8": " here.", "tOffsetMs": 300},
        ]},
    ]})
    approve_staged(_stage(conn, json3_to_enhanced_lrc(payload)), conn=conn)
    tl = timeline_from_lyrics(
        localcache.get_cached_lyrics("Test Artist", "Test Song", conn=conn)
    )
    assert tl.word_times[0] == pytest.approx([21.92, 22.0, 22.22])
    assert tl.word_index(22.25) == 2


def test_plain_lrc_still_approves_and_falls_back(conn):
    """A caption track without word tags must keep working (interpolation)."""
    approve_staged(
        _stage(conn, "[00:10.00]one two three four\n[00:14.00]next",
               kind="youtube_caption_automatic_en_synced"),
        conn=conn,
    )
    tl = timeline_from_lyrics(
        localcache.get_cached_lyrics("Test Artist", "Test Song", conn=conn)
    )
    assert tl.word_times == {}
    assert tl.word_index(13.9) == 3
