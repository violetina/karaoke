"""Filtering artifacts out of a Whisper transcription.

This matters most where Whisper is the only source of words. When real lyrics
exist each token can be corroborated against them; a track no lyric service
carries has nothing to check against, which is the case this was written for.
"""
import pytest

from karaoke import whisper_clean as wc


# -- artifact lines -----------------------------------------------------

@pytest.mark.parametrize("line", [
    "\U0001F3B5",              # the musical note Whisper emits over instrumentals
    "\U0001F3B5 \U0001F3B5",
    "[Music]",
    "(instrumental)",
    "[Applause]",
    "...",
    "---",
    "   ",
    "",
])
def test_artifacts_are_recognised(line):
    assert wc.is_artifact(line)


@pytest.mark.parametrize("line", [
    "no, don't fake me",
    "what's it for",
    "I got no friends to speak of",
    "hey",
])
def test_real_lyrics_are_not_artifacts(line):
    assert not wc.is_artifact(line)


# -- runaway repetition -------------------------------------------------

def test_a_stuck_model_is_collapsed():
    """Whisper on a music bed emits the same phrase dozens of times."""
    out = wc.drop_runaway_repeats(["la"] * 20 + ["a real line"])
    assert out.count("la") == wc.MAX_REPEATS
    assert out[-1] == "a real line"


def test_a_repeated_lyric_survives():
    """Songs really do repeat a line; two or three is a device, not a loop."""
    lines = ["hey", "hey", "come on"]
    assert wc.drop_runaway_repeats(lines) == lines


def test_repeats_are_counted_per_run_not_per_song():
    """A chorus recurring later is not the same as a stuck run."""
    lines = ["chorus", "verse", "chorus", "verse", "chorus"]
    assert wc.drop_runaway_repeats(lines) == lines


def test_repetition_matching_ignores_case_and_padding():
    out = wc.drop_runaway_repeats(["La", " la ", "LA", "la", "la"])
    assert len(out) == wc.MAX_REPEATS


# -- token plausibility -------------------------------------------------

def test_the_only_real_single_letters_are_kept():
    assert wc.looks_like_a_word("a")
    assert wc.looks_like_a_word("I")
    assert not wc.looks_like_a_word("x")


def test_a_token_with_no_vowel_is_noise():
    assert not wc.looks_like_a_word("brr")
    assert not wc.looks_like_a_word("tsk")


def test_ordinary_words_pass():
    for word in ("love", "conquering", "don't", "rhythm"):
        assert wc.looks_like_a_word(word)


# -- the whole filter ---------------------------------------------------

def test_clean_lines_removes_both_kinds_of_junk():
    lines = ["\U0001F3B5", "real one", "la", "la", "la", "la", "la", "[Music]",
             "real two"]
    out = wc.clean_lines(lines)
    assert "\U0001F3B5" not in out and "[Music]" not in out
    assert out.count("la") == wc.MAX_REPEATS
    assert out[0] == "real one" and out[-1] == "real two"


def test_clean_text_round_trips():
    assert wc.clean_text("a line\n\U0001F3B5\nanother") == "a line\nanother"


def test_cleaning_nothing_is_safe():
    assert wc.clean_lines([]) == []
    assert wc.clean_text("") == ""
    assert wc.clean_text(None) == ""
