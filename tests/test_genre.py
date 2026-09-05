"""Zero-shot genre labelling, and what it is honestly worth.

essentia's Discogs-EffNet models need TensorFlow, which will not install here
(no cp314 wheels, and the ebuild's libclang dependency exists in no repo), so
CLAP does the job instead: audio and text share a space, and classifying is
embedding the labels and taking the nearest.

Two measurements shape everything below, both from labelling 120 real tracks.
"pop" won 39 of them and "punk rock" 29 — far beyond what the library holds —
because both sit close to a great deal of music. And correcting that by
subtracting each label's mean was tried and rejected: it assumes a uniform
prior over genres, and this library really is mostly rock, so it moved
Portishead from "trip hop" to "hip hop" while fixing Modjo.
"""
from __future__ import annotations

import pytest

from karaoke import genre


def _labels(**scores):
    """Label vectors contrived so each scores exactly as asked."""
    return {name: [value] for name, value in scores.items()}


TRACK = [1.0]


# --- picking a label -------------------------------------------------------

def test_the_best_label_wins():
    verdict = genre.classify(TRACK, _labels(**{"trip hop": 0.62, "jazz": 0.40}))
    assert verdict.genre == "trip hop"
    assert verdict.runner_up == "jazz"


def test_nothing_convincing_is_left_unlabelled():
    """Better no label than filed under whichever happened to be nearest."""
    assert genre.classify(TRACK, _labels(jazz=0.30, folk=0.28)) is None


def test_an_empty_vector_labels_nothing():
    assert genre.classify([], _labels(jazz=0.9)) is None
    assert genre.classify(TRACK, {}) is None


def test_the_floor_came_from_the_observed_range():
    """Winners across 120 tracks ran 0.517 to 0.708."""
    assert 0.5 <= genre.MIN_SCORE <= 0.6


# --- how clear the decision was -------------------------------------------

def test_a_thin_margin_is_not_a_clear_answer():
    """Melvins really did come back "heavy metal 0.708, punk rock 0.689",
    which is a genuine ambiguity for sludge rather than a decision."""
    verdict = genre.classify(TRACK, _labels(**{"heavy metal": 0.708,
                                               "punk rock": 0.689}))
    assert verdict.genre == "heavy metal"
    assert verdict.clear is False


def test_a_wide_margin_is_clear():
    verdict = genre.classify(TRACK, _labels(**{"trip hop": 0.62, "folk": 0.45}))
    assert verdict.clear is True


def test_the_runner_up_is_kept_because_it_is_informative():
    """"heavy metal, close to punk rock" describes sludge better than either."""
    verdict = genre.classify(TRACK, _labels(**{"heavy metal": 0.70,
                                               "punk rock": 0.69}))
    assert verdict.runner_up == "punk rock"
    assert "close to" in genre.describe(verdict)


# --- the labels that absorb uncertainty ------------------------------------

def test_broad_labels_are_marked_as_such():
    """"pop" won a third of the library; a win there means less."""
    verdict = genre.classify(TRACK, _labels(pop=0.60, folk=0.40))
    assert verdict.broad is True


def test_a_specific_label_is_not_broad():
    verdict = genre.classify(TRACK, _labels(**{"trip hop": 0.60, "folk": 0.40}))
    assert verdict.broad is False


def test_confidence_needs_both_a_clear_win_and_a_specific_label():
    thin_specific = genre.classify(TRACK, _labels(**{"trip hop": 0.60,
                                                     "dub": 0.595}))
    wide_broad = genre.classify(TRACK, _labels(pop=0.65, folk=0.40))
    wide_specific = genre.classify(TRACK, _labels(**{"trip hop": 0.65,
                                                     "folk": 0.40}))
    assert thin_specific.confident is False       # clear? no
    assert wide_broad.confident is False          # specific? no
    assert wide_specific.confident is True


def test_the_broad_set_is_the_measured_one():
    assert "pop" in genre.BROAD_LABELS
    assert "punk rock" in genre.BROAD_LABELS
    assert "trip hop" not in genre.BROAD_LABELS


# --- the taxonomy is the design -------------------------------------------

def test_rock_is_fine_grained_because_the_library_is_rock():
    rock = [g for g in genre.GENRES if "rock" in g or g in
            {"grunge", "shoegaze", "heavy metal", "hardcore punk", "post-punk"}]
    assert len(rock) >= 10


def test_the_taxonomy_reaches_beyond_rock():
    """A jazz or hip hop track must have somewhere to land, or it is forced
    into a rock label -- which is how Modjo became "pop"."""
    for needed in ("jazz", "hip hop", "house", "classical", "folk", "reggae"):
        assert needed in genre.GENRES


def test_labels_are_unique():
    assert len(set(genre.GENRES)) == len(genre.GENRES)


def test_the_prompt_is_a_sentence():
    """CLAP was trained on captions; a bare word scores worse."""
    assert genre.PROMPT.format(genre="jazz").endswith(".")
    assert " " in genre.PROMPT.format(genre="jazz")


# --- describing ------------------------------------------------------------

def test_nothing_describes_as_unlabelled():
    assert genre.describe(None) == "unlabelled"


def test_a_clear_verdict_does_not_mention_a_runner_up():
    verdict = genre.classify(TRACK, _labels(**{"trip hop": 0.62, "folk": 0.40}))
    assert "close to" not in genre.describe(verdict)
