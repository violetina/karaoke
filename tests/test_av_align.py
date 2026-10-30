"""Synthetic (no-network) validation of the waveform aligner's sign and scale.

PLAN-av-sync.md step 3.1: before touching any real download, pin the sign
convention and get a rough scale check on a fully synthetic signal. A
deterministic synthetic song (not a real cached download) is used so this
test is hermetic and portable -- it must not depend on files that only exist
on one machine's local cache.

The synthetic "song" generator deliberately uses IRREGULAR (Poisson-like)
onset timing rather than a near-regular tempo: an early attempt with a
regular beat produced near-tied cross-correlation peaks at every beat period
(a real effect -- rhythmic self-similarity -- but an artifact that belongs to
real-pair validation, not to pinning the sign here), which tanked the
confidence margin without indicating any issue in the arithmetic itself.
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import librosa
import numpy as np
import pytest
import soundfile as sf

from karaoke import av_align

SR = 22050


def _make_song(duration_s: float, sr: int, seed: int) -> np.ndarray:
    """Deterministic, onset-rich, non-periodic synthetic signal."""
    rng = np.random.RandomState(seed)
    n = int(duration_s * sr)
    y = np.zeros(n, dtype=np.float64)
    t = 0.0
    while t < duration_s:
        idx = int(t * sr)
        decay_len = int(rng.uniform(0.04, 0.12) * sr)
        if idx + decay_len < n:
            env = np.exp(-np.linspace(0, rng.uniform(8, 16), decay_len))
            freq = rng.uniform(150, 500)
            amp = rng.uniform(0.4, 1.0)
            burst = (rng.randn(decay_len) * 0.4
                    + np.sin(2 * np.pi * freq * np.arange(decay_len) / sr)) * env * amp
            y[idx:idx + decay_len] += burst
        t += rng.uniform(0.15, 0.9)  # irregular inter-onset interval, no beat periodicity
    tt = np.arange(n) / sr
    y += 0.1 * np.sin(2 * np.pi * (280 + 10 * np.sin(tt * 0.3)) * tt)
    y = y / (np.max(np.abs(y)) + 1e-9) * 0.8
    return y.astype(np.float32)


def _lossy_roundtrip(y: np.ndarray, sr: int, bitrate: str = "64k") -> np.ndarray:
    """Encode to mp3 and back, to introduce real codec artifacts (no network)."""
    with tempfile.TemporaryDirectory() as d:
        wav_in = Path(d) / "in.wav"
        mp3 = Path(d) / "x.mp3"
        wav_out = Path(d) / "out.wav"
        sf.write(wav_in, y, sr)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(wav_in),
                        "-b:a", bitrate, str(mp3)], check=True)
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(mp3),
                        str(wav_out)], check=True)
        out, out_sr = sf.read(wav_out)
        if out_sr != sr:
            out = librosa.resample(out.astype(np.float32), orig_sr=out_sr, target_sr=sr)
        return out.astype(np.float32)


def test_constant_offset_recovered_with_correct_sign():
    """video = N seconds of other audio prepended to the audio release.

    Per av_align's documented convention, offset_s = video_time - audio_time,
    so a longer video intro must come back positive and close to N.
    """
    audio = _make_song(60.0, SR, seed=1)
    intro = _make_song(8.0, SR, seed=2)
    n_seconds = 8.0
    video = np.concatenate([intro, audio]).astype(np.float32)

    result = av_align.estimate_offset(audio, video, SR)

    assert result is not None
    assert result.kind == "constant"
    assert result.confidence > 0.5
    assert result.offset_s == pytest.approx(n_seconds, abs=0.2)
    assert abs(result.slope) < av_align.SLOPE_EPS * 2


def test_time_stretch_recovered_as_negative_slope_with_lossy_and_gain_changes():
    """video = intro + (audio sped up 0.3%, mp3 round-tripped, gain cut).

    A video edit that runs faster than the audio release falls further behind
    schedule as the track plays (the SAME audio instant keeps appearing
    earlier in the now-shorter video), so the lag shrinks over time: slope
    must come back negative. The lossy re-encode and gain change stand in for
    the real codec/mastering differences between an audio release and a
    video edit of the same song, per PLAN-av-sync.md.
    """
    audio = _make_song(90.0, SR, seed=1)
    intro = _make_song(5.0, SR, seed=2)
    n_seconds = 5.0
    rate = 1.003  # 0.3% faster; librosa's rate > 1 shortens the output
    stretched = librosa.effects.time_stretch(audio, rate=rate)
    video_raw = np.concatenate([intro, stretched]).astype(np.float32)
    video = _lossy_roundtrip(video_raw * 0.35, SR, "64k")  # quieter + lossy-roundtripped

    result = av_align.estimate_offset(audio, video, SR)

    assert result is not None
    assert result.kind == "slope"
    assert result.confidence > 0.5
    assert result.offset_s == pytest.approx(n_seconds, abs=0.3)
    assert result.slope < 0, "a faster video edit must report a shrinking (negative) lag"
    # theoretical slope is (1/rate - 1) ~= -0.003; allow generous slack for the
    # windowed-fit's quantization noise, this test only pins sign + ballpark
    assert -0.01 < result.slope < -0.0005


def test_unrelated_recordings_report_low_confidence_not_a_bogus_offset():
    song_a = _make_song(60.0, SR, seed=10)
    song_b = _make_song(60.0, SR, seed=99)

    result = av_align.estimate_offset(song_a, song_b, SR)

    assert result is not None
    assert result.kind == "low_confidence"
    assert result.confidence < av_align.MIN_CONFIDENCE


def test_too_short_input_returns_none():
    tiny = np.zeros(100, dtype=np.float32)
    assert av_align.estimate_offset(tiny, tiny, SR) is None
