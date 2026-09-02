"""Tests for staging whitelist + logger stream control."""

import logging

from karaoke import localcache, staging
from karaoke.logger import stream_logs
from karaoke.lyrics import Lyrics


def test_whitelist_staged_approves_into_cache(tmp_path):
    conn = localcache.connect(tmp_path / "k.db")
    sid = staging.stage_lyrics(
        "The Cure", "A Forest",
        Lyrics(plain="Come closer and see", source="youtube_caption"),
        source_kind="youtube_caption", conn=conn,
    )
    item = staging.whitelist_staged(sid, conn=conn)
    assert item.status == "approved"
    cached = localcache.get_cached_lyrics("The Cure", "A Forest", conn=conn)
    assert cached is not None and cached.plain == "Come closer and see"


def test_find_pending_by_track(tmp_path):
    conn = localcache.connect(tmp_path / "k.db")
    staging.stage_lyrics(
        "A", "B", Lyrics(plain="c", source="web"),
        source_kind="web", conn=conn,
    )
    found = staging.find_pending_by_track("A", "B", conn=conn)
    assert found is not None
    assert (found.artist, found.title) == ("A", "B")
    assert staging.find_pending_by_track("No", "Match", conn=conn) is None


def test_stream_logs_levels():
    h = stream_logs("err")
    assert h is not None and h.level == logging.WARNING
    h2 = stream_logs("full")
    assert h2 is not None and h2.level == logging.DEBUG
    # only one console handler at a time (idempotent)
    logger = logging.getLogger("karaoke")
    consoles = [x for x in logger.handlers if getattr(x, "name", "") == "karaoke-console"]
    assert len(consoles) == 1
    assert stream_logs("off") is None
    consoles = [x for x in logger.handlers if getattr(x, "name", "") == "karaoke-console"]
    assert len(consoles) == 0
