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


# -- spoken metadata intro ------------------------------------------------

def test_drops_spoken_metadata_intro_morphine_case():
    """The exact live case: a promo-style spoken intro naming artist, album,
    and title, one per line, before the real lyrics start."""
    lines = [
        (4.73, "Morphine"),
        (5.21, "Like Swimming"),
        (5.67, "I Know You (Pt. Iii)"),
        (14.52, "Give me a kiss hello goodbye what's the difference? Just end everything"),
        (19.73, "You say with a smile Wave goodbye hello"),
    ]
    out = wc.drop_spoken_metadata_intro(lines, artist="Morphine", title="I Know You (Pt. III)",
                                        album="Like Swimming")
    assert out == lines[3:]


def test_keeps_real_lyrics_with_no_metadata_match():
    lines = [(0.0, "Tonight"), (2.0, "I'm gonna have myself a real good time")]
    out = wc.drop_spoken_metadata_intro(lines, artist="Queen", title="Don't Stop Me Now")
    assert out == lines


def test_does_not_strip_a_late_coincidental_title_match():
    """A chorus line that happens to echo the title, well past the intro
    window, must never be treated as a spoken tag."""
    lines = [
        (10.0, "Here's the real opening line"),
        (45.0, "Don't stop me now"),  # a chorus hit, not an intro
    ]
    out = wc.drop_spoken_metadata_intro(lines, artist="Queen", title="Don't Stop Me Now")
    assert out == lines


def test_does_not_strip_when_no_metadata_given():
    lines = [(0.0, "Morphine"), (1.0, "real line")]
    assert wc.drop_spoken_metadata_intro(lines) == lines


def test_stops_at_first_non_matching_line_even_mid_intro():
    """Only a genuine, unbroken prefix run is dropped."""
    lines = [
        (1.0, "Morphine"),
        (5.0, "a real opening lyric"),
        (6.0, "Like Swimming"),  # would match album, but comes after a real line
    ]
    out = wc.drop_spoken_metadata_intro(lines, artist="Morphine", album="Like Swimming")
    assert out == lines[1:]
