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


# --- gap recording ---------------------------------------------------------

def test_record_gap_skips_when_player_gives_no_artist(tmp_path):
    """YouTube Music reports a title with no artist; such a gap is unfillable.

    Chromium exposes no xesam:url either, so nothing could ever resolve the
    artist later — the row would be retried forever against LRCLIB.
    """
    from karaoke import detect, localcache

    conn = localcache.connect(tmp_path / "karaoke.db")
    detect.record_gap(
        detect.Detection(mode="scan", player="chromium", artist="", title="September"),
        conn,
    )
    rows = conn.execute("SELECT artist, title FROM lyric_gaps").fetchall()
    assert rows == []


def test_record_gap_logs_when_artist_is_present(tmp_path):
    from karaoke import detect, localcache

    conn = localcache.connect(tmp_path / "karaoke.db")
    detect.record_gap(
        detect.Detection(mode="scan", player="chromium",
                         artist="Earth, Wind & Fire", title="September"),
        conn,
    )
    rows = conn.execute("SELECT artist, title FROM lyric_gaps").fetchall()
    assert [(r["artist"], r["title"]) for r in rows] == [("Earth, Wind & Fire", "September")]
