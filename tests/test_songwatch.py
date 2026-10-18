"""Planned songrec scans and song-end detection."""
import threading

import numpy as np
import pytest

from karaoke import songwatch as sw

SR = sw.SAMPLE_RATE


def test_a_long_way_from_the_end_checks_a_minute_before_it():
    assert sw.next_wait(0.0, 240.0) == 180.0          # 4:00 song matched at 0:00


def test_near_the_end_waits_until_just_after_it():
    assert sw.next_wait(200.0, 240.0) == 46.0         # 40 s left + 6 s margin


def test_waits_stay_within_bounds():
    assert sw.next_wait(239.0, 240.0) == sw.MIN_WAIT_S
    assert sw.next_wait(0.0, 1400.0) == sw.MAX_WAIT_S


def test_unknown_length_halves_the_old_rate():
    assert sw.next_wait(30.0, None) == sw.UNKNOWN_LENGTH_WAIT_S == 60.0
    assert sw.next_wait(None, 200.0) == 60.0


def test_misses_back_off_and_a_match_resets():
    plan = sw.ScanPlan()
    assert [plan.after_miss() for _ in range(5)] == [30.0, 60.0, 120.0, 180.0, 180.0]
    plan.after_match(10.0, 200.0)
    assert plan.misses == 0 and plan.after_miss() == 30.0


def _tone(seconds, level=0.3, freq=440.0):
    t = np.arange(int(SR * seconds)) / SR
    return (level * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _feed(det, signal):
    frame = int(SR * sw.FRAME_S)
    return [(k / SR, s) for k in range(0, len(signal) - frame, frame)
            if (s := det.feed(signal[k:k + frame], now=k / SR))]


def test_a_pause_between_songs_is_an_end():
    stream = np.concatenate([_tone(30), np.zeros(SR * 2, np.float32), _tone(20, freq=330.0)])
    hits = _feed(sw.EndDetector(), stream)
    assert hits and 30.0 <= hits[0][0] <= 32.5 and hits[0][1] == "end"


def test_a_steady_song_never_signals():
    assert _feed(sw.EndDetector(), _tone(120)) == []


def test_a_quiet_bar_is_not_an_end():
    # 0.5 s drop: shorter than GAP_FRAMES.
    stream = np.concatenate([_tone(30), _tone(0.5, level=0.001), _tone(30)])
    assert _feed(sw.EndDetector(), stream) == []


def test_signals_are_debounced():
    pause = np.zeros(SR * 2, np.float32)
    stream = np.concatenate([_tone(20), pause, _tone(8), pause, _tone(20)])
    assert len(_feed(sw.EndDetector(), stream)) == 1   # second pause inside DEBOUNCE_S


def test_a_reset_rebaselines_on_the_new_song():
    det = sw.EndDetector()
    _feed(det, _tone(20, level=0.3))
    det.reset()
    # A much quieter song is its own baseline after a reset, not a 30 dB gap.
    assert _feed(det, _tone(60, level=0.005)) == []


def test_waiting_ends_early_when_the_watcher_fires():
    class FakeWatcher:
        event = threading.Event()
        reason = "end"
    w = FakeWatcher()
    w.event.set()
    assert sw.wait_for_next_scan(threading.Event(), 30.0, w) == "end"
    stop = threading.Event(); stop.set()
    assert sw.wait_for_next_scan(stop, 30.0, None) == "stop"
    assert sw.wait_for_next_scan(threading.Event(), 0.01, None) == "planned"
