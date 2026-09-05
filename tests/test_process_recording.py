"""When a transcription may serve as a track's lyrics.

The rule got this wrong once. A killed run had already written two tracks with
``source=whisper``; the retry saw lyrics present, reported "already has
lyrics", and skipped them. Whisper's own earlier output is not a source to
protect from Whisper -- and treating it as one means that re-running after
improving the word filter or changing the model silently does nothing.
"""
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "process_recording",
    Path(__file__).resolve().parent.parent / "scripts" / "process_recording.py",
)
process_recording = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(process_recording)

decide = process_recording.promotion_decision
FLOOR = process_recording.PROMOTE_MIN_CONFIDENCE


def test_a_track_with_no_lyrics_gets_the_transcription():
    promote, reason = decide("", has_text=False, confidence=0.7)
    assert promote
    assert "no other source" in reason


def test_a_real_source_is_never_overwritten():
    """LyricFind words beat a Whisper guess, however confident it sounds."""
    promote, reason = decide("ytmusic_panel_lyricfind", has_text=True,
                             confidence=0.95)
    assert not promote
    assert "lyricfind" in reason


def test_lrclib_is_not_overwritten_either():
    promote, _ = decide("lrclib", has_text=True, confidence=0.9)
    assert not promote


def test_an_earlier_whisper_result_is_replaced():
    """The bug: the first run used to win permanently, including a worse one."""
    promote, reason = decide("whisper", has_text=True, confidence=0.7)
    assert promote
    assert "earlier whisper" in reason


def test_a_low_confidence_transcription_stays_a_note():
    promote, reason = decide("", has_text=False, confidence=FLOOR - 0.01)
    assert not promote
    assert "below" in reason


def test_a_low_confidence_transcription_does_not_replace_a_better_habit():
    """Even against whisper's own output, the floor still applies."""
    promote, _ = decide("whisper", has_text=True, confidence=0.05)
    assert not promote


def test_confidence_at_the_floor_is_accepted():
    promote, _ = decide("", has_text=False, confidence=FLOOR)
    assert promote


def test_an_unknown_confidence_does_not_block_promotion():
    """No probabilities at all is a missing measurement, not a bad one."""
    promote, _ = decide("", has_text=False, confidence=None)
    assert promote


def test_a_stale_source_string_with_no_text_does_not_block():
    """An empty lyrics row is not a source worth protecting."""
    promote, reason = decide("lrclib", has_text=False, confidence=0.7)
    assert promote
    assert "no other source" in reason


def test_the_floor_is_a_low_bar_by_design():
    """It exists to catch noise, not to judge quality; that is the note's job."""
    assert 0.1 <= FLOOR <= 0.5
