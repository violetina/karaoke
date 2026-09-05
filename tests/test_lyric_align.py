"""Tests for laying real lyrics onto Whisper's rhythm (offline)."""
import pytest

from karaoke import lyric_align as la
from karaoke.whisper_sync import Word


def _words(pairs):
    return [Word(start=t, end=t + 0.4, text=w) for t, w in pairs]


# Whisper's actual output for Springsteen's "The River": right rhythm, wrong
# words ("up to do" -> "up to doom", "daddy done" -> "daddy John"), plus the
# music-note artifacts it emits over instrumental passages.
RIVER_WORDS = _words([
    (15.86, "🎵I"), (16.4, "come"), (16.9, "from"), (17.3, "down"), (17.8, "in"),
    (18.1, "the"), (18.6, "valley"), (19.5, "🎵"), (20.1, "🎵Where"),
    (21.24, "mister,"), (21.9, "when"), (22.3, "you're"), (22.9, "gone"),
    (24.1, "🎵They"), (24.6, "bring"), (25.1, "you"), (26.32, "up"), (26.8, "to"),
    (27.2, "doom"), (28.6, "Like"), (29.1, "your"), (29.6, "daddy"), (30.2, "John"),
    (32.76, "Me"), (33.2, "and"), (33.6, "Mary"), (34.1, "we"), (34.5, "met"),
    (34.9, "in"), (35.3, "high"), (35.9, "school"), (38.06, "was"), (38.5, "just"),
    (39.1, "17"),
])

RIVER_LYRICS = """I come from down in the valley
Where, mister, when you're young
They bring you up to do
Like your daddy done

Me and Mary, we met in high school
When she was just 17"""


# --- normalization ---------------------------------------------------------

def test_normalize_strips_artifacts_and_punctuation():
    assert la.normalize_word("🎵They") == "they"
    assert la.normalize_word("mister,") == "mister"
    assert la.normalize_word("🎵") == ""
    assert la.normalize_word("// Music //") == ""


def test_normalize_folds_contractions_and_numbers():
    assert la.normalize_word("we'd") == la.normalize_word("wed")
    assert la.normalize_word("17") == la.normalize_word("seventeen")


# --- alignment -------------------------------------------------------------

def test_aligned_text_is_the_real_lyrics_not_whisper_words():
    """The whole point: Whisper gives timing, the source gives words."""
    out = la.align_lines(RIVER_LYRICS.strip().splitlines(), RIVER_WORDS)
    text = [line for _, line in out]
    assert "They bring you up to do" in text        # not "up to doom"
    assert "Like your daddy done" in text           # not "daddy John"
    assert not any("🎵" in line for line in text)


def test_lines_anchor_near_where_whisper_heard_them():
    lrc = la.align_lyrics_to_lrc(RIVER_LYRICS, RIVER_WORDS)
    lines = lrc.splitlines()
    assert lines[0].startswith("[00:15.")          # "I come from down in the valley"
    assert lines[4].startswith("[00:32.")          # "Me and Mary, we met in high school"


def test_blank_lines_do_not_consume_a_timestamp():
    """Stanza separators have nothing to sing."""
    lrc = la.align_lyrics_to_lrc(RIVER_LYRICS, RIVER_WORDS)
    assert len(lrc.splitlines()) == 6              # 6 real lines, blank dropped


def test_timestamps_never_go_backwards():
    out = la.align_lines(RIVER_LYRICS.strip().splitlines(), RIVER_WORDS)
    times = [t for t, _ in out]
    assert times == sorted(times)


def test_line_whisper_never_heard_is_interpolated_between_neighbours():
    """A dropped line still needs a sensible time, not 0.0."""
    lyrics = ["first line here", "totally unheard interlude", "final line here"]
    words = _words([(10.0, "first"), (10.5, "line"), (11.0, "here"),
                    (30.0, "final"), (30.5, "line"), (31.0, "here")])
    out = la.align_lines(lyrics, words)
    assert out[0][0] == 10.0
    assert out[2][0] == 30.0
    assert 10.0 < out[1][0] < 30.0


# --- degenerate inputs -----------------------------------------------------

def test_no_whisper_words_still_returns_every_line():
    lyrics = ["one", "two", "three", "four"]
    out = la.align_lines(lyrics, [], total_duration=120.0)
    assert [line for _, line in out] == lyrics
    assert [t for t, _ in out] == sorted(t for t, _ in out)


def test_no_lyrics_returns_empty():
    assert la.align_lines([], RIVER_WORDS) == []
    assert la.align_lyrics_to_lrc("", RIVER_WORDS) == ""


def test_whisper_heard_only_artifacts():
    """An instrumental transcribed as pure "🎵" anchors nothing."""
    out = la.align_lines(["a line", "another line"],
                         _words([(1.0, "🎵"), (2.0, "🎵")]), total_duration=60.0)
    assert len(out) == 2


def test_whisper_hearing_extra_words_does_not_shift_lines():
    """Invented phrases are absorbed as edits, not treated as anchors."""
    lyrics = ["hello darkness my old friend", "ive come to talk with you again"]
    words = _words([
        (5.0, "hello"), (5.4, "darkness"), (5.9, "my"), (6.2, "old"), (6.6, "friend"),
        (7.0, "yeah"), (7.3, "uh"), (7.6, "hmm"),
        (9.0, "ive"), (9.3, "come"), (9.7, "to"), (10.0, "talk"), (10.4, "with"),
        (10.8, "you"), (11.2, "again"),
    ])
    out = la.align_lines(lyrics, words)
    assert out[0][0] == 5.0
    assert out[1][0] == 9.0


# --- word length and singing rate ------------------------------------------
#
# Timings were derived from word *starts* alone, and untimed lines were spaced
# evenly. Both ignore how long a line takes to sing, which is what made an
# otherwise well-anchored alignment feel off the beat.

class _W:
    def __init__(self, text, start, end=None):
        self.text, self.start = text, start
        self.end = start if end is None else end


def test_a_plausible_word_span_is_accepted():
    assert la.word_span_ok(1.0, 1.4)


def test_an_instantaneous_word_is_rejected():
    """A zero-length word is a decoding glitch, not a syllable."""
    assert not la.word_span_ok(1.0, 1.001)


def test_a_word_spanning_an_instrumental_is_rejected():
    """Whisper attaches one token to a whole break; anchoring on it puts the
    line seconds away from the singing."""
    assert not la.word_span_ok(10.0, 40.0)


def test_implausible_words_are_dropped_from_the_stream():
    toks, starts, last = la._whisper_tokens(
        [_W("real", 1.0, 1.4), _W("stretched", 2.0, 30.0), _W("also", 31.0, 31.3)])
    assert toks == ["real", "also"]
    assert last == pytest.approx(31.3)


def test_a_multi_word_token_is_spread_across_its_span():
    """Stacking them all on the start loses the rhythm inside the token."""
    toks, starts, _ = la._whisper_tokens([_W("two words", 10.0, 11.0)])
    assert toks == ["two", "words"]
    assert starts[0] == pytest.approx(10.0)
    assert starts[1] == pytest.approx(10.5)


def test_a_longer_line_weighs_more():
    short = la.line_weight("what's it for")
    long = la.line_weight(
        "no, don't fake me don't ya know no one's ready for your war")
    assert long > short * 2


def test_even_a_one_word_line_has_weight():
    assert la.line_weight("hey") >= 1.0
    assert la.line_weight("") == 0.0


# -- weighted interpolation ---------------------------------------------

def test_untimed_lines_are_spaced_by_length_not_evenly():
    """A long line and a two-word line should not take the same time."""
    times = [0.0, None, None, 12.0]
    weights = [1.0, 10.0, 2.0, 1.0]
    out = la._interpolate(times, None, weights)
    first_gap = out[1] - out[0]
    second_gap = out[2] - out[1]
    assert second_gap > first_gap * 3     # the long line eats the gap


def test_interpolation_stays_non_decreasing():
    out = la._interpolate([5.0, None, None, 1.0], None, [1.0] * 4)
    assert out == sorted(out)


# -- rejecting impossible anchors ---------------------------------------

def test_an_anchor_needing_impossible_speed_is_dropped():
    """Repeated lyrics let the matcher anchor a late line to an early repeat.
    On a real track that pinned twenty lines into four seconds."""
    weights = [10.0] * 5
    per_line = [0.0, None, None, None, 1.0]      # 4 long lines in 1 second
    kept = la._drop_impossible_anchors(per_line, weights)
    assert kept[4] is None                        # the suspect anchor is gone
    assert kept[0] == 0.0                         # the first is kept


def test_a_comfortable_anchor_is_kept():
    weights = [10.0] * 5
    per_line = [0.0, None, None, None, 40.0]
    kept = la._drop_impossible_anchors(per_line, weights)
    assert kept[4] == 40.0


def test_dropping_anchors_leaves_the_first_one_alone():
    kept = la._drop_impossible_anchors([None, 5.0, 5.1], [10.0] * 3)
    assert kept[1] == 5.0
    assert kept[2] is None


def test_the_result_is_still_one_time_per_line():
    lines = ["a longer line here", "short", "another longish line", "end"]
    words = [_W("longer", 1.0, 1.4), _W("end", 20.0, 20.4)]
    out = la.align_lines(lines, words, total_duration=25.0)
    assert len(out) == len(lines)
    assert [t for t, _ in out] == sorted(t for t, _ in out)
