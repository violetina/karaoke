"""Detecting tracks whose title is only a remaster qualifier split off a song."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "fix_version_only_titles",
    Path(__file__).resolve().parent.parent / "scripts" / "fix_version_only_titles.py")
fix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fix)


def test_split_bad_row():
    assert fix.split_bad_row("Porcelina Of The Vast Oceans", "Remastered 2012") == (
        "Porcelina Of The Vast Oceans", "Remastered 2012")
    assert fix.split_bad_row("Embraceable You (2002", "Remaster)") == (
        "Embraceable You", "2002 Remaster")
    assert fix.split_bad_row("Bridge of Sighs", "2007 Remaster") == (
        "Bridge of Sighs", "2007 Remaster")


def test_real_titles_are_left_alone():
    assert fix.split_bad_row("Fugazi", "Version") is None
    assert fix.split_bad_row("Queen", "Bohemian Rhapsody (2011 Remaster)") is None
    assert fix.split_bad_row("Pink Floyd", "Money") is None
