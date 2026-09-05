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


# --- version markers mean different recordings -----------------------------
#
# The duration guard is supposed to keep a demo or a live take from being
# merged into the studio cut. It cannot: only 179 of 676 tracks carry a
# duration, so it reads "unknown" and abstains, leaving a near-exact title as
# the only test -- which "Take The Box" and "Take The Box (Demo)" both pass.


def _ver(artist, title, duration=None):
    """A row for the version tests; the module-level _row takes an id too."""
    return {"artist": artist, "title": title, "duration": duration}


def test_a_demo_is_not_the_album_version():
    assert not db_cleanup.same_version("Take The Box", "Take The Box (Demo)")


def test_a_remaster_is_not_the_original():
    assert not db_cleanup.same_version("Bouree", "Bouree (2001 Remastered Version)")


def test_a_rough_mix_is_not_a_demo():
    assert not db_cleanup.same_version('Instant Hit - "Rough Mix', "Instant Hit - 8 Track Demo")


def test_a_live_take_is_not_the_studio_cut():
    assert not db_cleanup.same_version("Aqualung", "Aqualung (Live)")


def test_two_identically_marked_takes_are_the_same_recording():
    """Markers are compared as sets: it is a *difference* that blocks a merge,
    not the mere presence of a marker."""
    assert db_cleanup.same_version("Aqualung (Live)", "Aqualung - Live")


def test_plain_titles_are_the_same_version():
    assert db_cleanup.same_version("Lights", "Lights")
    assert db_cleanup.same_version("Henry Lee", "Henry Lee")


def test_a_word_containing_a_marker_is_not_a_marker():
    """"Alive" must not read as "live"."""
    assert db_cleanup.version_markers("Stayin' Alive") == frozenset()
    assert db_cleanup.same_version("Stayin' Alive", "Stayin Alive")


def test_a_different_version_is_never_merged_even_at_the_same_length():
    """A live take can run the same length as the studio cut, so equal
    durations are not evidence that two rows are the same recording."""
    studio = _ver("Jethro Tull", "Aqualung", 400.0)
    live = _ver("Jethro Tull", "Aqualung (Live)", 400.0)
    assert not db_cleanup.is_duplicate(studio, live)


def test_a_spelling_variant_is_still_merged():
    """The guard must not block the duplicates it was never about."""
    a = _ver("Omar Rodriguez-Lopez", "Lights")
    b = _ver("Omar Rodríguez-López", "Lights")
    assert db_cleanup.is_duplicate(a, b)


def test_a_credited_artist_variant_is_still_merged():
    a = _ver("Dave Grohl", "Mantra")
    b = _ver("Dave Grohl, Joshua Homme & Trent Reznor", "Mantra")
    assert db_cleanup.is_duplicate(a, b)
