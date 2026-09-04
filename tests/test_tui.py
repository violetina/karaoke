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
    assert _default_sync_offset() == 0.0


def test_default_sync_offset_default(monkeypatch):
    monkeypatch.delenv("KARAOKE_SYNC_OFFSET", raising=False)
    assert _default_sync_offset() == 0.0


def test_default_sync_offset_spotify_is_zero(monkeypatch):
    # Spotify reports an accurate native position, so no browser-lag offset.
    monkeypatch.delenv("KARAOKE_SYNC_OFFSET_SPOTIFY", raising=False)
    assert _default_sync_offset("spotify") == 0.0


def test_default_sync_offset_spotify_env_override(monkeypatch):
    monkeypatch.setenv("KARAOKE_SYNC_OFFSET_SPOTIFY", "0.3")
    assert _default_sync_offset("spotify") == 0.3


def test_default_sync_offset_scan_unaffected_by_spotify_env(monkeypatch):
    monkeypatch.setenv("KARAOKE_SYNC_OFFSET_SPOTIFY", "0.3")
    monkeypatch.delenv("KARAOKE_SYNC_OFFSET", raising=False)
    assert _default_sync_offset("scan") == 0.0


def test_sync_offset_get_set_roundtrip(tmp_path):
    from karaoke import localcache
    from karaoke.lyrics import Lyrics

    c = localcache.connect(tmp_path / "karaoke.db")
    localcache.add_track_and_lyrics("A", "B", Lyrics(plain="x", source="lrclib"), conn=c)
    tid = localcache.find_track_id("A", "B", c)
    assert tid is not None

    # No offset saved yet.
    assert localcache.get_sync_offset(tid, c) is None

    localcache.set_sync_offset(tid, 1.4, c)
    assert localcache.get_sync_offset(tid, c) == 1.4

    # Upsert replaces, does not duplicate.
    localcache.set_sync_offset(tid, -0.5, c)
    assert localcache.get_sync_offset(tid, c) == -0.5
    assert c.execute("SELECT count(*) FROM track_sync_offsets").fetchone()[0] == 1


def test_sync_offset_none_track_is_safe(tmp_path):
    from karaoke import localcache

    c = localcache.connect(tmp_path / "karaoke.db")
    assert localcache.get_sync_offset(None, c) is None


def test_mood_square_glyph_rows_are_uniform_width():
    """All five squares must be the same visual width.

    "☔" and "🔥" are 2 cells while "☀ ♡ ◇" are 1, so the unpadded squares
    rendered lopsided under `content-align: center`.
    """
    from karaoke import visuals
    from karaoke.tui import MOOD_GLYPHS

    for mood, art in MOOD_GLYPHS.items():
        rows = art.splitlines()
        assert len(rows) == 3, mood
        assert {visuals.cell_width(r) for r in rows} == {2}, mood
