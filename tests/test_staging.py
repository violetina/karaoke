"""Tests for unapproved lyrics staging."""

import pytest

from karaoke import localcache, staging
from karaoke.lyrics import Lyrics


def _conn(tmp_path):
    return localcache.connect(tmp_path / "karaoke.db")


def test_stage_list_show_and_approve_into_local_cache(tmp_path):
    conn = _conn(tmp_path)
    staged_id = staging.stage_lyrics(
        "The Cure", "A Forest", Lyrics(plain="Come closer and see", source="youtube_caption"),
        source_kind="youtube_caption_manual_en", source_url="https://youtu.be/x",
        confidence=0.65, conn=conn,
    )

    items = staging.list_staged(conn=conn)
    assert [i.id for i in items] == [staged_id]
    assert items[0].status == "pending"
    assert items[0].plain_lyrics == "Come closer and see"

    approved = staging.approve_staged(staged_id, conn=conn)
    assert approved.status == "approved"
    cached = localcache.get_cached_lyrics("The Cure", "A Forest", conn=conn)
    assert cached is not None
    assert cached.plain == "Come closer and see"
    assert cached.source == "youtube_caption_manual_en"


def test_reject_does_not_populate_local_cache(tmp_path):
    conn = _conn(tmp_path)
    staged_id = staging.stage_lyrics(
        "A", "B", Lyrics(plain="candidate", source="web"),
        source_kind="web", conn=conn,
    )

    rejected = staging.reject_staged(staged_id, conn=conn)
    assert rejected.status == "rejected"
    assert localcache.get_cached_lyrics("A", "B", conn=conn) is None


def test_stage_empty_lyrics_rejected(tmp_path):
    conn = _conn(tmp_path)
    with pytest.raises(ValueError):
        staging.stage_lyrics("A", "B", Lyrics(), source_kind="web", conn=conn)
