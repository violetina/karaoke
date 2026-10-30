"""Tests for Audio vs Video synchronization, LRC shifting, and dual-version lyrics storage."""
import pytest
from types import SimpleNamespace

from karaoke import av_align, detect, localcache
from karaoke.caption_sync import (
    _durations_compatible,
    _try_waveform_offset,
    _try_whisper_offset,
    sync_audio_video_counterparts,
)
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


def _make_av_track(conn, artist, title, audio_offset_s=0.0, video_offset_s=3.5):
    cur = conn.cursor()
    cur.execute("INSERT INTO tracks (artist, title) VALUES (%s, %s) RETURNING track_id", (artist, title))
    track_id = cur.fetchone()["track_id"]
    audio_lrc = "[00:10.00] Line one\n[00:20.00] Line two"
    localcache.put_cached_lyrics(
        artist, title,
        Lyrics(plain="Line one\nLine two", synced_raw=audio_lrc, source="lrclib"),
        media_type="audio", conn=conn,
    )
    v_one = 10.0 + video_offset_s
    v_two = 20.0 + video_offset_s
    video_lrc = f"[{int(v_one)//60:02d}:{v_one%60:05.2f}] Line one\n[{int(v_two)//60:02d}:{v_two%60:05.2f}] Line two"
    localcache.put_cached_lyrics(
        artist, title,
        Lyrics(plain="Line one\nLine two", synced_raw=video_lrc, source="youtube_caption"),
        media_type="video", conn=conn,
    )
    return track_id


def test_try_waveform_offset_returns_offset_on_confident_match(monkeypatch):
    fake_client = SimpleNamespace(
        get_audio_and_video_versions=lambda a, t: {"audio_video_id": "AID", "video_video_id": "VID"}
    )
    fake_result = av_align.AlignmentResult(offset_s=9.0, slope=0.0, confidence=0.9, kind="constant")
    monkeypatch.setattr("karaoke.postprocess_worker.run_download_logic", lambda url, cb: "/fake/path")
    monkeypatch.setattr(av_align, "load_mono", lambda path, sr: "fake-signal")
    monkeypatch.setattr(av_align, "estimate_offset", lambda a, v, sr: fake_result)

    info = _try_waveform_offset({"artist": "A", "title": "T", "track_id": 1}, fake_client)

    assert info["offset"] == 9.0
    assert info["confidence"] == 0.9
    assert info["kind"] == "constant"
    assert info["audio_id"] == "AID" and info["video_id"] == "VID"


def test_try_waveform_offset_returns_ids_only_when_low_confidence(monkeypatch):
    fake_client = SimpleNamespace(
        get_audio_and_video_versions=lambda a, t: {"audio_video_id": "AID", "video_video_id": "VID"}
    )
    fake_result = av_align.AlignmentResult(offset_s=0.0, slope=0.0, confidence=0.01, kind="low_confidence")
    monkeypatch.setattr("karaoke.postprocess_worker.run_download_logic", lambda url, cb: "/fake/path")
    monkeypatch.setattr(av_align, "load_mono", lambda path, sr: "fake-signal")
    monkeypatch.setattr(av_align, "estimate_offset", lambda a, v, sr: fake_result)

    info = _try_waveform_offset({"artist": "A", "title": "T", "track_id": 1}, fake_client)

    assert "offset" not in info
    assert info["video_id"] == "VID"


def test_try_waveform_offset_none_when_no_video_id():
    fake_client = SimpleNamespace(
        get_audio_and_video_versions=lambda a, t: {"audio_video_id": "AID", "video_video_id": None}
    )
    assert _try_waveform_offset({"artist": "A", "title": "T", "track_id": 1}, fake_client) is None


def test_try_waveform_offset_ids_only_when_not_distinct():
    fake_client = SimpleNamespace(
        get_audio_and_video_versions=lambda a, t: {"audio_video_id": "SAME", "video_video_id": "SAME"}
    )
    info = _try_waveform_offset({"artist": "A", "title": "T", "track_id": 1}, fake_client)
    assert info == {"audio_id": "SAME", "video_id": "SAME"}


def test_try_waveform_offset_none_on_lookup_exception():
    def boom(a, t):
        raise RuntimeError("network down")
    fake_client = SimpleNamespace(get_audio_and_video_versions=boom)
    assert _try_waveform_offset({"artist": "A", "title": "T", "track_id": 1}, fake_client) is None


def test_sync_prefers_waveform_over_captions_when_both_available(tmp_path, monkeypatch):
    """Both lyrics exist (captions would give offset 3.5), but a confident
    waveform match must win per PLAN-av-sync.md's order of preference."""
    conn = localcache.connect(tmp_path / "waveform_wins.db")
    try:
        track_id = _make_av_track(conn, "WaveBand", "WaveTrack", video_offset_s=3.5)

        fake_client = SimpleNamespace(
            get_audio_and_video_versions=lambda a, t: {"audio_video_id": "AID", "video_video_id": "VID"}
        )
        fake_result = av_align.AlignmentResult(offset_s=9.0, slope=0.0, confidence=0.9, kind="constant")
        monkeypatch.setattr("karaoke.postprocess_worker.run_download_logic", lambda url, cb: "/fake/path")
        monkeypatch.setattr(av_align, "load_mono", lambda path, sr: "fake-signal")
        monkeypatch.setattr(av_align, "estimate_offset", lambda a, v, sr: fake_result)

        res = sync_audio_video_counterparts(track_id, conn, client=fake_client)

        assert res["offset"] == 9.0
        assert res["method"] == "waveform"
        assert res["confidence"] == 0.9
        assert localcache.get_sync_offset(track_id, conn, mode="video") == 9.0
    finally:
        conn.close()


def test_sync_waveform_offset_not_relabeled_as_cached_when_video_lyrics_missing(tmp_path, monkeypatch):
    """Regression: when a track has only audio lyrics (the common case --
    not real_video_lyrics), the branch that derives video lyrics from a
    stored offset must not clobber a freshly-computed waveform result's
    method attribution. Found live via preprocessing the AV Sync Candidates
    playlist against production Postgres: track_sync_offsets.offset_s is a
    REAL (float4) column, so round-tripping a freshly-set float64 offset
    through get_sync_offset can read back a slightly different value,
    silently flipping `offset == waveform_info["offset"]` to False and
    mislabeling a correct, fresh waveform result as "cached". Simulated here
    by making a re-read return an obviously wrong number: if the code looks
    up an offset again instead of reusing what it just computed, this test
    catches it immediately rather than relying on float imprecision."""
    conn = localcache.connect(tmp_path / "waveform_not_clobbered.db")
    try:
        track_id = _make_audio_only_track(conn, "FreshWaveBand", "FreshWaveTrack")
        fake_client = SimpleNamespace(
            get_audio_and_video_versions=lambda a, t: {"audio_video_id": "AID", "video_video_id": "VID"}
        )
        fake_result = av_align.AlignmentResult(offset_s=12.5, slope=0.0, confidence=0.7, kind="constant")
        monkeypatch.setattr("karaoke.postprocess_worker.run_download_logic", lambda url, cb: "/fake/path")
        monkeypatch.setattr(av_align, "load_mono", lambda path, sr: "fake-signal")
        monkeypatch.setattr(av_align, "estimate_offset", lambda a, v, sr: fake_result)
        monkeypatch.setattr("karaoke.caption_sync._durations_compatible", lambda a, v, t=None: True)
        call_count = {"n": 0}
        def wrong_on_second_read(tid, c, mode=None):
            # First call is the function's own "already known?" pre-check
            # (must see nothing yet, or it skips the waveform attempt
            # entirely); any call after that is the bug under test re-reading
            # instead of reusing what was just computed.
            call_count["n"] += 1
            if tid == track_id and call_count["n"] > 1:
                return 999.0  # proves the code took this path instead of reusing the in-memory offset
            return None
        monkeypatch.setattr(localcache, "get_sync_offset", wrong_on_second_read)

        res = sync_audio_video_counterparts(track_id, conn, client=fake_client)

        assert res["offset"] == 12.5
        assert res["method"] == "waveform"
        assert res["confidence"] == 0.7
        assert res["has_video_lyrics"] is True  # still derived+stored for display
    finally:
        conn.close()


def test_sync_falls_back_to_captions_without_distinct_ids(tmp_path, monkeypatch):
    conn = localcache.connect(tmp_path / "waveform_fallback.db")
    try:
        track_id = _make_av_track(conn, "CapBand", "CapTrack", video_offset_s=3.5)

        fake_client = SimpleNamespace(
            get_audio_and_video_versions=lambda a, t: {"audio_video_id": "ID1", "video_video_id": "ID1"}
        )
        res = sync_audio_video_counterparts(track_id, conn, client=fake_client)

        assert res["offset"] == 3.5
        assert res["method"] == "captions"
        assert res["confidence"] is None
    finally:
        conn.close()


def test_sync_skips_waveform_download_when_offset_already_known(tmp_path, monkeypatch):
    """Avoid the two extra downloads on a track that's already synced --
    sync_audio_video_counterparts runs more than once per track in the
    existing postprocess chain. The cheap video-id lookup (for finding
    captions, a separate concern) may still legitimately run."""
    conn = localcache.connect(tmp_path / "waveform_skip.db")
    try:
        track_id = _make_av_track(conn, "KnownBand", "KnownTrack", video_offset_s=3.5)
        localcache.set_sync_offset(track_id, 3.5, conn, mode="video")

        download_calls = []
        monkeypatch.setattr("karaoke.postprocess_worker.run_download_logic",
                            lambda url, cb: download_calls.append(url) or "/fake/path")

        fake_client = SimpleNamespace(
            get_audio_and_video_versions=lambda a, t: {"audio_video_id": "AID", "video_video_id": "VID"}
        )
        res = sync_audio_video_counterparts(track_id, conn, client=fake_client)

        assert download_calls == [], "should not download audio for the waveform method when an offset is already known"
        assert res["offset"] == 3.5
    finally:
        conn.close()


def _make_audio_only_track(conn, artist, title):
    """Like _make_av_track but with no video lyrics row at all -- deleting the
    video row after _make_av_track's insert doesn't work for this purpose:
    get_lyrics_by_track_id's media_type='video' lookup falls back to ANY
    approved lyrics row when none is specifically typed 'video' (documented,
    intentional behavior for untyped single-lyrics tracks), so it would just
    hand back the audio row relabeled as 'video'."""
    cur = conn.cursor()
    cur.execute("INSERT INTO tracks (artist, title) VALUES (%s, %s) RETURNING track_id", (artist, title))
    track_id = cur.fetchone()["track_id"]
    audio_lrc = "[00:10.00] Line one\n[00:20.00] Line two"
    localcache.put_cached_lyrics(
        artist, title,
        Lyrics(plain="Line one\nLine two", synced_raw=audio_lrc, source="lrclib"),
        media_type="audio", conn=conn,
    )
    return track_id


def test_try_whisper_offset_returns_aligned_offset(monkeypatch):
    audio_lrc = "[00:10.00] Line one\n[00:20.00] Line two"
    video_lrc = "[00:17.00] Line one\n[00:27.00] Line two"  # +7s vs audio
    monkeypatch.setattr("karaoke.postprocess_worker.run_download_logic", lambda url, cb: "/fake/video.webm")
    monkeypatch.setattr("karaoke.whisper_sync.transcribe_to_lrc", lambda path, **kw: video_lrc)

    offset = _try_whisper_offset({"track_id": 1}, audio_lrc, "https://www.youtube.com/watch?v=VID")

    assert offset == 7.0


def test_try_whisper_offset_none_when_download_fails(monkeypatch):
    monkeypatch.setattr("karaoke.postprocess_worker.run_download_logic", lambda url, cb: None)
    offset = _try_whisper_offset({"track_id": 1}, "[00:10.00] x", "https://y/v")
    assert offset is None


def test_try_whisper_offset_none_on_empty_transcript(monkeypatch):
    monkeypatch.setattr("karaoke.postprocess_worker.run_download_logic", lambda url, cb: "/fake/video.webm")
    monkeypatch.setattr("karaoke.whisper_sync.transcribe_to_lrc", lambda path, **kw: "   ")
    offset = _try_whisper_offset({"track_id": 1}, "[00:10.00] x", "https://y/v")
    assert offset is None


def test_try_whisper_offset_none_on_transcription_exception(monkeypatch):
    def boom(path, **kw):
        raise RuntimeError("model failed")
    monkeypatch.setattr("karaoke.postprocess_worker.run_download_logic", lambda url, cb: "/fake/video.webm")
    monkeypatch.setattr("karaoke.whisper_sync.transcribe_to_lrc", boom)
    offset = _try_whisper_offset({"track_id": 1}, "[00:10.00] x", "https://y/v")
    assert offset is None


def test_try_whisper_offset_uses_fixed_temperature(monkeypatch):
    seen_kwargs = {}

    def fake_transcribe(path, **kw):
        seen_kwargs.update(kw)
        return "[00:10.00] x"

    monkeypatch.setattr("karaoke.postprocess_worker.run_download_logic", lambda url, cb: "/fake/video.webm")
    monkeypatch.setattr("karaoke.whisper_sync.transcribe_to_lrc", fake_transcribe)
    _try_whisper_offset({"track_id": 1}, "[00:10.00] x", "https://y/v")

    assert seen_kwargs.get("temperature") == 0.0


def test_sync_whisper_fallback_disabled_by_default(tmp_path, monkeypatch):
    """Whisper must not run unless the caller explicitly opts in -- almost no
    tracks have real video captions, so leaving it on by default would start
    full-track transcription for nearly the whole library."""
    conn = localcache.connect(tmp_path / "whisper_off.db")
    try:
        track_id = _make_audio_only_track(conn, "NoWhisperBand", "NoWhisperTrack")

        whisper_calls = []
        monkeypatch.setattr(
            "karaoke.caption_sync._try_whisper_offset",
            lambda *a, **kw: whisper_calls.append(1) or 9.0,
        )
        fake_client = SimpleNamespace(
            get_audio_and_video_versions=lambda a, t: {"audio_video_id": "SAME", "video_video_id": "SAME"}
        )
        res = sync_audio_video_counterparts(track_id, conn, client=fake_client)

        assert whisper_calls == []
        assert res["offset"] is None
        assert res["method"] is None
    finally:
        conn.close()


def test_sync_whisper_fallback_used_as_last_resort_when_enabled(tmp_path, monkeypatch):
    conn = localcache.connect(tmp_path / "whisper_on.db")
    try:
        track_id = _make_audio_only_track(conn, "WhisperBand", "WhisperTrack")

        fake_client = SimpleNamespace(
            get_audio_and_video_versions=lambda a, t: {"audio_video_id": "SAME", "video_video_id": "SAME"}
        )
        monkeypatch.setattr("karaoke.caption_sync._try_whisper_offset", lambda track, lrc, url: 11.0)

        res = sync_audio_video_counterparts(track_id, conn, client=fake_client, enable_whisper_fallback=True)

        assert res["offset"] == 11.0
        assert res["method"] == "whisper"
        assert localcache.get_sync_offset(track_id, conn, mode="video") == 11.0
    finally:
        conn.close()


def test_durations_compatible_true_for_normal_difference(monkeypatch):
    monkeypatch.setattr(
        "karaoke.youtube.fetch_metadata",
        lambda url, **kw: {"duration": 300.0 if "AID" in url else 320.0},
    )
    assert _durations_compatible("AID", "VID") is True


def test_durations_compatible_false_when_video_much_longer(monkeypatch):
    """Mirrors the real Thriller case: a 13-minute short-film video against
    a 6-minute audio release -- no offset can fix a different recording."""
    monkeypatch.setattr(
        "karaoke.youtube.fetch_metadata",
        lambda url, **kw: {"duration": 358.0 if "AID" in url else 822.0},
    )
    assert _durations_compatible("AID", "VID") is False


def test_durations_compatible_false_when_video_much_shorter(monkeypatch):
    monkeypatch.setattr(
        "karaoke.youtube.fetch_metadata",
        lambda url, **kw: {"duration": 300.0 if "AID" in url else 60.0},
    )
    assert _durations_compatible("AID", "VID") is False


def test_durations_compatible_true_for_a_long_intro_within_bounds():
    """A long-but-plausible intro (close to av_align's own search bound)
    must not be rejected before the real correlation even gets to try."""
    from karaoke import av_align

    with_patch = {"audio": 300.0, "video": 300.0 + av_align.COARSE_SEARCH_S - 1}

    def fake_fetch(url, **kw):
        return {"duration": with_patch["audio"] if "AID" in url else with_patch["video"]}

    import karaoke.youtube as youtube_mod
    orig = youtube_mod.fetch_metadata
    youtube_mod.fetch_metadata = fake_fetch
    try:
        assert _durations_compatible("AID", "VID") is True
    finally:
        youtube_mod.fetch_metadata = orig


def test_durations_compatible_true_when_metadata_unavailable(monkeypatch):
    monkeypatch.setattr("karaoke.youtube.fetch_metadata", lambda url, **kw: {"duration": None})
    assert _durations_compatible("AID", "VID") is True


def test_durations_compatible_true_on_metadata_error(monkeypatch):
    def boom(url, **kw):
        raise RuntimeError("network error")
    monkeypatch.setattr("karaoke.youtube.fetch_metadata", boom)
    assert _durations_compatible("AID", "VID") is True


def test_try_waveform_offset_skips_download_on_duration_mismatch(monkeypatch):
    """The duration guard must run BEFORE any download -- that's the whole
    point (not burning effort on a video that's clearly a different
    recording)."""
    fake_client = SimpleNamespace(
        get_audio_and_video_versions=lambda a, t: {"audio_video_id": "AID", "video_video_id": "VID"}
    )
    monkeypatch.setattr("karaoke.caption_sync._durations_compatible", lambda a, v, t=None: False)
    download_calls = []
    monkeypatch.setattr("karaoke.postprocess_worker.run_download_logic",
                        lambda url, cb: download_calls.append(url) or "/fake/path")

    info = _try_waveform_offset({"artist": "A", "title": "T", "track_id": 1}, fake_client)

    assert download_calls == []
    assert info == {"audio_id": "AID", "video_id": "VID", "skipped_reason": "duration_mismatch"}
