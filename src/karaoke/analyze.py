"""Local audio key + tempo (BPM) analysis.

Heavy DSP stacks (essentia, librosa) are optional and imported lazily/guarded, so
the main package stays light and the analyzer degrades gracefully to
``key="unknown"`` when they aren't installed (see the music-audio-analysis
skill). Install with ``make install-audio`` (requirements-audio.txt).

Key detection follows the skill's vetted approach: Essentia ``KeyExtractor`` with
``profileType="edma"``, voted across multiple windows with a seeded RNG, because
a single window (intro/bridge) is a coin flip. Confidence is reported honestly.
"""
from __future__ import annotations

import os
import random
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

from .musictheory import Key, parse_key

# Bump when the algorithm changes so a cache of stale answers is recomputed.
ANALYZER_VERSION = 1

# Formats essentia's bundled loader handles directly; anything else (webm/opus,
# m4a, etc.) is transcoded to WAV with system ffmpeg first.
_NATIVE_EXTS = {".wav", ".flac", ".ogg", ".mp3", ".aiff", ".aif"}


@contextmanager
def _as_wav(audio_path: str, sample_rate: int = 44100) -> Iterator[Optional[str]]:
    """Yield a path essentia/librosa can decode, transcoding via ffmpeg if needed.

    essentia's bundled AudioLoader rejects webm/opus with "Unsupported codec!".
    We transcode unsupported containers to a temporary mono WAV using system
    ffmpeg (a documented system dependency). Yields None when the input is
    missing or transcoding fails.
    """
    if not os.path.isfile(audio_path):
        yield None
        return
    ext = os.path.splitext(audio_path)[1].lower()
    if ext in _NATIVE_EXTS:
        yield audio_path
        return
    if not shutil.which("ffmpeg"):
        # No transcoder: essentia may still handle a few extra formats.
        yield audio_path
        return
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    try:
        proc = subprocess.run(
            ["ffmpeg", "-y", "-i", audio_path, "-ac", "1", "-ar",
             str(sample_rate), "-vn", tmp.name],
            capture_output=True, timeout=180,
        )
        if proc.returncode != 0 or os.path.getsize(tmp.name) == 0:
            yield None
        else:
            yield tmp.name
    except (subprocess.SubprocessError, OSError):
        yield None
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


@dataclass(frozen=True)
class KeyResult:
    """Detected key with honest confidence + provenance."""

    key: Optional[Key]
    confidence: float          # winning share (0..1); < 0.5 is ambiguous
    agreement: str             # e.g. "4/6" windows agreeing
    full_track: Optional[Key]  # the full-track read (often wrong on its own)
    method: str                # "essentia-edma-vote" | "unavailable"

    @property
    def ambiguous(self) -> bool:
        """True when confidence is low enough to treat as a coin flip."""
        return self.key is None or self.confidence < 0.5


@dataclass(frozen=True)
class AudioAnalysis:
    """Combined key + tempo analysis for one audio file."""

    key: Optional[Key]
    key_confidence: float
    key_agreement: str
    bpm: Optional[float]
    method: str
    version: int = ANALYZER_VERSION


def detect_bpm(audio_path: str) -> Optional[float]:
    """Estimate tempo (BPM) via librosa, or None if unavailable/unreadable."""
    try:
        import librosa
    except Exception:
        return None
    with _as_wav(audio_path) as path:
        if path is None:
            return None
        try:
            y, sr = librosa.load(path, mono=True)
            tempo, _ = librosa.beat.beat_track(y=y, sr=sr, units="frames")
            bpm = float(tempo) if not hasattr(tempo, "__len__") else float(tempo[0])
            return round(bpm, 1) if bpm > 0 else None
        except Exception:
            return None


def _essentia_key(audio, es, sample_rate: int) -> Optional[tuple[Key, float]]:
    """Run Essentia KeyExtractor(edma) on an audio buffer -> (Key, strength)."""
    try:
        key_str, scale, strength = es.KeyExtractor(
            profileType="edma", sampleRate=sample_rate
        )(audio)
    except Exception:
        return None
    parsed = parse_key(f"{key_str} {scale}")
    if parsed is None:
        return None
    return parsed, float(strength)


def detect_key(audio_path: str, *, sample_rate: int = 44100,
               windows: int = 6, seed: int = 42) -> KeyResult:
    """Detect musical key via Essentia edma, voted across windows.

    Transcodes unsupported containers (webm/opus) to WAV first, then analyzes
    the full track plus several windows (intro, ~35%, ~70%, seeded random
    slices), voting by summed strength with the full track weighted ~1.5x but
    not enough to override a consistent window majority.
    """
    with _as_wav(audio_path, sample_rate) as path:
        if path is None:
            return KeyResult(None, 0.0, "0/0", None, "unavailable")
        return _detect_key_wav(path, sample_rate=sample_rate,
                               windows=windows, seed=seed)


def _detect_key_wav(audio_path: str, *, sample_rate: int, windows: int,
                    seed: int) -> KeyResult:
    """Key detection on a directly-decodable (WAV/native) file."""
    try:
        import essentia
        import essentia.standard as es
    except Exception:
        return KeyResult(None, 0.0, "0/0", None, "unavailable")

    essentia.log.infoActive = False
    essentia.log.warningActive = False

    try:
        audio = es.MonoLoader(filename=audio_path, sampleRate=sample_rate)()
    except Exception:
        return KeyResult(None, 0.0, "0/0", None, "unavailable")

    n = len(audio)
    if n < sample_rate * 5:  # too short to be meaningful
        full = _essentia_key(audio, es, sample_rate)
        if full is None:
            return KeyResult(None, 0.0, "0/0", None, "essentia-edma-vote")
        return KeyResult(full[0], 1.0, "1/1", full[0], "essentia-edma-vote")

    win_len = max(sample_rate * 20, n // 4)
    rng = random.Random(seed)
    starts = [0, int(n * 0.35), int(n * 0.70)]
    for _ in range(max(0, windows - len(starts))):
        starts.append(rng.randint(0, max(0, n - win_len)))

    votes: dict[Key, float] = {}
    win_keys: list[Key] = []
    full_res = _essentia_key(audio, es, sample_rate)

    for start in starts:
        seg = audio[start:start + win_len]
        if len(seg) < sample_rate * 5:
            continue
        res = _essentia_key(seg, es, sample_rate)
        if res is None:
            continue
        win_keys.append(res[0])
        votes[res[0]] = votes.get(res[0], 0.0) + res[1]

    total = len(win_keys)
    if not votes:
        # No usable windows: fall back to the full-track read alone.
        if full_res is None:
            return KeyResult(None, 0.0, "0/0", None, "essentia-edma-vote")
        return KeyResult(full_res[0], round(full_res[1], 3), "0/0",
                         full_res[0], "essentia-edma-vote")

    # Windows are authoritative: the windowed-strength leader wins. The
    # full-track read is NOT added to the tally — on the benchmark it is wrong
    # on ~half the tracks and full-track alone scores only 2/4, so it must never
    # override a window consensus. It is kept only for reporting / the no-window
    # fallback above.
    winner = max(votes, key=lambda k: votes[k])

    agree = sum(1 for k in win_keys if k == winner)
    total_strength = sum(votes.values())
    confidence = votes[winner] / total_strength if total_strength else 0.0
    full_key = full_res[0] if full_res else None
    return KeyResult(winner, round(confidence, 3), f"{agree}/{total}",
                     full_key, "essentia-edma-vote")


def analyze_audio(audio_path: str) -> AudioAnalysis:
    """Full local analysis: key (voted) + tempo. Degrades gracefully.

    Transcodes once to WAV (when needed) and reuses it for both key and tempo,
    so webm/opus downloads analyze without a per-call ffmpeg round-trip.
    """
    with _as_wav(audio_path) as path:
        if path is None:
            return AudioAnalysis(None, 0.0, "0/0", None, "unavailable")
        kr = _detect_key_wav(path, sample_rate=44100, windows=6, seed=42)
        bpm = detect_bpm(path)
    return AudioAnalysis(
        key=kr.key,
        key_confidence=kr.confidence,
        key_agreement=kr.agreement,
        bpm=bpm,
        method=kr.method,
    )
