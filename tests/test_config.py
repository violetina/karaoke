"""Tests for environment/configuration loading."""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path


def test_dotenv_loader_survives_shallow_package_layout(tmp_path):
    """config must import when the package is not 3 levels below a root.

    Regression: `_load_dotenv` indexed `Path(__file__).parents[3]`
    unconditionally, which raised IndexError inside the container image where
    the package lives at /app/karaoke rather than <repo>/src/karaoke.
    """
    src = Path(__file__).resolve().parents[1] / "src" / "karaoke"
    shallow = tmp_path / "karaoke"
    shallow.mkdir()
    for name in ("__init__.py", "config.py"):
        (shallow / name).write_text((src / name).read_text(), encoding="utf-8")

    script = textwrap.dedent(
        """
        from karaoke.config import settings
        print("OK", settings.index_name)
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_data_dir_is_env_overridable(tmp_path, monkeypatch):
    """KARAOKE_DATA_DIR is how the container points at its mounted volume."""
    monkeypatch.setenv("KARAOKE_DATA_DIR", str(tmp_path / "data"))
    from karaoke.config import Settings

    settings = Settings.load()
    assert settings.data_dir == tmp_path / "data"
    assert settings.local_db == tmp_path / "data" / "karaoke.db"
