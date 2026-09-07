import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path
from karaoke.identify import identify_file_fingerprint

def test_identify_file_fingerprint_success(tmp_path):
    dummy_audio = tmp_path / "test.mp3"
    dummy_audio.write_bytes(b"dummy")

    mock_json = '{"track": {"subtitle": "Portishead", "title": "Glory Box", "sections": [{"metadata": [{"title": "Album", "text": "Dummy"}]}]}, "matches": [{"offset": 12.0}]}'
    with patch("shutil.which", return_value="/usr/bin/songrec"), \
         patch("subprocess.run") as mock_run:
        mock_run.return_value.stdout = mock_json
        mock_run.return_value.returncode = 0

        res = identify_file_fingerprint(dummy_audio)
        assert res is not None
        assert res.artist == "Portishead"
        assert res.title == "Glory Box"
        assert res.album == "Dummy"
        assert res.source == "fingerprint"
        assert res.offset == 12.0

def test_identify_file_fingerprint_missing_file(tmp_path):
    missing = tmp_path / "nonexistent.mp3"
    assert identify_file_fingerprint(missing) is None

def test_identify_file_fingerprint_no_songrec():
    with patch("shutil.which", return_value=None):
        assert identify_file_fingerprint("/tmp/test.mp3") is None
