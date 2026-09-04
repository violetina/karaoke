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

import json
import os
import random
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from .logger import log
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
    """Combined key + tempo + energy analysis for one audio file."""

    key: Optional[Key]
    key_confidence: float
    key_agreement: str
    bpm: Optional[float]
    method: str
    energy: Optional[float] = None      # RMS loudness, normalized 0..1
    brightness: Optional[float] = None  # spectral centroid, normalized 0..1
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


def detect_features(audio_path: str) -> dict[str, Optional[float]]:
    """Extract tempo + energy + brightness in a single librosa load.

    Returns a dict with ``bpm`` (float), ``energy`` (RMS loudness normalized to
    0..1) and ``brightness`` (spectral centroid normalized to 0..1 of Nyquist).
    Values are None when librosa is unavailable or the file can't be read. Used
    by ``analyze_audio`` so the WAV is decoded once for all librosa features.
    """
    out: dict[str, Optional[float]] = {"bpm": None, "energy": None, "brightness": None}
    try:
        import librosa
        import numpy as np
    except Exception:
        return out
    with _as_wav(audio_path) as path:
        if path is None:
            return out
        try:
            y, sr = librosa.load(path, mono=True)
        except Exception:
            return out
        try:
            tempo, _ = librosa.beat.beat_track(y=y, sr=sr, units="frames")
            bpm = float(tempo) if not hasattr(tempo, "__len__") else float(tempo[0])
            out["bpm"] = round(bpm, 1) if bpm > 0 else None
        except Exception:
            pass
        try:
            rms = float(np.mean(librosa.feature.rms(y=y)))
            # Map RMS (typically ~0..0.3 for music) to a friendly 0..1 scale.
            out["energy"] = round(min(1.0, rms / 0.25), 3)
        except Exception:
            pass
        try:
            cent = float(np.mean(librosa.feature.spectral_centroid(y=y, sr=sr)))
            out["brightness"] = round(min(1.0, cent / (sr / 2.0)), 3)
        except Exception:
            pass
    return out


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


def stack_available() -> bool:
    """Whether this interpreter can run the DSP stack itself."""
    try:
        import essentia  # noqa: F401
        import librosa   # noqa: F401
    except ImportError:
        return False
    return True


def audio_python() -> Optional[str]:
    """An interpreter that has the DSP stack, when this one does not.

    The heavy stack lives in an isolated venv on purpose (``make
    install-audio``), but analysis imports it in-process -- so every venv that
    wants to analyse has ended up needing a duplicate copy, and a worktree
    whose venv lacks it fails at the point of use with nothing to suggest why.
    Finding the audio venv and delegating to it is what makes the isolation
    actually work.

    ``KARAOKE_AUDIO_PYTHON`` overrides the search.
    """
    override = os.environ.get("KARAOKE_AUDIO_PYTHON")
    if override:
        return override if Path(override).is_file() else None
    # src/karaoke/analyze.py -> the checkout root, which is where the Makefile
    # puts .venv-audio. Worktrees each get their own, so this resolves per tree.
    root = Path(__file__).resolve().parents[2]
    candidate = root / ".venv-audio" / "bin" / "python"
    return str(candidate) if candidate.is_file() else None


def _analyze_out_of_process(audio_path: str, interpreter: str) -> Optional[AudioAnalysis]:
    """Run the analysis in the audio venv and bring the numbers back."""
    from .musictheory import parse_key

    try:
        proc = subprocess.run(
            [interpreter, "-m", "karaoke.analyze", "--json", audio_path],
            capture_output=True, text=True, timeout=600,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("out-of-process analysis failed: %s", exc)
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        log.debug("out-of-process analysis returned nothing: %s",
                  (proc.stderr or "")[-200:])
        return None
    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    return AudioAnalysis(
        key=parse_key(data["key"]) if data.get("key") else None,
        key_confidence=float(data.get("key_confidence") or 0.0),
        key_agreement=str(data.get("key_agreement") or "0/0"),
        bpm=data.get("bpm"),
        method=str(data.get("method") or "unavailable"),
        energy=data.get("energy"),
        brightness=data.get("brightness"),
        version=int(data.get("version") or 0),
    )


def analyze_audio(audio_path: str) -> AudioAnalysis:
    """Full local analysis: key (voted) + tempo + energy. Degrades gracefully.

    Transcodes once to WAV (when needed) and reuses it for the essentia key
    detection and all librosa features, so webm/opus downloads analyze without
    repeated ffmpeg round-trips.

    When this interpreter lacks the DSP stack, the work is handed to the audio
    venv rather than reported as unavailable -- see :func:`audio_python`.
    """
    if not stack_available():
        interpreter = audio_python()
        if interpreter:
            result = _analyze_out_of_process(audio_path, interpreter)
            if result is not None:
                return result

    with _as_wav(audio_path) as path:
        if path is None:
            return AudioAnalysis(None, 0.0, "0/0", None, "unavailable")
        kr = _detect_key_wav(path, sample_rate=44100, windows=6, seed=42)
        feats = detect_features(path)
    return AudioAnalysis(
        key=kr.key,
        key_confidence=kr.confidence,
        key_agreement=kr.agreement,
        bpm=feats.get("bpm"),
        method=kr.method,
        energy=feats.get("energy"),
        brightness=feats.get("brightness"),
    )


def _json_main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover
    """``python -m karaoke.analyze --json FILE`` -- the out-of-process entry.

    Prints one JSON object so the calling interpreter can rebuild the result
    without importing anything heavy.
    """
    import argparse

    ap = argparse.ArgumentParser(prog="karaoke.analyze")
    ap.add_argument("--json", dest="path", required=True)
    args = ap.parse_args(argv)

    result = analyze_audio(args.path)
    print(json.dumps({
        "key": result.key.name if result.key else None,
        "key_confidence": result.key_confidence,
        "key_agreement": result.key_agreement,
        "bpm": result.bpm,
        "method": result.method,
        "energy": result.energy,
        "brightness": result.brightness,
        "version": result.version,
    }))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_json_main())
