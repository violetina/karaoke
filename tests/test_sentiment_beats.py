"""Tests for lexicon mood detection and beat/flash timing (pure functions)."""
from karaoke.sentiment import mood_of, score_line, MOODS
from karaoke.beats import beat_on, line_pulse, nearest_bpm_hold


def test_mood_happy():
    assert mood_of("I am so happy and free, let's dance") == "happy"


def test_mood_sad():
    assert mood_of("tears fall in the cold dark rain, all alone") == "sad"


def test_mood_angry():
    assert mood_of("burn it all down with rage and fire") == "angry"


def test_mood_tender():
    assert mood_of("hold me close forever, my love") == "tender"


def test_mood_neutral_when_no_signal():
    assert mood_of("the number is seven and the door is blue-ish") in MOODS
    assert mood_of("walked to the store today") == "neutral"
    assert mood_of("") == "neutral"


def test_mood_is_always_valid():
    for line in ["", "x", "love hate love", "happy sad happy sad"]:
        assert mood_of(line) in MOODS


def test_score_line_counts_repeats():
    s = score_line("love love love")
    assert s["tender"] == 3


def test_mood_tiebreak_prefers_specific_then_positive():
    # equal happy(1: "happy") and sad(1: "sad") -> happy wins the valence tie
    assert mood_of("happy sad") == "happy"
    # anger is more specific than a generic positive on a tie
    assert mood_of("happy hate") == "angry"


def test_beat_on_within_hold_after_beat():
    beats = [1.0, 2.0, 3.0]
    assert beat_on(beats, 2.0) is True
    assert beat_on(beats, 2.05, hold=0.11) is True
    assert beat_on(beats, 2.5, hold=0.11) is False


def test_beat_on_before_first_beat_and_empty():
    assert beat_on([1.0, 2.0], 0.5) is False
    assert beat_on([], 5.0) is False


def test_line_pulse():
    assert line_pulse(10.0, 10.0) is True
    assert line_pulse(10.0, 10.15, hold=0.18) is True
    assert line_pulse(10.0, 11.0) is False
    assert line_pulse(None, 5.0) is False   # intro: no active line


def test_nearest_bpm_hold():
    assert nearest_bpm_hold(0) == 0.18            # unknown -> cap
    assert nearest_bpm_hold(120) == 0.125         # 0.5s period * 0.25
    assert nearest_bpm_hold(240) <= 0.18          # fast song, capped/short
    assert nearest_bpm_hold(30) == 0.18           # slow -> hits cap
