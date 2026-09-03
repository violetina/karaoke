"""Tests for YouTube mode: the pure title parser + yt-dlp resolver (mocked).

The network/yt-dlp layer is faked via a stub injected into sys.modules, so these
run offline with no yt-dlp installed.
"""
import os
import sys
import types
from pathlib import Path

import pytest

from karaoke.youtube import (
    _cookie_opts,
    _ejs_opts,
    _youtube_audio_files,
    clean_uploader,
    clear_youtube_cache,
    fetch_metadata,
    parse_youtube_title,
    prune_youtube_cache,
    resolve_youtube,
    youtube_cache_summary,
)


# --- pure title parsing ----------------------------------------------------

def test_parse_basic_artist_title():
    assert parse_youtube_title("Queen - Bohemian Rhapsody") == (
        "Queen", "Bohemian Rhapsody")


def test_parse_strips_official_video_paren():
    assert parse_youtube_title(
        "Rick Astley - Never Gonna Give You Up (Official Music Video)"
    ) == ("Rick Astley", "Never Gonna Give You Up")


def test_parse_strips_bracketed_lyrics_tag():
    assert parse_youtube_title("Adele - Hello [Official Lyric Video]") == (
        "Adele", "Hello")


def test_parse_strips_bare_trailing_official_video():
    # No brackets around the promo tokens.
    assert parse_youtube_title(
        "Coldplay - Yellow Official Video"
    ) == ("Coldplay", "Yellow")


def test_parse_strips_trailing_pipe_label():
    assert parse_youtube_title(
        "Dua Lipa - Levitating (Official Video) | Warner Records"
    ) == ("Dua Lipa", "Levitating")


def test_parse_stacked_decorations():
    assert parse_youtube_title(
        "Eminem - Lose Yourself [HD] (Official Music Video) (Explicit)"
    ) == ("Eminem", "Lose Yourself")


def test_parse_keeps_feat_and_remaster_for_lrclib():
    # clean_title (in lyrics.py) handles these on the LRCLIB retry; we must NOT
    # strip them here or we lose signal that could actually match.
    a, t = parse_youtube_title(
        "Mark Ronson - Uptown Funk (feat. Bruno Mars) (Official Video)"
    )
    assert a == "Mark Ronson"
    assert t == "Uptown Funk (feat. Bruno Mars)"


def test_parse_topic_channel_uses_uploader_as_artist():
    # Auto-generated "- Topic" channel: title is often bare, uploader is artist.
    assert parse_youtube_title("Weightless", uploader="Marconi Union - Topic") == (
        "Marconi Union", "Weightless")


def test_parse_topic_channel_prefers_explicit_split():
    assert parse_youtube_title(
        "Radiohead - Creep", uploader="Radiohead - Topic"
    ) == ("Radiohead", "Creep")


def test_parse_no_dash_falls_back_to_uploader():
    assert parse_youtube_title("Weightless", uploader="MarconiUnionVEVO") == (
        "MarconiUnion", "Weightless")


def test_parse_no_dash_no_uploader_title_only():
    assert parse_youtube_title("Some Random Song") == ("", "Some Random Song")


def test_parse_em_dash_separator():
    assert parse_youtube_title("Sigur Rós — Hoppípolla") == (
        "Sigur Rós", "Hoppípolla")


def test_parse_strips_smart_quotes_around_title():
    a, t = parse_youtube_title('Beyoncé - "Halo" (Official Video)')
    assert a == "Beyoncé"
    assert t == "Halo"


@pytest.mark.parametrize("uploader,expected", [
    ("Marconi Union - Topic", "Marconi Union"),
    ("ColdplayVEVO", "Coldplay"),
    ("Atlantic Records", "Atlantic"),
    ("Some Artist Official", "Some Artist"),
    ("PlainName", "PlainName"),
])
def test_clean_uploader(uploader, expected):
    assert clean_uploader(uploader) == expected


# --- yt-dlp wrapper (faked) -------------------------------------------------

class _FakeYDL:
    """Minimal stand-in for yt_dlp.YoutubeDL used in tests."""
    _info = {}

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        return dict(self._info)

    def prepare_filename(self, info):
        return "/tmp/does-not-exist.webm"


def _install_fake_ytdlp(monkeypatch, info):
    mod = types.ModuleType("yt_dlp")
    ydl = type("YoutubeDL", (_FakeYDL,), {"_info": info})
    mod.YoutubeDL = ydl
    monkeypatch.setitem(sys.modules, "yt_dlp", mod)


def test_fetch_metadata_uses_music_tags_when_present(monkeypatch):
    _install_fake_ytdlp(monkeypatch, {
        "track": "Bohemian Rhapsody", "artist": "Queen",
        "title": "Queen - Bohemian Rhapsody (Official Video)",
        "uploader": "Queen Official", "duration": 355,
    })
    meta = fetch_metadata("https://youtu.be/x")
    assert meta["title"] == "Bohemian Rhapsody"   # track tag preferred
    assert meta["uploader"] == "Queen"            # artist tag preferred
    assert meta["duration"] == 355.0
    assert meta["path"] is None


def test_resolve_youtube_end_to_end(monkeypatch):
    _install_fake_ytdlp(monkeypatch, {
        "title": "a-ha - Take On Me (Official Video)",
        "uploader": "a-ha", "duration": 227,
    })
    ref = resolve_youtube("https://youtu.be/djV11Xbc914")
    assert ref is not None
    assert ref.artist == "a-ha"
    assert ref.title == "Take On Me"
    assert ref.duration == 227.0
    assert ref.source == "youtube"
    assert ref.path is None


def test_resolve_youtube_none_when_no_title(monkeypatch):
    _install_fake_ytdlp(monkeypatch, {"title": "", "uploader": "", "duration": None})
    assert resolve_youtube("https://youtu.be/x") is None


def test_fetch_metadata_raises_actionable_error_without_ytdlp(monkeypatch):
    # Ensure importing yt_dlp fails.
    monkeypatch.setitem(sys.modules, "yt_dlp", None)
    with pytest.raises(RuntimeError, match="yt-dlp"):
        fetch_metadata("https://youtu.be/x")


# --- cookie / Premium auth --------------------------------------------------

def test_cookie_opts_empty_when_none(monkeypatch):
    monkeypatch.delenv("KARAOKE_COOKIES_FROM_BROWSER", raising=False)
    assert _cookie_opts(None, None) == {}


def test_cookie_opts_browser_simple():
    assert _cookie_opts("firefox", None) == {
        "cookiesfrombrowser": ("firefox", None, None, None)
    }


def test_cookie_opts_browser_with_profile():
    assert _cookie_opts("chrome:Default", None) == {
        "cookiesfrombrowser": ("chrome", "Default", None, None)
    }


def test_cookie_opts_browser_with_keyring_and_container():
    # Full BROWSER+KEYRING:PROFILE::CONTAINER spec.
    assert _cookie_opts("firefox+basictext:myprofile::Personal", None) == {
        "cookiesfrombrowser": ("firefox", "myprofile", "basictext", "Personal")
    }


def test_cookie_opts_file(monkeypatch):
    monkeypatch.delenv("KARAOKE_COOKIES_FROM_BROWSER", raising=False)
    assert _cookie_opts(None, "/home/tina/cookies.txt") == {
        "cookiefile": "/home/tina/cookies.txt"
    }


def test_cookie_opts_both_sources():
    opts = _cookie_opts("firefox", "/tmp/c.txt")
    assert opts["cookiesfrombrowser"] == ("firefox", None, None, None)
    assert opts["cookiefile"] == "/tmp/c.txt"


class _CapturingYDL(_FakeYDL):
    """FakeYDL that records the opts of the last constructed instance."""
    last_opts: dict = {}

    def __init__(self, opts):
        super().__init__(opts)
        type(self).last_opts = opts


def test_ejs_opts_default_enables_github(monkeypatch):
    monkeypatch.delenv("KARAOKE_YTDLP_REMOTE_COMPONENTS", raising=False)
    assert _ejs_opts() == {"remote_components": ["ejs:github"]}


def test_ejs_opts_disabled_when_env_empty(monkeypatch):
    monkeypatch.setenv("KARAOKE_YTDLP_REMOTE_COMPONENTS", "")
    assert _ejs_opts() == {}


def test_ejs_opts_custom_multi(monkeypatch):
    monkeypatch.setenv("KARAOKE_YTDLP_REMOTE_COMPONENTS", "ejs:github,ejs:npm")
    assert _ejs_opts() == {"remote_components": ["ejs:github", "ejs:npm"]}


def _install_capturing_ytdlp(monkeypatch, info):
    mod = types.ModuleType("yt_dlp")
    ydl = type("YoutubeDL", (_CapturingYDL,), {"_info": info, "last_opts": {}})
    mod.YoutubeDL = ydl
    monkeypatch.setitem(sys.modules, "yt_dlp", mod)
    return ydl


def test_fetch_metadata_forwards_cookies_to_ytdlp(monkeypatch):
    ydl = _install_capturing_ytdlp(monkeypatch, {
        "title": "x - y", "uploader": "x", "duration": 10,
    })
    fetch_metadata("https://youtu.be/x", cookies_from_browser="firefox",
                   cookies_file="/tmp/c.txt")
    assert ydl.last_opts["cookiesfrombrowser"] == ("firefox", None, None, None)
    assert ydl.last_opts["cookiefile"] == "/tmp/c.txt"


def test_fetch_metadata_no_cookie_keys_by_default(monkeypatch):
    monkeypatch.delenv("KARAOKE_COOKIES_FROM_BROWSER", raising=False)
    ydl = _install_capturing_ytdlp(monkeypatch, {
        "title": "x - y", "uploader": "x", "duration": 10,
    })
    fetch_metadata("https://youtu.be/x")
    assert "cookiesfrombrowser" not in ydl.last_opts
    assert "cookiefile" not in ydl.last_opts


def test_fetch_metadata_uses_cookie_env_fallback(monkeypatch):
    monkeypatch.setenv("KARAOKE_COOKIES_FROM_BROWSER", "firefox")
    ydl = _install_capturing_ytdlp(monkeypatch, {
        "title": "x - y", "uploader": "x", "duration": 10,
    })
    fetch_metadata("https://youtu.be/x")
    assert ydl.last_opts["cookiesfrombrowser"] == ("firefox", None, None, None)


# --- YouTube download-cache cleanup ----------------------------------------

def _audio(path: Path, size: int, mtime: int) -> Path:
    path.write_bytes(b"x" * size)
    os.utime(path, (mtime, mtime))
    return path


def test_youtube_audio_files_only_known_audio_exts_oldest_first(tmp_path):
    newer = _audio(tmp_path / "new.webm", 10, 30)
    older = _audio(tmp_path / "old.m4a", 10, 10)
    _audio(tmp_path / "notes.txt", 10, 1)
    assert _youtube_audio_files(tmp_path) == [older, newer]


def test_youtube_cache_summary(tmp_path):
    _audio(tmp_path / "a.webm", 1024, 1)
    _audio(tmp_path / "b.opus", 2048, 2)
    s = youtube_cache_summary(tmp_path)
    assert s.directory == tmp_path
    assert s.files == 2
    assert s.bytes == 3072


def test_prune_youtube_cache_oldest_first(tmp_path):
    old = _audio(tmp_path / "old.webm", 2 * 1024 * 1024, 1)
    mid = _audio(tmp_path / "mid.webm", 2 * 1024 * 1024, 2)
    new = _audio(tmp_path / "new.webm", 2 * 1024 * 1024, 3)

    s = prune_youtube_cache(4, directory=tmp_path)
    assert s.removed_files == 1
    assert s.removed_bytes == old.stat().st_size if old.exists() else 2 * 1024 * 1024
    assert not old.exists()
    assert mid.exists()
    assert new.exists()
    assert s.files == 2
    assert s.bytes == 4 * 1024 * 1024


def test_prune_youtube_cache_keeps_current_file(tmp_path):
    old = _audio(tmp_path / "old.webm", 2 * 1024 * 1024, 1)
    current = _audio(tmp_path / "current.webm", 4 * 1024 * 1024, 2)

    s = prune_youtube_cache(1, keep=current, directory=tmp_path)
    assert not old.exists()
    assert current.exists()
    assert s.files == 1
    # Above the requested cap, but the protected current download was preserved.
    assert s.bytes == 4 * 1024 * 1024


def test_prune_youtube_cache_zero_disables(tmp_path):
    f = _audio(tmp_path / "a.webm", 1024, 1)
    s = prune_youtube_cache(0, directory=tmp_path)
    assert f.exists()
    assert s.removed_files == 0
    assert s.files == 1


def test_clear_youtube_cache(tmp_path):
    _audio(tmp_path / "a.webm", 1024, 1)
    _audio(tmp_path / "b.m4a", 2048, 2)
    _audio(tmp_path / "keep.txt", 4096, 3)
    s = clear_youtube_cache(tmp_path)
    assert s.removed_files == 2
    assert s.removed_bytes == 3072
    assert not (tmp_path / "a.webm").exists()
    assert not (tmp_path / "b.m4a").exists()
    assert (tmp_path / "keep.txt").exists()


def test_fetch_metadata_prunes_after_download(monkeypatch, tmp_path):
    downloaded = _audio(tmp_path / "downloaded.webm", 1024, 10)

    class _DownloadingYDL(_FakeYDL):
        _info = {"title": "x - y", "uploader": "x", "duration": 1}

        def prepare_filename(self, info):
            return str(downloaded)

    mod = types.ModuleType("yt_dlp")
    mod.YoutubeDL = _DownloadingYDL
    monkeypatch.setitem(sys.modules, "yt_dlp", mod)

    # Put old files in the real temp cache dir via monkeypatching the module's
    # settings object (Settings itself is a frozen dataclass).
    import karaoke.youtube as ytm
    monkeypatch.setattr(
        ytm, "settings",
        types.SimpleNamespace(youtube_dir=tmp_path, yt_cache_max_mb=500),
    )
    old = _audio(tmp_path / "old.webm", 2 * 1024 * 1024, 1)

    meta = fetch_metadata("URL", download=True, cache_max_mb=1)
    assert meta["path"] == str(downloaded)
    assert downloaded.exists()
    assert not old.exists()
