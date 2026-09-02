"""Tests for the analyze module's graceful degradation (no audio deps needed)."""

from karaoke import analyze


def test_analyze_missing_file_degrades():
    result = analyze.analyze_audio("/nonexistent/path/to/song.webm")
    assert result.key is None
    assert result.method in ("unavailable", "essentia-edma-vote")
    assert result.bpm is None


def test_detect_key_missing_file():
    kr = analyze.detect_key("/nonexistent/song.webm")
    assert kr.key is None
    assert kr.ambiguous is True


def test_key_result_ambiguous_flag():
    from karaoke.musictheory import Key
    high = analyze.KeyResult(Key(0, "major"), 0.9, "5/6", None, "x")
    low = analyze.KeyResult(Key(0, "major"), 0.3, "2/6", None, "x")
    assert high.ambiguous is False
    assert low.ambiguous is True
