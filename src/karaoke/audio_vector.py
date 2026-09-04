"""Audio feature vectors, for "sounds like" search over recorded audio.

The lyric index answers "what is this song *about*". Nothing answers "what does
it *sound* like" — which is the question a recording is best placed to answer,
since for a Spotify track the recorded audio is the only copy that exists.

Rather than pull in an audio embedding network (CLAP, VGGish, PANNs) and a torch
dependency with it, the vector is assembled from features librosa already
computes during analysis:

- **MFCC** mean and standard deviation (40 dims) — timbre: the overall colour of
  the sound, and how much it varies across the track.
- **Chroma** mean (12 dims) — the pitch-class profile, i.e. the harmonic
  fingerprint. This is what makes two recordings in the same key with similar
  progressions land near each other.
- **Spectral contrast** mean (7 dims) — the peak-to-valley structure of the
  spectrum, which separates dense/distorted material from sparse/clean.
- Three normalised scalars — energy, brightness, tempo.

62 dimensions in total. Small enough to store per segment without thought, and
grounded in features that are already being extracted, so the audio is decoded
once for everything.

Each family is normalised *separately* before being concatenated (see
:func:`_balance`) — without that step the raw MFCC magnitudes swamp everything
else and the result is a timbre-only vector wearing a harmony label. The
assembled vector is unit length, so cosine similarity is a plain dot product and
loudness differences between recordings do not dominate: two takes of the same
song at different volumes should still be neighbours.

Requires librosa, which lives in the isolated audio venv (``make install-audio``).
Every entry point returns None rather than raising when it is unavailable, the
same contract as :mod:`karaoke.analyze`.
"""
from __future__ import annotations

from typing import Optional

from .logger import log

# 20 MFCC mean + 20 MFCC std + 12 chroma + 7 contrast + 3 scalars.
N_MFCC = 20
N_CHROMA = 12
N_CONTRAST = 7
AUDIO_VECTOR_DIM = (N_MFCC * 2) + N_CHROMA + N_CONTRAST + 3   # 62

# Tempo is normalised against this so it shares a scale with the other
# components. Above it, material is rare enough that clamping costs nothing.
MAX_BPM = 220.0


def _l2_normalise(values: list[float]) -> list[float]:
    """Scale to unit length, so cosine similarity is a dot product.

    Also removes overall loudness as a factor: two recordings of the same music
    at different volumes should be neighbours, not distant points.
    """
    total = sum(v * v for v in values) ** 0.5
    if total <= 0.0:
        return values
    return [v / total for v in values]


def _balance(blocks: list[list[float]]) -> list[float]:
    """Normalise each feature family separately, then concatenate.

    Without this the vector is MFCC and nothing else. Raw MFCC coefficients run
    to tens or hundreds while chroma and the scalars sit in 0..1, so a single
    normalisation over the concatenation leaves the musical components at
    ~0.002 — present in the arithmetic, absent from the result. Normalising per
    family first gives each an equal say, which is the whole point of combining
    timbre, harmony, texture and tempo rather than using timbre alone.

    The final scaling makes the assembled vector unit length, so
    :func:`similarity` stays a plain dot product.
    """
    scale = len(blocks) ** 0.5
    out: list[float] = []
    for block in blocks:
        out += [v / scale for v in _l2_normalise(block)]
    return out


def extract(audio_path: str, *, sample_rate: int = 22050) -> Optional[list[float]]:
    """Return the audio feature vector for a file, or None if unavailable.

    22.05kHz is deliberate: every feature here is spectral-envelope or
    pitch-class based, none of which needs the top octave, and halving the rate
    halves the decode cost on recordings that can run to hours.
    """
    try:
        import librosa
        import numpy as np
    except ImportError:
        log.debug("librosa unavailable; no audio vector")
        return None

    from .analyze import _as_wav

    try:
        with _as_wav(audio_path, sample_rate) as path:
            if not path:
                return None
            y, sr = librosa.load(path, mono=True, sr=sample_rate)
            if y is None or len(y) == 0:
                return None

            mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=N_MFCC)
            chroma = librosa.feature.chroma_stft(y=y, sr=sr)
            contrast = librosa.feature.spectral_contrast(y=y, sr=sr)
            rms = float(np.mean(librosa.feature.rms(y=y)))
            centroid = float(np.mean(librosa.feature.spectral_centroid(y=y, sr=sr)))
            tempo, _ = librosa.beat.beat_track(y=y, sr=sr, units="frames")
            # librosa 1.x returns tempo as an array; analyze.detect_features
            # unwraps it the same way.
            bpm = float(tempo) if not hasattr(tempo, "__len__") else float(tempo[0])

            # Kept as separate families so _balance can give each an equal
            # say; see there for why a single normalisation is not enough.
            blocks = [
                [float(v) for v in np.mean(mfcc, axis=1)],      # timbre
                [float(v) for v in np.std(mfcc, axis=1)],       # timbre variation
                [float(v) for v in np.mean(chroma, axis=1)],    # harmony
                [float(v) for v in np.mean(contrast, axis=1)],  # texture
                [
                    min(1.0, rms * 4.0),                        # energy
                    min(1.0, centroid / (sr / 2.0)),            # brightness
                    min(1.0, bpm / MAX_BPM),                    # tempo
                ],
            ]
            parts = _balance(blocks)
    except Exception:
        log.debug("audio vector extraction failed for %s", audio_path, exc_info=True)
        return None

    if len(parts) != AUDIO_VECTOR_DIM:
        # A librosa version returning a different feature shape would silently
        # produce vectors that cannot be compared with the stored ones.
        log.warning("audio vector has %d dims, expected %d; discarding",
                    len(parts), AUDIO_VECTOR_DIM)
        return None
    return parts


def similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two extracted vectors."""
    if len(a) != len(b):
        raise ValueError(f"dimension mismatch: {len(a)} vs {len(b)}")
    return sum(x * y for x, y in zip(a, b))


AUDIO_INDEX = "karaoke-audio"


def audio_doc_id(track_id: int, start_wall: float) -> str:
    """Stable id for one analysed stretch of audio.

    Keyed on the start time as well as the track: the same song recorded twice
    is two observations, and keeping both is the point of a recording index.
    """
    return f"audio:{track_id}:{int(start_wall)}"


def ensure_audio_index(os_client, index_name: str = AUDIO_INDEX) -> bool:
    """Create the audio vector index if absent. True if it was created."""
    if os_client.indices.exists(index=index_name):
        return False
    os_client.indices.create(index=index_name, body={
        "settings": {"index": {"knn": True, "number_of_replicas": 0}},
        "mappings": {
            "properties": {
                "track_id": {"type": "integer"},
                "recording_id": {"type": "integer"},
                "artist": {"type": "text", "fields": {"raw": {"type": "keyword"}}},
                "title": {"type": "text", "fields": {"raw": {"type": "keyword"}}},
                "recorded_at": {"type": "date"},
                "duration_s": {"type": "float"},
                "detected_key": {"type": "keyword"},
                "bpm": {"type": "float"},
                "source": {"type": "keyword"},
                "audio_vector": {
                    "type": "knn_vector",
                    "dimension": AUDIO_VECTOR_DIM,
                    "method": {
                        "name": "hnsw",
                        "space_type": "cosinesimil",
                        "engine": "lucene",
                    },
                },
            }
        },
    })
    return True


def build_audio_doc(*, track_id: int, recording_id: int, artist: str, title: str,
                    vector: list[float], recorded_at: str,
                    duration_s: float = 0.0, detected_key: str = "",
                    bpm: Optional[float] = None,
                    source: str = "recording") -> dict:
    """Assemble the OpenSearch document for one analysed segment."""
    return {
        "track_id": track_id,
        "recording_id": recording_id,
        "artist": artist,
        "title": title,
        "recorded_at": recorded_at,
        "duration_s": duration_s,
        "detected_key": detected_key,
        "bpm": bpm,
        "source": source,
        "audio_vector": vector,
    }
