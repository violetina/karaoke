"""Tests for the `karaoke-yt` CLI front-end (argv translation only).

These verify karaoke_yt_main forwards its friendlier options to karaoke_main as
the right `--youtube ...` argv, without touching the network (karaoke_main is
patched to capture argv).
"""
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
