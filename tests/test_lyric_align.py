"""Tests for laying real lyrics onto Whisper's rhythm (offline)."""
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
