"""Tests for Audio vs Video synchronization, LRC shifting, and dual-version lyrics storage."""
import pytest
from types import SimpleNamespace

from karaoke import detect, localcache
from karaoke.caption_sync import sync_audio_video_counterparts
from karaoke.lyrics import (
    Lyrics,
    align_audio_and_video_lyrics,
    parse_lrc,
    shift_lrc,
    sync_audio_from_video,
    sync_video_from_audio,
)
from karaoke.playerctl import PlayerMetadata


def test_shift_lrc_positive():
    lrc = "[00:10.00] Line one\n[00:15.50] Line two"
    shifted = shift_lrc(lrc, 5.0)
    assert "[00:15.00] Line one" in shifted
    assert "[00:20.50] Line two" in shifted


def test_shift_lrc_negative_and_clamping():
    lrc = "[00:03.00] Line one\n[00:10.00] Line two"
    shifted = shift_lrc(lrc, -5.0)
    assert "[00:00.00] Line one" in shifted
    assert "[00:05.00] Line two" in shifted


def test_shift_lrc_enhanced():
    lrc = "[00:10.00]<00:10.00>Hello <00:11.50>world"
    shifted = shift_lrc(lrc, 4.25)
    assert "[00:14.25]<00:14.25>Hello <00:15.75>world" in shifted


def test_sync_video_and_audio_roundtrip():
    audio_lrc = "[00:12.00] First line\n[00:18.00] Second line"
    video_lrc = sync_video_from_audio(audio_lrc, 7.5)
    assert "[00:19.50] First line" in video_lrc
    assert "[00:25.50] Second line" in video_lrc

    restored_audio = sync_audio_from_video(video_lrc, 7.5)
    assert "[00:12.00] First line" in restored_audio
    assert "[00:18.00] Second line" in restored_audio


def test_align_audio_and_video_lyrics():
    audio_lrc = """
[00:05.00] Welcome to the show
[00:10.00] Hope you enjoy it
[00:15.00] Goodnight everyone
"""
    # Video has a 6.2s intro before the song starts
    video_lrc = """
[00:11.20] Welcome to the show
[00:16.18] Hope you enjoy it
[00:21.22] Goodnight everyone
"""
    offset = align_audio_and_video_lyrics(audio_lrc, video_lrc)
    assert offset is not None
    assert abs(offset - 6.2) <= 0.05


def test_align_audio_and_video_no_match():
    audio_lrc = "[00:05.00] Totally different song"
    video_lrc = "[00:10.00] Unrelated words here"
    offset = align_audio_and_video_lyrics(audio_lrc, video_lrc)
    assert offset is None


def test_dual_lyrics_and_sync_offsets_storage(tmp_path):
    conn = localcache.connect(tmp_path / "test.db")
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO tracks (artist, title) VALUES ('ArtistX', 'SongY') RETURNING track_id")
        row = cur.fetchone()
        track_id = row["track_id"] if row else 1

        # Store Audio lyrics
        audio_lrc = "[00:10.00] Studio audio lyrics"
        localcache.put_cached_lyrics(
            "ArtistX", "SongY",
            Lyrics(plain="Studio audio lyrics", synced_raw=audio_lrc, source="lrclib"),
            media_type="audio",
            conn=conn,
        )

        # Store Video lyrics
        video_lrc = "[00:15.00] Video caption lyrics"
        localcache.put_cached_lyrics(
            "ArtistX", "SongY",
            Lyrics(plain="Video caption lyrics", synced_raw=video_lrc, source="youtube_caption"),
            media_type="video",
            conn=conn,
        )

        # Query audio vs video lyrics
        retrieved_audio = localcache.get_lyrics_by_track_id(track_id, conn, media_type="audio")
        assert retrieved_audio is not None
        assert retrieved_audio.source == "lrclib"
        assert "[00:10.00]" in retrieved_audio.synced_raw

        retrieved_video = localcache.get_lyrics_by_track_id(track_id, conn, media_type="video")
        assert retrieved_video is not None
        assert retrieved_video.source == "youtube_caption"
        assert "[00:15.00]" in retrieved_video.synced_raw

        # Save separate offsets for audio and video
        localcache.set_sync_offset(track_id, 0.0, conn, mode="audio")
        localcache.set_sync_offset(track_id, 5.0, conn, mode="video")

        assert localcache.get_sync_offset(track_id, conn, mode="audio") == 0.0
        assert localcache.get_sync_offset(track_id, conn, mode="video") == 5.0
    finally:
        conn.close()


def test_detect_media_type_classification():
    video_meta = PlayerMetadata(
        player="chrome",
        artist="Artist",
        title="Song (Official Music Video)",
        url="https://www.youtube.com/watch?v=abcdefghijk",
    )
    det_video = detect.classify(video_meta)
    assert det_video.media_type == "video"

    audio_meta = PlayerMetadata(
        player="chrome",
        artist="Artist",
        title="Song",
        url="https://music.youtube.com/watch?v=abcdefghijk",
    )
    det_audio = detect.classify(audio_meta)
    assert det_audio.media_type == "audio"


def test_sync_audio_video_counterparts_workflow(tmp_path):
    conn = localcache.connect(tmp_path / "sync_test.db")
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO tracks (artist, title) VALUES ('BandZ', 'TrackW') RETURNING track_id")
        row = cur.fetchone()
        track_id = row["track_id"] if row else 1

        # Put audio lyrics
        audio_lrc = "[00:10.00] Line one\n[00:20.00] Line two"
        localcache.put_cached_lyrics(
            "BandZ", "TrackW",
            Lyrics(plain="Line one\nLine two", synced_raw=audio_lrc, source="lrclib"),
            media_type="audio",
            conn=conn,
        )

        # Put video lyrics with 3.5s offset
        video_lrc = "[00:13.50] Line one\n[00:23.50] Line two"
        localcache.put_cached_lyrics(
            "BandZ", "TrackW",
            Lyrics(plain="Line one\nLine two", synced_raw=video_lrc, source="youtube_caption"),
            media_type="video",
            conn=conn,
        )

        res = sync_audio_video_counterparts(track_id, conn)
        assert res["status"] == "ok"
        assert res["has_audio_lyrics"] is True
        assert res["has_video_lyrics"] is True
        assert res["offset"] == 3.5

        # Video offset was automatically stored
        saved_vid_offset = localcache.get_sync_offset(track_id, conn, mode="video")
        assert saved_vid_offset == 3.5
    finally:
        conn.close()
