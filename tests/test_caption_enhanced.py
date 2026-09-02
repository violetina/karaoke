"""Tests for Enhanced LRC output from YouTube json3 captions (issue #21)."""
from __future__ import annotations

import json

import pytest

from karaoke.caption_sync import json3_to_enhanced_lrc, json3_to_lrc
from karaoke.lyrics import parse_enhanced_lrc


def _json3(events):
    return json.dumps({"events": events})


def _cue(start, tokens, step=300):
    """A cue whose words carry per-word offsets, as real json3 does."""
    return {
        "tStartMs": start,
        "segs": [
            {"utf8": (" " if i else "") + w, "tOffsetMs": i * step}
            for i, w in enumerate(tokens)
        ],
    }


def test_enhanced_output_carries_per_word_tags():
    payload = _json3([_cue(12_000, ["I", "see", "trees"])])
    out = json3_to_enhanced_lrc(payload)
    assert "<00:12.00>I" in out
    assert "<00:12.30>see" in out
    assert "<00:12.60>trees" in out


def test_enhanced_output_round_trips_to_word_times():
    payload = _json3([_cue(12_000, ["I", "see", "trees"])])
    lines, _, words = parse_enhanced_lrc(json3_to_enhanced_lrc(payload))
    assert lines == [(12.0, "I see trees")]
    assert words[0] == pytest.approx([12.0, 12.3, 12.6])


def test_line_tag_matches_first_word_tag():
    """Enhanced LRC spec: the first word timestamp matches the line stamp."""
    out = json3_to_enhanced_lrc(_json3([_cue(21_920, ["Heat", "up", "here."])]))
    line = out.splitlines()[0]
    assert line.startswith("[00:21.92]<00:21.92>Heat")


def test_enhanced_output_strips_noise_and_speakers():
    payload = _json3([
        {"tStartMs": 7205, "segs": [{"utf8": "[music]"}]},
        {"tStartMs": 21_920, "segs": [
            {"utf8": ">> Heat", "tOffsetMs": 0},
            {"utf8": " up", "tOffsetMs": 80},
        ]},
    ])
    lines, _, _ = parse_enhanced_lrc(json3_to_enhanced_lrc(payload))
    assert [t for _, t in lines] == ["Heat up"]


def test_enhanced_output_splits_long_cues_like_plain_output():
    payload = _json3([_cue(0, [f"w{i}" for i in range(12)], step=500)])
    lines, _, words = parse_enhanced_lrc(
        json3_to_enhanced_lrc(payload, max_words=5)
    )
    assert len(lines) == 3
    # Word timings stay attached to their own chunk.
    assert words[0] == pytest.approx([0.0, 0.5, 1.0, 1.5, 2.0])
    assert words[1][0] == pytest.approx(2.5)


def test_enhanced_output_preserves_all_words():
    tokens = [f"w{i}" for i in range(23)]
    lines, _, _ = parse_enhanced_lrc(
        json3_to_enhanced_lrc(_json3([_cue(0, tokens)]), max_words=7)
    )
    assert " ".join(t for _, t in lines).split() == tokens


def test_enhanced_empty_payload_is_empty_string():
    assert json3_to_enhanced_lrc(_json3([])) == ""


def test_enhanced_rejects_malformed_json():
    with pytest.raises(ValueError):
        json3_to_enhanced_lrc("not json")


def test_plain_and_enhanced_agree_on_line_starts():
    """The enhanced form must not shift any line's start time."""
    payload = _json3([
        _cue(1_000, ["one", "two"]),
        _cue(9_000, ["three", "four", "five"]),
    ])
    plain_lines = parse_enhanced_lrc(json3_to_lrc(payload))[0]
    enh_lines = parse_enhanced_lrc(json3_to_enhanced_lrc(payload))[0]
    assert [t for t, _ in plain_lines] == [t for t, _ in enh_lines]
    assert [s for _, s in plain_lines] == [s for _, s in enh_lines]
