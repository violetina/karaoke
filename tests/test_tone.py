"""Lyric tone: the second axis, and why it is tone rather than genre.

Classifying the *genre* of lyrics was tried and measured at 5% agreement with
the audio genre across 83 tracks -- Will Smith's "Miami" read as psychedelic
rock, Wet Leg's "Chaise Longue" as hip hop at 0.194. That is noise, and the
reason is structural: the words of a pop song and a punk song are not
distinguishable in genre terms, because genre is a property of sound.

Tone is a property of text, so this axis works where that one did not.
"""
from __future__ import annotations

import pytest

from karaoke import tone


def _labels(**scores):
    """Label vectors whose cosine with the track vector is exactly the score.

    Two dimensions, not one: a one-dimensional vector normalises to [1.0]
    whatever its magnitude, so every label would score identically and the
    tests would pass or fail on dict ordering rather than on the values.
    """
    return {name: [value, (max(0.0, 1.0 - value * value)) ** 0.5]
            for name, value in scores.items()}


LYRIC = "x" * (tone.MIN_CHARS + 50)

# The track vector: unit length along the first axis, so a dot product with a
# label is that label's first component.
TRACK_VECTOR = [1.0, 0.0]


def _classify(monkeypatch, **scores):
    monkeypatch.setattr("karaoke.embed.embed_text", lambda text: list(TRACK_VECTOR))
    return tone.classify(LYRIC, _labels(**scores))


# --- reading a tone --------------------------------------------------------

def test_the_strongest_attitude_wins(monkeypatch):
    verdict = _classify(monkeypatch, **{"cynical and sarcastic": 0.42,
                                        "sad and mournful": 0.30})
    assert verdict.tone == "cynical and sarcastic"
    assert verdict.short == "cynical"


def test_a_short_lyric_is_not_read(monkeypatch):
    """A chorus stub is not enough text to judge an attitude from."""
    monkeypatch.setattr("karaoke.embed.embed_text", lambda text: list(TRACK_VECTOR))
    assert tone.classify("too short", _labels(**{"sad and mournful": 0.9})) is None


def test_no_labels_reads_nothing(monkeypatch):
    monkeypatch.setattr("karaoke.embed.embed_text", lambda text: list(TRACK_VECTOR))
    assert tone.classify(LYRIC, {}) is None


def test_a_narrow_margin_is_not_a_clear_reading(monkeypatch):
    """"You Know I'm No Good" scored 0.427 playful against 0.421 tender --
    a coin toss reported as a judgement, and the one it got wrong."""
    verdict = _classify(monkeypatch, **{"joyful and celebratory": 0.427,
                                        "tender and romantic": 0.421})
    assert verdict.clear is False


def test_a_wide_margin_is_clear(monkeypatch):
    verdict = _classify(monkeypatch, **{"sad and mournful": 0.47,
                                        "tender and romantic": 0.32})
    assert verdict.clear is True


# --- what must not be read -------------------------------------------------

def test_a_whisper_transcription_has_no_tone_worth_reading(monkeypatch):
    """Its words are a guess, so the attitude describes the model, not the song."""
    monkeypatch.setattr("karaoke.embed.embed_text", lambda text: list(TRACK_VECTOR))
    labels = _labels(**{"sad and mournful": 0.9})
    assert tone.classify_lyrics(LYRIC, "whisper", labels) is None
    assert tone.classify_lyrics(LYRIC, "whisper_synced", labels) is None


def test_real_words_are_read(monkeypatch):
    monkeypatch.setattr("karaoke.embed.embed_text", lambda text: list(TRACK_VECTOR))
    labels = _labels(**{"sad and mournful": 0.9, "joyful and celebratory": 0.3})
    assert tone.classify_lyrics(LYRIC, "lrclib", labels) is not None


def test_whisper_aligned_words_are_real(monkeypatch):
    """Only the timings came from Whisper there; the words are a real source."""
    monkeypatch.setattr("karaoke.embed.embed_text", lambda text: list(TRACK_VECTOR))
    labels = _labels(**{"sad and mournful": 0.9, "joyful and celebratory": 0.3})
    assert tone.classify_lyrics(LYRIC, "whisper_aligned", labels) is not None


# --- the taxonomy ----------------------------------------------------------

def test_the_tone_set_is_small_on_purpose():
    """Ten labels split the vote between near-synonyms. Measured over 40
    tracks: ten gave 20 clear decisions at a median margin of 0.0241, six gave
    24 at 0.0312 with double the p90."""
    assert len(tone.TONES) <= 7


def test_every_tone_has_a_short_form():
    """"cynical pop" reads; "cynical and sarcastic pop" does not."""
    for name in tone.TONES:
        assert name in tone.SHORT
        assert " " not in tone.SHORT[name]


def test_the_tones_are_attitudes_not_topics():
    """What stance the words take is what qualifies a genre usefully."""
    assert "cynical and sarcastic" in tone.TONES
    assert "defiant and rebellious" in tone.TONES


# --- compounding with a genre ---------------------------------------------

def test_a_clear_tone_qualifies_the_genre():
    verdict = tone.ToneVerdict(tone="cynical and sarcastic", score=0.42,
                               runner_up="sad and mournful", runner_up_score=0.30)
    assert tone.compound("pop", verdict) == "cynical pop"


def test_an_unclear_tone_leaves_the_genre_alone():
    """A coin toss attached to a genre reads as a finding rather than noise."""
    verdict = tone.ToneVerdict(tone="cynical and sarcastic", score=0.42,
                               runner_up="sad and mournful", runner_up_score=0.415)
    assert tone.compound("pop", verdict) == "pop"


def test_no_tone_leaves_the_genre_alone():
    assert tone.compound("pop", None) == "pop"


def test_no_genre_compounds_to_nothing():
    """An instrumental has a genre and no tone; the reverse can also happen."""
    verdict = tone.ToneVerdict(tone="sad and mournful", score=0.5)
    assert tone.compound("", verdict) == ""
