"""Tests for the fuzzy + duration-aware track deduplication decision logic."""
import importlib.util
from pathlib import Path

# db_cleanup lives in scripts/, not the karaoke package; load it directly.
_SPEC = importlib.util.spec_from_file_location(
    "db_cleanup",
    Path(__file__).resolve().parent.parent / "scripts" / "db_cleanup.py",
)
db_cleanup = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(db_cleanup)


def _row(track_id, artist, title, duration):
    """Minimal mapping standing in for a sqlite3.Row."""
    return {"track_id": track_id, "artist": artist, "title": title, "duration": duration}


# -- duration_relation --------------------------------------------------------

def test_duration_same_within_tolerance():
    assert db_cleanup.duration_relation(200.0, 202.0) == "same"


def test_duration_different_beyond_tolerance():
    assert db_cleanup.duration_relation(200.0, 230.0) == "different"


def test_duration_unknown_when_missing():
    assert db_cleanup.duration_relation(None, 200.0) == "unknown"
    assert db_cleanup.duration_relation(200.0, None) == "unknown"


# -- are_titles_similar -------------------------------------------------------

def test_titles_similar_ignores_decorations():
    assert db_cleanup.are_titles_similar(
        "Somebody That I Used To Know (feat. Kimbra)",
        "Somebody That I Used to Know",
    )


def test_titles_not_similar_for_different_songs():
    assert not db_cleanup.are_titles_similar("Faint", "Two Faced")


# -- is_duplicate (the core rule) --------------------------------------------

def test_same_length_same_title_is_duplicate():
    # Different video, same length, title differs only by a version suffix
    # clean_title strips (feat./remaster/live) -> duplicate.
    a = _row(1, "Gotye", "Somebody That I Used to Know", 244.0)
    b = _row(2, "Gotye", "Somebody That I Used To Know (feat. Kimbra)", 245.0)
    assert db_cleanup.is_duplicate(a, b)


def test_same_length_case_only_title_is_duplicate():
    # Pure case difference, same length -> duplicate.
    a = _row(1, "Ween", "Tried And True", 242.0)
    b = _row(2, "Ween", "tried and true", 243.0)
    assert db_cleanup.is_duplicate(a, b)


def test_different_length_same_title_is_allowed():
    # Same title but a different length -> distinct version, not a duplicate.
    a = _row(1, "The Jesus Lizard", "Monkey Trick", 258.0)
    b = _row(2, "The Jesus Lizard", "Monkey Trick - Live", 276.0)
    assert not db_cleanup.is_duplicate(a, b)


def test_swapped_artist_title_different_songs_not_merged():
    # Real-world trap: artist/title swapped so title collides but songs differ;
    # durations disagree, so they must NOT be merged.
    a = _row(33, "The Emptiness Machine", "Linkin Park", 201.0)
    b = _row(39, "Faint", "Linkin Park", 168.0)
    assert not db_cleanup.is_duplicate(a, b)


def test_unknown_duration_requires_near_identical_title():
    # Unknown durations: only merge on a near-exact title.
    near = _row(1, "Gotye", "Somebody That I Used to Know", None)
    also = _row(2, "Gotye", "Somebody That I Used To Know", None)
    assert db_cleanup.is_duplicate(near, also)

    loose = _row(3, "Ween", "Tried And True", None)
    amp = _row(4, "Ween", "Tried & True", None)
    assert not db_cleanup.is_duplicate(loose, amp)


def test_incompatible_artist_never_duplicate():
    a = _row(1, "Radiohead", "Karma Police", 250.0)
    b = _row(2, "The Balkanexe Crew", "Karma Police", 251.0)
    assert not db_cleanup.is_duplicate(a, b)
