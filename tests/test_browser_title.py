"""Tests for browser MPRIS title cleanup and track normalization."""

from karaoke.playerctl import clean_browser_title, normalize_player_track


def test_strip_youtube_suffix():
    assert clean_browser_title("Come - YouTube") == "Come"
    assert clean_browser_title("Song - YouTube Music") == "Song"


def test_strip_video_descriptors():
    assert clean_browser_title("Come (Official Video)") == "Come"
    assert clean_browser_title("Come [Official Music Video]") == "Come"
    assert clean_browser_title("Come (Lyric Video)") == "Come"
    assert clean_browser_title("Come (Official Audio)") == "Come"
    assert clean_browser_title("Come (HD)") == "Come"


def test_real_world_chromium_title():
    # The actual title observed from a live chromium YouTube tab.
    assert clean_browser_title("Jain - Come (Official Video) - YouTube") == "Jain - Come"


def test_stacked_descriptors_collapse():
    assert clean_browser_title(
        "Song (Official Music Video) [HD] - YouTube"
    ) == "Song"


def test_trailing_bullet_fragment_removed():
    assert clean_browser_title("Artist - Song • 1.2M views") == "Artist - Song"


def test_plain_title_unchanged():
    assert clean_browser_title("Just A Normal Title") == "Just A Normal Title"


def test_never_returns_empty():
    # Over-eager stripping must fall back to the original.
    assert clean_browser_title("(Official Video)") == "(Official Video)"


def test_normalize_uses_browser_cleanup():
    ref = normalize_player_track(
        "", "Jain - Come (Official Video) - YouTube",
        url="https://youtube.com/watch?v=x",
    )
    assert ref.artist == "Jain"
    assert ref.title == "Come"


def test_normalize_strips_duplicated_artist_after_cleanup():
    ref = normalize_player_track(
        "Jain", "Jain - Come (Official Video) - YouTube",
    )
    assert ref.artist == "Jain"
    assert ref.title == "Come"


def test_normalize_endash_separator():
    ref = normalize_player_track("", "Jain – Come - YouTube")
    assert ref.artist == "Jain"
    assert ref.title == "Come"
