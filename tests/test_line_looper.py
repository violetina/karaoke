from __future__ import annotations

import pytest

from karaoke import line_looper as ll


def test_word_weight_longer_word_weighs_more():
    assert ll.word_weight("hi") < ll.word_weight("wondering")


def test_word_weight_punctuation_adds_pause_bonus():
    base = ll.word_weight("wait")
    with_bang = ll.word_weight("wait!")
    assert with_bang > base


def test_word_weight_empty_is_zero():
    assert ll.word_weight("") == 0.0
    assert ll.word_weight("   ") == 0.0


def test_distribute_word_times_single_word_is_start():
    assert ll.distribute_word_times(["hello"], 10.0, 12.0) == [10.0]


def test_distribute_word_times_first_and_last_match_anchors():
    words = ["I", "been", "wondering", "where", "you", "are"]
    times = ll.distribute_word_times(words, 10.0, 13.0)
    assert len(times) == len(words)
    assert times[0] == 10.0
    assert times[-1] == 13.0


def test_distribute_word_times_is_monotonic():
    words = ["what's", "it", "for"]
    times = ll.distribute_word_times(words, 5.0, 5.5)
    assert all(b >= a for a, b in zip(times, times[1:]))


def test_distribute_word_times_weights_long_words_more_than_short():
    # The middle word ("wondering") should occupy noticeably more of the
    # span -- i.e. the gap *after* it starts -- than the short "it" before it.
    words = ["it", "wondering", "it"]
    times = ll.distribute_word_times(words, 0.0, 3.0)
    duration_of_it = times[1] - times[0]
    duration_of_wondering = times[2] - times[1]
    assert duration_of_wondering > duration_of_it


def test_distribute_word_times_rejects_end_before_start():
    with pytest.raises(ValueError):
        ll.distribute_word_times(["a", "b"], 5.0, 4.0)


def test_distribute_word_times_respects_min_word_gap_on_a_tight_span():
    # Many words crammed into a span shorter than n * MIN_WORD_S still
    # produces a monotonic, non-overlapping sequence rather than raising.
    words = [str(i) for i in range(10)]
    times = ll.distribute_word_times(words, 0.0, 0.05)
    assert all(b >= a for a, b in zip(times, times[1:]))


def test_render_enhanced_lrc_line_matches_caption_sync_convention():
    words = ["I", "see", "trees"]
    times = [12.0, 12.3, 12.6]
    line = ll.render_enhanced_lrc_line(12.0, words, times)
    assert line == "[00:12.00] <00:12.00>I <00:12.30>see <00:12.60>trees"


def test_replace_line_in_synced_lyrics_swaps_only_the_matched_line():
    synced = "[00:10.00] first line\n[00:12.00] second line\n[00:14.00] third line"
    new_line = "[00:12.00] <00:12.00>second <00:12.50>corrected"
    out = ll.replace_line_in_synced_lyrics(synced, 12.0, new_line)
    lines = out.splitlines()
    assert lines[0] == "[00:10.00] first line"
    assert lines[1] == new_line
    assert lines[2] == "[00:14.00] third line"


def test_replace_line_in_synced_lyrics_raises_when_no_match():
    synced = "[00:10.00] only line"
    with pytest.raises(ValueError):
        ll.replace_line_in_synced_lyrics(synced, 99.0, "[01:39.00] x")
