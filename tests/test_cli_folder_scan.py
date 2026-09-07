import pytest
from karaoke.cli import folder_scan_main


def test_cli_folder_scan_help():
    with pytest.raises(SystemExit) as exc:
        folder_scan_main(["--help"])
    assert exc.value.code == 0
