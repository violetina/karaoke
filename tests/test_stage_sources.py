"""Tests for lower-trust lyrics source collectors."""

from karaoke.stage_sources import _caption_tracks, _vtt_to_plain


def test_vtt_to_plain_strips_timestamps_tags_and_duplicates():
    text = """WEBVTT

00:00:01.000 --> 00:00:03.000 align:start
<c>Come closer</c>
Come closer

00:00:03.000 --> 00:00:05.000
and see
"""
    assert _vtt_to_plain(text) == "Come closer\nand see"


def test_caption_tracks_prefers_manual_before_automatic_language_order():
    info = {
        "subtitles": {
            "nl": [{"ext": "vtt", "url": "manual-nl"}],
        },
        "automatic_captions": {
            "en": [{"ext": "vtt", "url": "auto-en"}],
        },
    }
    tracks = _caption_tracks(info, ["en", "nl"])
    assert tracks[0]["url"] == "manual-nl"
    assert tracks[0]["kind"] == "manual"
    assert tracks[1]["url"] == "auto-en"
    assert tracks[1]["kind"] == "automatic"
