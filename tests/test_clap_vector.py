"""CLAP embeddings: audio and text in one space.

Why this exists rather than more tuning of the 62-dimension spectral vector:
across 3000 random pairs that vector's cosine ran a median of 0.965 with a
p5-p95 spread of 0.10, and mean-centring widened the spread fourteenfold
*without reordering the neighbours*. The narrow range was cosmetic — the
features themselves put The Cranberries next to Macy Gray, and no
normalisation fixes that.

The tests here avoid loading the model (a 600 MB download and seconds of CPU);
they cover the contracts around it, which is where the mistakes live.
"""
from __future__ import annotations

import pytest

from karaoke import clap_vector as cv


# --- shape and identity ----------------------------------------------------

def test_the_dimension_matches_the_model():
    assert cv.CLAP_DIM == 512


def test_the_index_is_separate_from_the_spectral_one():
    """Two spaces answering different questions; mixing them is meaningless."""
    from karaoke import audio_vector

    assert cv.CLAP_INDEX != audio_vector.AUDIO_INDEX


def test_one_embedding_per_track():
    """Unlike a recording, this describes the music, not a capture of it."""
    assert cv.doc_id(7) == "clap:7"
    assert cv.doc_id(7) == cv.doc_id(7)


def test_the_sample_rate_is_the_model_s_own():
    """CLAP's front end expects 48kHz; resampling changes what it hears.

    Deliberately not the 22.05kHz the spectral vector uses, where halving the
    rate is free because nothing there needs the top octave.
    """
    assert cv.SAMPLE_RATE == 48000


def test_windows_are_spread_rather_than_taken_from_the_opening():
    """A song's first ten seconds are regularly an intro that sounds nothing
    like the rest — the same trap that made Whisper detect the wrong language."""
    assert cv.MAX_WINDOWS > 1


# --- normalising -----------------------------------------------------------

def test_vectors_come_back_unit_length():
    """So cosine is a plain dot product and loudness does not dominate."""
    out = cv._normalise([3.0, 4.0])
    assert sum(x * x for x in out) == pytest.approx(1.0)


def test_a_zero_vector_does_not_divide_by_zero():
    assert cv._normalise([0.0, 0.0]) == [0.0, 0.0]


# --- refusing rather than guessing -----------------------------------------

def test_empty_text_embeds_to_nothing():
    assert cv.embed_text("") is None
    assert cv.embed_text("   ") is None


def test_a_missing_stack_returns_none_rather_than_raising(monkeypatch):
    """Same contract as analyze and audio_vector: optional, never fatal."""
    monkeypatch.setattr(cv, "available", lambda: False)
    assert cv.embed_text("anything") is None
    assert cv.embed_audio("/nonexistent.webm") is None


def test_unreadable_audio_returns_none(monkeypatch):
    monkeypatch.setattr(cv, "available", lambda: True)
    assert cv.embed_audio("/nonexistent/definitely-not-here.webm") is None


# --- the document ----------------------------------------------------------

def test_a_document_carries_what_a_result_needs_to_explain_itself():
    doc = cv.build_doc(track_id=3, artist="A", title="T",
                       vector=[0.0] * cv.CLAP_DIM,
                       embedded_at="2026-09-05T00:00:00+00:00",
                       detected_key="D minor", bpm=120.0)
    assert doc["track_id"] == 3
    assert doc["detected_key"] == "D minor"
    assert doc["bpm"] == 120.0
    assert len(doc["clap_vector"]) == cv.CLAP_DIM


def test_the_index_mapping_declares_the_right_dimension():
    created = {}

    class _Client:
        class indices:
            @staticmethod
            def exists(index): return False

            @staticmethod
            def create(index, body): created.update(body)

    assert cv.ensure_index(_Client()) is True
    props = created["mappings"]["properties"]
    assert props["clap_vector"]["dimension"] == cv.CLAP_DIM
    assert props["clap_vector"]["method"]["space_type"] == "cosinesimil"


def test_an_existing_index_is_not_recreated():
    class _Client:
        class indices:
            @staticmethod
            def exists(index): return True

            @staticmethod
            def create(index, body): raise AssertionError("must not recreate")

    assert cv.ensure_index(_Client()) is False


# --- what the scores mean --------------------------------------------------

def test_the_baseline_reflects_the_measured_distribution():
    """Measured over 60 library tracks: p5 0.589, median 0.779, p95 0.909.

    A real spread, unlike the spectral vector's 0.885-0.987, so a score can be
    read directly instead of needing percentile translation.
    """
    assert 0.6 < cv.SIMILARITY_TYPICAL < cv.SIMILARITY_NOTABLE < 1.0


def test_clap_is_better_spread_than_the_spectral_vector():
    from karaoke import search

    clap_spread = cv.SIMILARITY_NOTABLE - cv.SIMILARITY_TYPICAL
    spectral_spread = search.SIMILARITY_NOTABLE - search.SIMILARITY_TYPICAL
    assert clap_spread > spectral_spread
