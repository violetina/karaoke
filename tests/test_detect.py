"""Tests for active-player detection and mode selection."""

from karaoke.detect import Detection, classify, is_youtube_url
from karaoke.playerctl import PlayerMetadata


def test_no_metadata_is_browse():
    det = classify(None)
    assert det.mode == "browse"
    assert not det.is_active


def test_spotify_player_is_spotify_mode():
    meta = PlayerMetadata(artist="Ren", title="Hi Ren", player="spotify")
    det = classify(meta)
    assert det.mode == "spotify"
    assert det.is_active
    assert det.title == "Hi Ren"


def test_browser_youtube_is_scan_mode():
    meta = PlayerMetadata(
        artist="",
        title="Ren - The Tale of Jenny & Screech",
        url="https://www.youtube.com/watch?v=abc",
        player="firefox.instance123",
    )
    det = classify(meta)
    assert det.mode == "scan"
    assert det.is_active
    assert det.url.endswith("v=abc")


def test_empty_track_is_browse():
    meta = PlayerMetadata(artist="", title="", url="", player="firefox")
    assert classify(meta).mode == "browse"


def test_is_youtube_url():
    assert is_youtube_url("https://music.youtube.com/watch?v=x")
    assert is_youtube_url("https://youtu.be/x")
    assert not is_youtube_url("https://open.spotify.com/track/x")


def test_detection_is_active_flag():
    assert Detection("scan").is_active
    assert Detection("spotify").is_active
    assert not Detection("browse").is_active
