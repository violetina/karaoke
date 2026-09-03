"""Tests for the expanded Textual TUI helper functions."""

from karaoke.tui import (
    _default_sync_offset,
    caption_is_synced,
    first_nonempty_line,
    lyric_preview,
    mood_for_preview,
)


def test_lyric_preview_prefers_synced_lyrics():
    song = {
        "synced_lyrics": "[00:01.00] I love this\n[00:02.00] ignored later",
        "plain_lyrics": "plain fallback",
    }

    assert lyric_preview(song, max_lines=1) == "[00:01.00] I love this"


def test_lyric_preview_falls_back_to_plain_lyrics():
    song = {"synced_lyrics": "", "plain_lyrics": "\nhello\nworld\n"}

    assert lyric_preview(song) == "hello\nworld"


def test_mood_for_preview_uses_first_non_neutral_line():
    assert mood_for_preview("la la la\nI love you") == "tender"


def test_first_nonempty_line():
    assert first_nonempty_line("\n  \n  seed text  \nnext") == "seed text"


def test_caption_is_synced_gates_autoload():
    assert caption_is_synced("youtube_caption_manual_en-US_enhanced")
    assert caption_is_synced("youtube_caption_automatic_en_synced")
    assert not caption_is_synced("youtube_caption_manual_en_plain")
    assert not caption_is_synced("")


def test_default_sync_offset_from_env(monkeypatch):
    monkeypatch.setenv("KARAOKE_SYNC_OFFSET", "0.7")
    assert _default_sync_offset() == 0.7


def test_default_sync_offset_fallback_on_bad_value(monkeypatch):
    monkeypatch.setenv("KARAOKE_SYNC_OFFSET", "not-a-number")
    assert _default_sync_offset() == 1.3


def test_default_sync_offset_default(monkeypatch):
    monkeypatch.delenv("KARAOKE_SYNC_OFFSET", raising=False)
    assert _default_sync_offset() == 1.3
