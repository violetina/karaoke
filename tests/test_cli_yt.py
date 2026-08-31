"""Tests for the `karaoke-yt` CLI front-end (argv translation only).

These verify karaoke_yt_main forwards its friendlier options to karaoke_main as
the right `--youtube ...` argv, without touching the network (karaoke_main is
patched to capture argv).
"""
from types import SimpleNamespace

import karaoke.cli as cli


def _capture(monkeypatch):
    seen = {}

    def fake_main(argv=None):
        seen["argv"] = argv
        return 0

    monkeypatch.setattr(cli, "karaoke_main", fake_main)
    return seen


def test_yt_minimal_forwards_url(monkeypatch):
    seen = _capture(monkeypatch)
    rc = cli.karaoke_yt_main(["https://youtu.be/abc"])
    assert rc == 0
    assert seen["argv"] == ["--youtube", "https://youtu.be/abc"]


def test_yt_download_flag(monkeypatch):
    seen = _capture(monkeypatch)
    cli.karaoke_yt_main(["URL", "--download"])
    assert seen["argv"] == ["--youtube", "URL", "--download"]


def test_yt_transcribe_implies_download(monkeypatch):
    seen = _capture(monkeypatch)
    cli.karaoke_yt_main(["URL", "--transcribe"])
    # --transcribe needs local audio, so --download is added automatically.
    assert seen["argv"] == ["--youtube", "URL", "--download", "--transcribe"]


def test_yt_print_and_no_cache(monkeypatch):
    seen = _capture(monkeypatch)
    cli.karaoke_yt_main(["URL", "--print", "--no-cache"])
    assert seen["argv"] == ["--youtube", "URL", "--print", "--no-cache"]


def test_yt_offset_forwarded(monkeypatch):
    seen = _capture(monkeypatch)
    cli.karaoke_yt_main(["URL", "--offset", "1.5"])
    assert seen["argv"] == ["--youtube", "URL", "--offset", "1.5"]


def test_yt_no_beats_forwarded(monkeypatch):
    seen = _capture(monkeypatch)
    cli.karaoke_yt_main(["URL", "-d", "--no-beats"])
    assert seen["argv"] == ["--youtube", "URL", "--download", "--no-beats"]


def test_yt_all_options_combined(monkeypatch):
    seen = _capture(monkeypatch)
    cli.karaoke_yt_main(["URL", "--transcribe", "--print", "--no-cache",
                         "--no-beats", "--offset", "2.0"])
    assert seen["argv"] == [
        "--youtube", "URL", "--download", "--transcribe", "--print",
        "--no-cache", "--no-beats", "--offset", "2.0",
    ]


def test_yt_cookies_from_browser_forwarded(monkeypatch):
    seen = _capture(monkeypatch)
    cli.karaoke_yt_main(["URL", "--cookies-from-browser", "firefox"])
    assert seen["argv"] == ["--youtube", "URL",
                            "--cookies-from-browser", "firefox"]


def test_yt_cookies_file_forwarded(monkeypatch):
    seen = _capture(monkeypatch)
    cli.karaoke_yt_main(["URL", "--cookies", "/tmp/c.txt"])
    assert seen["argv"] == ["--youtube", "URL", "--cookies", "/tmp/c.txt"]


def test_yt_cache_max_forwarded(monkeypatch):
    seen = _capture(monkeypatch)
    cli.karaoke_yt_main(["URL", "--cache-max-mb", "50"])
    assert seen["argv"] == ["--youtube", "URL", "--yt-cache-max-mb", "50"]


def test_yt_cache_status_does_not_need_url(monkeypatch, capsys):
    import karaoke.youtube as ytm

    monkeypatch.setattr(ytm, "youtube_cache_summary", lambda: SimpleNamespace(
        files=3, mib=12.25, directory="/tmp/yt",
    ))
    rc = cli.karaoke_yt_main(["--cache-status"])
    assert rc == 0
    assert "3 files, 12.2 MiB" in capsys.readouterr().out


def test_yt_clear_cache_does_not_need_url(monkeypatch, capsys):
    import karaoke.youtube as ytm

    monkeypatch.setattr(ytm, "clear_youtube_cache", lambda: SimpleNamespace(
        removed_files=2, removed_mib=7.5, directory="/tmp/yt",
    ))
    rc = cli.karaoke_yt_main(["--clear-cache"])
    assert rc == 0
    assert "removed 2 files (7.5 MiB)" in capsys.readouterr().out


def test_yt_prune_cache_does_not_need_url(monkeypatch, capsys):
    import karaoke.youtube as ytm

    monkeypatch.setattr(ytm, "prune_youtube_cache", lambda mb: SimpleNamespace(
        removed_files=4, removed_mib=20.0, files=1, mib=3.0, directory="/tmp/yt",
    ))
    rc = cli.karaoke_yt_main(["--prune-cache", "5"])
    assert rc == 0
    assert "Pruned YouTube cache to <= 5 MiB" in capsys.readouterr().out
