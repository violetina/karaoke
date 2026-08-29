"""Tests for Whisper word-grouping -> LRC (no model needed)."""
from karaoke.whisper_sync import Word, group_words_to_lines, lines_to_lrc


def _w(start, end, text):
    return Word(start=start, end=end, text=text)


def test_group_splits_on_long_pause():
    words = [
        _w(0.0, 0.3, "hello"), _w(0.4, 0.7, "there"),
        _w(2.0, 2.3, "friend"),  # 1.3s gap -> new line
    ]
    lines = group_words_to_lines(words, pause_split=0.8)
    assert lines == [(0.0, "hello there"), (2.0, "friend")]


def test_group_splits_on_max_words():
    words = [_w(i * 0.1, i * 0.1 + 0.05, f"w{i}") for i in range(12)]
    lines = group_words_to_lines(words, max_words=5, max_chars=999, pause_split=99)
    # 12 words / 5 per line -> 3 lines (5,5,2)
    assert len(lines) == 3
    assert lines[0][1].split() == ["w0", "w1", "w2", "w3", "w4"]


def test_group_splits_on_max_chars():
    words = [_w(0.0, 0.1, "aaaa"), _w(0.2, 0.3, "bbbb"), _w(0.4, 0.5, "cccc")]
    lines = group_words_to_lines(words, max_chars=9, max_words=99, pause_split=99)
    # "aaaa bbbb" = 9 chars ok; adding " cccc" -> 14 > 9 -> new line
    assert lines == [(0.0, "aaaa bbbb"), (0.4, "cccc")]


def test_group_line_timestamp_is_first_word():
    words = [_w(5.5, 5.8, "start"), _w(6.0, 6.3, "here")]
    lines = group_words_to_lines(words)
    assert lines[0][0] == 5.5


def test_group_skips_empty_words():
    words = [_w(0.0, 0.1, "  "), _w(0.2, 0.4, "real")]
    lines = group_words_to_lines(words)
    assert lines == [(0.2, "real")]


def test_group_empty_input():
    assert group_words_to_lines([]) == []


def test_lines_to_lrc_format():
    lrc = lines_to_lrc([(5.5, "hi"), (65.25, "later")])
    assert lrc == "[00:05.50] hi\n[01:05.25] later"


def test_roundtrip_lrc_parseable():
    from karaoke.lyrics import parse_lrc
    words = [_w(0.0, 0.3, "one"), _w(1.5, 1.8, "two")]
    lrc = lines_to_lrc(group_words_to_lines(words))
    parsed = parse_lrc(lrc)
    assert [t for t, _ in parsed] == [0.0, 1.5]
