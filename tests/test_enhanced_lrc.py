"""Tests for Enhanced LRC parsing and empty-timestamp line-end markers."""
from __future__ import annotations

import pytest

from karaoke.lyrics import parse_enhanced_lrc, parse_lrc, parse_lrc_with_ends


# --- empty-text timestamps as line ends (issue #21) ---

def test_parse_lrc_with_ends_reports_end_marker():
    """`[00:14.00]` with no text means the previous line ends there."""
    lines, ends, _ = parse_lrc_with_ends(
        "[00:10.00]first\n[00:14.00]\n[01:05.00]after the riff"
    )
    assert [t for t, _ in lines] == [10.0, 65.0]
    assert ends == {0: 14.0}


def test_end_marker_applies_to_preceding_line_only():
    lines, ends, _ = parse_lrc_with_ends(
        "[00:10.00]a\n[00:12.00]b\n[00:20.00]\n[01:00.00]c"
    )
    assert [t for t, _ in lines] == [10.0, 12.0, 60.0]
    assert ends == {1: 20.0}


def test_leading_end_marker_is_ignored():
    """An end stamp before any lyric has nothing to terminate."""
    lines, ends, _ = parse_lrc_with_ends("[00:05.00]\n[00:10.00]first")
    assert [t for t, _ in lines] == [10.0]
    assert ends == {}


def test_metadata_lines_are_not_end_markers():
    lines, ends, _ = parse_lrc_with_ends("[ar: Artist]\n[00:10.00]first")
    assert [t for t, _ in lines] == [10.0]
    assert ends == {}


def test_parse_lrc_remains_backwards_compatible():
    """The original helper keeps its exact signature and behaviour."""
    assert parse_lrc("[00:10.00]a\n[00:12.00]b") == [(10.0, "a"), (12.0, "b")]
    # Empty stamps are still skipped by the plain parser.
    assert parse_lrc("[00:10.00]a\n[00:14.00]\n[00:20.00]b") == [
        (10.0, "a"), (20.0, "b")
    ]


# --- Enhanced LRC word timings ---

def test_parse_enhanced_lrc_extracts_word_times():
    lines, ends, words = parse_enhanced_lrc(
        "[00:12.00]<00:12.00>I <00:12.30>see <00:12.60>trees"
    )
    assert lines == [(12.0, "I see trees")]
    assert words[0] == pytest.approx([12.0, 12.3, 12.6])


def test_enhanced_lrc_text_has_no_word_tags():
    lines, _, _ = parse_enhanced_lrc("[00:01.00]<00:01.00>a <00:01.50>b")
    assert lines[0][1] == "a b"


def test_plain_lrc_parses_through_enhanced_parser():
    lines, ends, words = parse_enhanced_lrc("[00:10.00]hello world")
    assert lines == [(10.0, "hello world")]
    assert words == {}
    assert ends == {}


def test_enhanced_and_end_markers_combine():
    lines, ends, words = parse_enhanced_lrc(
        "[00:10.00]<00:10.00>one <00:10.50>two\n[00:14.00]\n[01:00.00]next"
    )
    assert [t for t, _ in lines] == [10.0, 60.0]
    assert ends == {0: 14.0}
    assert words[0] == pytest.approx([10.0, 10.5])


def test_enhanced_lrc_handles_over_one_hour():
    lines, _, words = parse_enhanced_lrc("[62:03.00]<62:03.00>late")
    assert lines[0][0] == pytest.approx(3723.0)
    assert words[0] == pytest.approx([3723.0])


def test_enhanced_lrc_ignores_trailing_end_tag_as_word():
    """A trailing <mm:ss.xx> with no word after it marks the line's end."""
    lines, ends, words = parse_enhanced_lrc(
        "[00:10.00]<00:10.00>one <00:10.50>two <00:11.20>"
    )
    assert lines[0][1] == "one two"
    assert words[0] == pytest.approx([10.0, 10.5])
    assert ends[0] == pytest.approx(11.2)
