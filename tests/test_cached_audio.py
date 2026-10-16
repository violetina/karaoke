"""A cached audio file is reused only when it reads as audio."""
import shutil
import subprocess

import pytest

from karaoke import postprocess_worker as pw


@pytest.fixture()
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(pw, "_get_cached_audio_path",
                        lambda url: next(iter(sorted(tmp_path.glob("vid.*"))), None))
    return tmp_path


URL = "https://www.youtube.com/watch?v=ImS6Yw0O_FA"


def test_a_good_cached_file_is_reused_without_downloading(cache, monkeypatch):
    (cache / "vid.webm").write_bytes(b"good")
    monkeypatch.setattr(pw, "audio_file_ok", lambda p: True)
    monkeypatch.setattr("karaoke.youtube.fetch_metadata",
                        lambda *a, **k: pytest.fail("should not download"))
    assert pw.run_download_logic(URL, None) == cache / "vid.webm"


def test_a_broken_cached_file_is_deleted_and_fetched_again(cache, monkeypatch):
    broken = cache / "vid.webm"
    broken.write_bytes(b"\x00" * 10)
    fresh = cache / "vid.m4a"

    def download(url, download, cookies_from_browser):
        fresh.write_bytes(b"good")
        return {"path": str(fresh)}

    monkeypatch.setattr("karaoke.youtube.fetch_metadata", download)
    monkeypatch.setattr(pw, "audio_file_ok", lambda p: p == fresh)
    assert pw.run_download_logic(URL, None) == fresh
    assert not broken.exists()


def test_a_broken_download_is_removed_and_reported_as_failed(cache, monkeypatch):
    bad = cache / "vid.webm"

    def download(url, download, cookies_from_browser):
        bad.write_bytes(b"\x00" * 10)
        return {"path": str(bad)}

    monkeypatch.setattr("karaoke.youtube.fetch_metadata", download)
    monkeypatch.setattr(pw, "audio_file_ok", lambda p: False)
    assert pw.run_download_logic(URL, None) is None
    assert not bad.exists()


@pytest.mark.skipif(not shutil.which("ffprobe") or not shutil.which("ffmpeg"),
                    reason="needs ffmpeg/ffprobe")
def test_audio_file_ok_tells_audio_from_garbage(tmp_path):
    garbage = tmp_path / "broken.webm"
    garbage.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 300)
    assert pw.audio_file_ok(garbage) is False
    good = tmp_path / "tone.ogg"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
                    str(good)], check=True)
    assert pw.audio_file_ok(good) is True
    short = tmp_path / "blip.ogg"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    str(short)], check=True)
    assert pw.audio_file_ok(short) is False
