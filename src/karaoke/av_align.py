"""Waveform-based audio/video offset estimation.

Given two decoded mono audio signals -- the audio release and a video edit of
the same song -- estimates the time offset between them from the audio
itself, without needing lyrics. See PLAN-av-sync.md for the overall design
and status.

Sign convention (matches caption_sync.align_audio_and_video_lyrics / the
mode="video" rows in track_sync_offsets): the returned offset is VIDEO TIME
MINUS AUDIO TIME. A positive offset means the video's cold open/intro runs
longer than the audio release's, so a given musical moment appears LATER in
the video (video_time = audio_time + offset). Pinned empirically against a
synthetic signal with a known prepended intro before this was used on any
real download -- see tests/test_av_align.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np

TARGET_SR = 22050
HOP_LENGTH = 512          # ~23 ms at 22.05 kHz, inside the 20-40 ms band the plan calls for
WINDOW_S = 25.0           # per-window lag estimation window
COARSE_PREFIX_CANDIDATES_S = (20.0, 45.0, 75.0)  # tried short-to-long; the best (highest-
                          # confidence) one wins. A single fixed prefix can't serve both
                          # cases found empirically: a real pair (Balthazar - Bunker) needed
                          # ~60s of real content past a quiet intro before the true lag's
                          # peak (~14x any competitor) cleared MIN_CONFIDENCE, but a speed
                          # change drifts the alignment over a long correlation and blunts
                          # the peak, so a synthetic time-stretch needed a SHORT prefix to
                          # stay confident. Trying several and keeping the best threads that.
COARSE_SEARCH_S = 90.0    # coarse search covers cold opens up to this long
NARROW_BAND_S = 3.0       # +/- band around the coarse lag for the per-window search
MIN_CONFIDENCE = 0.15     # peak-to-second-peak margin below this => unreliable
SLOPE_EPS = 0.0005        # |slope| below this (s of lag per s of audio) is "constant"


@dataclass
class AlignmentResult:
    offset_s: float        # offset at the START of the audio track (t=0), video minus audio
    slope: float            # additional seconds of lag per second of audio (speed change)
    confidence: float       # 0..1, lower of the coarse and the windowed fit
    kind: str                # "constant" | "slope" | "low_confidence"
    windows: list[tuple[float, float, float]] = field(default_factory=list)  # (audio_t_s, lag_s, confidence)


def onset_envelope(y: np.ndarray, sr: int = TARGET_SR, hop_length: int = HOP_LENGTH) -> np.ndarray:
    """Gain-invariant onset-strength envelope, z-scored.

    Not the raw waveform: two releases of the same song differ in codec,
    loudness and mastering, which a waveform cross-correlation would pick up
    as noise. Onset strength (spectral flux) is far less sensitive to a
    constant gain difference than the raw signal, and z-scoring removes what
    is left of it.
    """
    import librosa
    env = librosa.onset.onset_strength(y=y.astype(np.float32), sr=sr, hop_length=hop_length)
    std = env.std()
    if std < 1e-9:
        return env - env.mean()
    return (env - env.mean()) / std


def _xcorr_lag(a: np.ndarray, b: np.ndarray, max_lag: int) -> tuple[int, float]:
    """Best integer lag (frames) such that ``a[n] ~= b[n + lag]`` over the overlap.

    A positive lag means b's matching content sits AHEAD of a's at the same
    index -- b needs to be read ``lag`` frames further in to match a, i.e. b
    runs later than a (used with a=audio, b=video: lag > 0 means the video is
    behind / has a longer intro than the audio).

    Returns (lag, confidence); confidence is the normalised margin of the
    correlation peak over the next-best peak found at least 5 frames away
    (a flat/noisy correlation surface means the match is not trustworthy).
    """
    from scipy.signal import correlate
    full = correlate(a, b, mode="full", method="fft")
    # correlate(a, b, 'full')[k] corresponds to shifting b by (k - (len(b)-1))
    # samples relative to a: index i -> raw_lag = i - (len(b) - 1), and
    # full[i] = sum_n a[n] * b[n - raw_lag] = sum_n a[n] * b[n + lag] with
    # lag = -raw_lag. See tests/test_av_align.py for the empirical pin.
    raw_lags = np.arange(len(full)) - (len(b) - 1)
    lags = -raw_lags
    mask = (lags >= -max_lag) & (lags <= max_lag)
    full = full[mask]
    lags = lags[mask]
    if len(full) == 0:
        return 0, 0.0
    peak_idx = int(np.argmax(full))
    peak_val = float(full[peak_idx])
    lo, hi = max(0, peak_idx - 5), min(len(full), peak_idx + 6)
    rest = np.delete(full, np.arange(lo, hi))
    second = float(np.max(rest)) if len(rest) else 0.0
    denom = abs(peak_val) + 1e-9
    confidence = max(0.0, (peak_val - second) / denom)
    return int(lags[peak_idx]), float(confidence)


def estimate_offset(audio_y: np.ndarray, video_y: np.ndarray,
                    sr: int = TARGET_SR) -> Optional[AlignmentResult]:
    """Estimate the video-minus-audio offset from decoded mono waveforms.

    Returns None only when the inputs are too short to say anything; a
    genuinely unreliable match comes back as ``kind == "low_confidence"``
    (confidence set, offset left at 0) rather than None, so the caller sees
    why it was rejected.
    """
    if len(audio_y) < sr or len(video_y) < sr:
        return None

    a_env = onset_envelope(audio_y, sr)
    v_env = onset_envelope(video_y, sr)
    frame_s = HOP_LENGTH / sr

    coarse_max_lag = int(COARSE_SEARCH_S / frame_s)
    best: tuple[int, float] = (0, -1.0)
    for prefix_s in COARSE_PREFIX_CANDIDATES_S:
        prefix_frames = int(prefix_s / frame_s)
        cand_lag, cand_conf = _xcorr_lag(a_env[:prefix_frames], v_env, coarse_max_lag)
        if cand_conf > best[1]:
            best = (cand_lag, cand_conf)
    coarse_lag_frames, coarse_conf = best
    coarse_offset_s = coarse_lag_frames * frame_s

    if coarse_conf < MIN_CONFIDENCE:
        return AlignmentResult(0.0, 0.0, coarse_conf, "low_confidence")

    # Per-window lag estimate in a narrow band around the coarse lag, so a
    # speed change (a linear drift away from the coarse lag) still resolves
    # without widening the search to the whole track.
    window_frames = int(WINDOW_S / frame_s)
    band_frames = max(1, int(NARROW_BAND_S / frame_s))
    windows: list[tuple[float, float, float]] = []
    n = len(a_env)
    for start in range(0, max(1, n - window_frames), window_frames):
        seg = a_env[start:start + window_frames]
        if len(seg) < window_frames // 2:
            continue
        center = start + coarse_lag_frames
        lo = max(0, center - band_frames)
        hi = min(len(v_env), center + window_frames + band_frames)
        if hi - lo < window_frames // 2:
            continue
        vseg = v_env[lo:hi]
        local_lag, local_conf = _xcorr_lag(seg, vseg, band_frames)
        # local_lag is relative to vseg starting at `lo`; the absolute lag
        # (video frame - audio frame) is how far `lo` already sits from
        # `start`, plus that local correction.
        abs_lag_frames = (lo - start) + local_lag
        windows.append((start * frame_s, abs_lag_frames * frame_s, local_conf))

    if not windows:
        return AlignmentResult(coarse_offset_s, 0.0, coarse_conf, "constant")

    ts = np.array([w[0] for w in windows])
    lags = np.array([w[1] for w in windows])
    confs = np.array([w[2] for w in windows])
    weights = np.clip(confs, 1e-6, None)

    if len(ts) >= 2 and weights.sum() > 0:
        sw = np.sqrt(weights)
        design = np.vstack([ts, np.ones_like(ts)]).T * sw[:, None]
        target = lags * sw
        sol, *_ = np.linalg.lstsq(design, target, rcond=None)
        slope, intercept = float(sol[0]), float(sol[1])
    else:
        slope, intercept = 0.0, coarse_offset_s

    overall_conf = float(min(coarse_conf, float(np.average(confs, weights=weights))))
    kind = "slope" if abs(slope) > SLOPE_EPS else "constant"
    if overall_conf < MIN_CONFIDENCE:
        kind = "low_confidence"

    return AlignmentResult(offset_s=intercept, slope=slope, confidence=overall_conf,
                           kind=kind, windows=windows)
