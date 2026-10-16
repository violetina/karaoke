"""Mood over time: where a song sits, moment to moment, and where it swings.

One label per song hides what a song does. A quiet verse and a furious
chorus, a ballad that turns into hard rock, a slow instrumental crescendo --
the movement is the point, and a single "sad" or "angry" describes none of it.
This places each ten-second stretch of a track on the valence x arousal plane
of :mod:`karaoke.mood_match` and reads the moods, the swings between them, and
the stretches that sit too close to a boundary to call.

**Two axes, two sources, chosen by measurement.** Against a reference set of
fifteen songs with uncontroversial moods:

- *Arousal* (calm <-> intense) comes from the sound. CLAP, scored against mood
  prompts, put 13 of 15 songs on the right side -- angry/happy above,
  sad/tender below.
- *Valence* (dark <-> warm) does not. Soft acoustic music reads "tender" to
  CLAP whether it is a love song or about a dead child. The lyrics' *meaning*
  -- their sentence embedding against tone prompts -- tells those apart; a
  word-count lexicon does not ("Tears in Heaven" scored positive on "heaven",
  "love" and "stay"). So valence is mostly the words, where there are words.
  The detected key's mode was tried as a valence hint and made results worse:
  "Tears in Heaven" is in A major.

Both zero-shot scores are **calibrated** against the library itself. Raw,
CLAP called almost everything "happy" -- that prompt sits nearest to music in
general -- and every sad and tender song in the set came out happy. Each score
is z-scored against its own spread over thousands of library tracks, so a mood
has to beat its own baseline.

Track-level accuracy on the reference set: raw CLAP 6/15, calibrated 7/15,
plus lexicon valence 9/15, plus lyric meaning 11/15. The misses are mostly
songs whose label is itself debatable, and they come out flagged borderline.

Instrumentals have only the sound, so valence there is CLAP's alone and less
certain -- which the borderline flags then say.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from .logger import log

MOOD_VERSION = 1

#: Each window is one CLAP forward pass, CLAP's own input length.
WINDOW_S = 10.0

#: Several phrasings per mood, averaged: one sentence is one opinion.
AUDIO_PROMPTS: dict[str, tuple[str, ...]] = {
    "happy": ("happy upbeat cheerful music", "joyful feel-good song",
              "bright playful party music"),
    "sad": ("sad melancholic music", "sorrowful mournful slow song",
            "heartbroken lonely music"),
    "angry": ("angry aggressive intense music", "furious loud distorted rock",
              "violent heavy music"),
    "tender": ("tender gentle romantic music", "soft warm loving ballad",
               "calm intimate love song"),
}
AUDIO_MOODS = tuple(AUDIO_PROMPTS)

#: Lyric tones (tone.TONES) and the valence each carries.
TONE_VALENCE: dict[str, float] = {
    "joyful and celebratory": 1.0,
    "tender and romantic": 1.0,
    "sad and mournful": -1.0,
    "angry and confrontational": -1.0,
    "cynical and sarcastic": -0.5,
    "defiant and rebellious": -0.3,
}

#: How much the words decide valence when there are words. Swept on the
#: reference set: 0.8 to 1.0 all gave the best result; the sound keeps a
#: small say for passages where the words are thin.
WORDS_WEIGHT = 0.85

#: Within-song loudness adds to arousal, so a quiet verse and a loud chorus
#: separate even when CLAP hears the same instruments in both. Measured in dB
#: against the song's median, LOUDNESS_DB to a unit: z-scoring it per song
#: instead blew a steady song's 1 dB wobble up to a full unit, and "Pale Blue
#: Eyes" swung eight times.
LOUDNESS_WEIGHT = 0.3
LOUDNESS_DB = 6.0

#: Closer than this to either axis and the stretch is borderline: reported
#: with the mood across that axis too, not forced into one.
BORDER = 0.15

#: A mood must hold this many windows (30 s) to count as a swing; shorter runs are
#: absorbed by their neighbours.
MIN_SEGMENT_WINDOWS = 3

#: Less text than this in a window says nothing about its meaning.
MIN_TEXT_CHARS = 40

#: How much a window's own words count against the song's overall meaning.
#: Ten seconds of lyric is a fragment: at an even split, fragments of sad
#: songs read neutral or warm and "The Scientist" came out tender.
WINDOW_WORDS_SHARE = 0.25

QUADRANT = {(True, True): "happy", (False, True): "angry",
            (False, False): "sad", (True, False): "tender"}


# -- calibration ---------------------------------------------------------------

def _calibration_path() -> Path:
    from .config import settings

    return Path(settings.data_dir) / "mood_calibration.json"


def _unit(rows):
    import numpy as np

    rows = np.asarray(rows, dtype=float)
    norms = np.linalg.norm(rows, axis=-1, keepdims=True)
    norms[norms == 0] = 1.0
    return rows / norms


def _audio_prompt_matrix():
    """Unit text vectors, one per mood, each the mean of its phrasings."""
    import numpy as np

    from . import clap_vector

    rows = []
    for mood in AUDIO_MOODS:
        vecs = [clap_vector.embed_text(p) for p in AUDIO_PROMPTS[mood]]
        rows.append(np.mean([v for v in vecs if v], axis=0))
    return _unit(rows)


def _tone_matrix():
    from .embed import embed_text

    return _unit([embed_text(f"lyrics that are {tone}") for tone in TONE_VALENCE])


def build_calibration(*, clap_sample: int = 4000, lyric_sample: int = 300) -> dict[str, Any]:
    """Measure each prompt's baseline over the library, and store it.

    Run once (and again if the prompts change): the stored means and spreads
    are what make a score mean "more than this mood usually scores".
    """
    import numpy as np
    from opensearchpy import helpers

    from . import clap_vector, localcache
    from .embed import embed_text
    from .osclient import client

    vectors = [h["_source"]["clap_vector"] for _, h in zip(range(clap_sample), helpers.scan(
        client(), index=clap_vector.CLAP_INDEX, preserve_order=True,
        query={"_source": ["clap_vector"],
               "query": {"function_score": {"random_score": {"seed": 7}}}}))]
    audio = _unit(vectors) @ _audio_prompt_matrix().T

    with localcache.connect() as conn:
        rows = conn.execute(
            "SELECT synced_lyrics, plain_lyrics FROM lyrics WHERE kind = 'approved'"
            " AND synced_lyrics IS NOT NULL ORDER BY md5(track_id::text) LIMIT %s",
            (lyric_sample,)).fetchall()
    texts = [t for t in (_lyrics_text(r) for r in rows) if len(t) >= 80]
    tones = _unit([embed_text(t[:2000]) for t in texts]) @ _tone_matrix().T

    cal = {
        "version": MOOD_VERSION,
        "built_at": time.time(),
        "audio_mean": audio.mean(axis=0).tolist(), "audio_std": audio.std(axis=0).tolist(),
        "tone_mean": tones.mean(axis=0).tolist(), "tone_std": tones.std(axis=0).tolist(),
        "samples": {"audio": len(vectors), "lyrics": len(texts)},
    }
    path = _calibration_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cal))
    return cal


_calibration: Optional[dict[str, Any]] = None


def calibration() -> dict[str, Any]:
    """The stored calibration, building it on first use."""
    global _calibration
    if _calibration is None:
        path = _calibration_path()
        if path.is_file():
            loaded = json.loads(path.read_text())
            if loaded.get("version") == MOOD_VERSION:
                _calibration = loaded
        if _calibration is None:
            _calibration = build_calibration()
    return _calibration


# -- the pure part: scores in, timeline out -----------------------------------

@dataclass
class Segment:
    start_s: float
    end_s: float
    mood: str
    valence: float
    arousal: float
    #: The mood across the nearer boundary, when the stretch is borderline.
    alt: Optional[str] = None

    @property
    def label(self) -> str:
        return f"{self.mood}↔{self.alt}" if self.alt else self.mood


@dataclass
class MoodTimeline:
    segments: list[Segment]
    duration_s: float
    has_lyrics: bool
    summary: dict[str, Any] = field(default_factory=dict)

    def at(self, seconds: float) -> Optional[Segment]:
        for seg in self.segments:
            if seg.start_s <= seconds < seg.end_s:
                return seg
        return None

    def as_rows(self) -> list[dict[str, Any]]:
        return [{**asdict(s), "valence": round(s.valence, 3),
                 "arousal": round(s.arousal, 3)} for s in self.segments]


def classify(valence: float, arousal: float) -> tuple[str, Optional[str]]:
    """Quadrant of the plane, plus the neighbour when too close to call."""
    import math

    a = math.tanh(arousal)
    mood = QUADRANT[(valence > 0, a > 0)]
    if min(abs(valence), abs(a)) >= BORDER:
        return mood, None
    # Across whichever axis is nearer.
    if abs(valence) <= abs(a):
        return mood, QUADRANT[(not valence > 0, a > 0)]
    return mood, QUADRANT[(valence > 0, not a > 0)]


def _smooth(values, width: int = 3):
    """Centred moving average; the ends average what they have."""
    import numpy as np

    values = np.asarray(values, dtype=float)
    if len(values) < width:
        return values
    half = width // 2
    return np.array([values[max(0, i - half):i + half + 1].mean()
                     for i in range(len(values))])


def build(valence, arousal, *, window_s: float = WINDOW_S,
          duration_s: Optional[float] = None, has_lyrics: bool = False) -> MoodTimeline:
    """Smoothed per-window valence/arousal -> segments, swings and borderliners."""
    v, a = _smooth(valence), _smooth(arousal)
    n = len(v)
    duration = duration_s if duration_s is not None else n * window_s
    # Hysteresis: a window that is only borderline with the mood already
    # running keeps it. A swing has to clear the boundary, not touch it.
    labels: list[str] = []
    for i in range(n):
        mood, alt = classify(float(v[i]), float(a[i]))
        if labels and alt == labels[-1]:
            mood = labels[-1]
        labels.append(mood)

    # Runs shorter than MIN_SEGMENT_WINDOWS are a blip, not a swing: give them
    # to the longer neighbour.
    changed = True
    while changed and n:
        changed = False
        runs = _runs(labels)
        for k, (lab, s, e) in enumerate(runs):
            if e - s >= MIN_SEGMENT_WINDOWS or len(runs) == 1:
                continue
            before = runs[k - 1] if k else None
            after = runs[k + 1] if k + 1 < len(runs) else None
            take = max((r for r in (before, after) if r), key=lambda r: r[2] - r[1])
            for i in range(s, e):
                labels[i] = take[0]
            changed = True
            break

    segments = []
    for lab, s, e in _runs(labels):
        sv, sa = float(v[s:e].mean()), float(a[s:e].mean())
        _, alt = classify(sv, sa)
        if alt == lab:
            alt = None
        segments.append(Segment(start_s=s * window_s, end_s=min(e * window_s, duration),
                                mood=lab, valence=sv, arousal=sa, alt=alt))
    return MoodTimeline(segments=segments, duration_s=duration, has_lyrics=has_lyrics,
                        summary=summarise(segments, duration))


def _runs(labels: list[str]) -> list[tuple[str, int, int]]:
    out: list[tuple[str, int, int]] = []
    for i, lab in enumerate(labels):
        if out and out[-1][0] == lab:
            out[-1] = (lab, out[-1][1], i + 1)
        else:
            out.append((lab, i, i + 1))
    return out


def summarise(segments: list[Segment], duration_s: float) -> dict[str, Any]:
    """Dominant mood by time, the swings, and how much is borderline."""
    if not segments:
        return {}
    time_in: dict[str, float] = {}
    border = 0.0
    for seg in segments:
        span = seg.end_s - seg.start_s
        time_in[seg.mood] = time_in.get(seg.mood, 0.0) + span
        if seg.alt:
            border += span
    swings = [{"at_s": round(b.start_s, 1), "from": a.mood, "to": b.mood,
               "jump": round(((b.valence - a.valence) ** 2 + (b.arousal - a.arousal) ** 2) ** 0.5, 3)}
              for a, b in zip(segments, segments[1:])]
    minutes = duration_s / 60.0 if duration_s else 0.0
    dominant = max(time_in, key=time_in.get)
    return {
        "dominant": dominant,
        "arc": " → ".join(s.mood for s in segments),
        "swings": swings,
        "swing_count": len(swings),
        "swings_per_minute": round(len(swings) / minutes, 2) if minutes else 0.0,
        "borderline_share": round(border / duration_s, 3) if duration_s else 0.0,
        "shares": {m: round(t / duration_s, 3) for m, t in time_in.items()} if duration_s else {},
    }


# -- the audio/text part -------------------------------------------------------

def _lyrics_text(row) -> str:
    from .lyrics import parse_lrc

    if row is None:
        return ""
    synced = row.get("synced_lyrics") or ""
    text = "\n".join(t for _, t in parse_lrc(synced)) if synced else ""
    return text or (row.get("plain_lyrics") or "")


def _audio_scores(audio_path: str):
    """Per-window calibrated CLAP mood z-scores and loudness."""
    import librosa
    import numpy as np
    import torch

    from . import clap_vector
    from .analyze import _as_wav

    model, processor = clap_vector._load()
    try:
        with _as_wav(audio_path, clap_vector.SAMPLE_RATE) as wav:
            if not wav:
                return None
            y, _sr = librosa.load(wav, sr=clap_vector.SAMPLE_RATE, mono=True)
    except Exception as exc:
        # A corrupt file (libsndfile "Unspecified internal error") is no
        # audio, not a crash: the library backfill died on one after 415 songs.
        log.warning("mood timeline: cannot decode %s: %s", audio_path, exc)
        return None
    span = int(clap_vector.SAMPLE_RATE * WINDOW_S)
    windows = [y[i:i + span] for i in range(0, len(y), span)]
    windows = [w for w in windows if len(w) >= clap_vector.SAMPLE_RATE * 3]
    if not windows:
        return None
    feats = []
    with torch.no_grad():
        for k in range(0, len(windows), 8):
            inputs = processor(audio=windows[k:k + 8], sampling_rate=clap_vector.SAMPLE_RATE,
                               return_tensors="pt")
            feats.append(clap_vector._unwrap(model.get_audio_features(**inputs)).numpy())
    cal = calibration()
    sims = _unit(np.concatenate(feats)) @ _audio_prompt_matrix().T
    z = (sims - np.array(cal["audio_mean"])) / np.array(cal["audio_std"])
    rms = np.array([float(np.sqrt(np.mean(w ** 2))) for w in windows])
    return z, rms, len(y) / clap_vector.SAMPLE_RATE


def _meaning_valence(texts: list[str]):
    """Calibrated lyric-meaning valence per text; None where too little text."""
    import numpy as np

    from .embed import embed_text

    cal = calibration()
    weights = np.array(list(TONE_VALENCE.values()))
    tones = _tone_matrix()
    out: list[Optional[float]] = []
    for text in texts:
        if len(text.strip()) < MIN_TEXT_CHARS:
            out.append(None)
            continue
        z = ((_unit(embed_text(text[:2000])) @ tones.T) - np.array(cal["tone_mean"])) \
            / np.array(cal["tone_std"])
        out.append(float(np.tanh((z * weights).sum() / 2)))
    return out


def analyse(audio_path: str, lyric_lines: list[tuple[float, str]],
            plain_lyrics: str = "") -> Optional[MoodTimeline]:
    """A track's mood timeline from its audio and (timed) lyrics."""
    import numpy as np

    scored = _audio_scores(audio_path)
    if scored is None:
        return None
    z, rms, duration = scored
    d = {m: z[:, i] for i, m in enumerate(AUDIO_MOODS)}
    db = 20 * np.log10(np.maximum(rms, 1e-6))
    loud = np.clip((db - np.median(db)) / LOUDNESS_DB, -1.5, 1.5)
    arousal = (d["angry"] + d["happy"] - d["sad"] - d["tender"]) / 2 + LOUDNESS_WEIGHT * loud
    audio_v = np.tanh((d["happy"] + d["tender"] - d["sad"] - d["angry"]) / 2)

    whole = "\n".join(t for _, t in lyric_lines) or plain_lyrics
    song_v = _meaning_valence([whole])[0] if whole else None
    if song_v is None:
        valence = audio_v
    else:
        # Each window: its own words where it has enough, else the song's.
        texts = []
        for i in range(len(z)):
            lo, hi = i * WINDOW_S - 5.0, (i + 1) * WINDOW_S + 5.0
            texts.append("\n".join(t for at, t in lyric_lines if lo <= at < hi))
        seg_v = _meaning_valence(texts) if lyric_lines else [None] * len(z)
        words = np.array([song_v if sv is None
                          else WINDOW_WORDS_SHARE * sv + (1 - WINDOW_WORDS_SHARE) * song_v
                          for sv in seg_v])
        valence = WORDS_WEIGHT * words + (1 - WORDS_WEIGHT) * audio_v
    return build(valence, arousal, duration_s=duration, has_lyrics=song_v is not None)


# -- storage -------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS track_moods (
    track_id     INTEGER PRIMARY KEY,
    mood_version INTEGER NOT NULL,
    duration_s   REAL,
    segments     JSONB NOT NULL,
    summary      JSONB NOT NULL DEFAULT '{}'::jsonb,
    has_lyrics   BOOLEAN NOT NULL DEFAULT FALSE,
    source_path  TEXT NOT NULL DEFAULT '',
    updated_at   DOUBLE PRECISION NOT NULL
)
"""


_schema_ready = False


def ensure_schema(conn) -> None:
    """Create the table once per process.

    needs_postprocessing asks has_current() for every track it checks, and
    re-running the DDL each time took a table lock per call -- the test suite
    went from 2.5 to 9 minutes before this was cached.
    """
    global _schema_ready
    if _schema_ready:
        return
    conn.execute(_SCHEMA)
    conn.commit()
    _schema_ready = True


def save(track_id: int, timeline: MoodTimeline, conn, *, source_path: str = "") -> None:
    from psycopg.types.json import Jsonb

    ensure_schema(conn)
    conn.execute(
        """
        INSERT INTO track_moods (track_id, mood_version, duration_s, segments, summary,
                                 has_lyrics, source_path, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (track_id) DO UPDATE SET
            mood_version = excluded.mood_version, duration_s = excluded.duration_s,
            segments = excluded.segments, summary = excluded.summary,
            has_lyrics = excluded.has_lyrics, source_path = excluded.source_path,
            updated_at = excluded.updated_at
        """,
        (track_id, MOOD_VERSION, round(timeline.duration_s, 2), Jsonb(timeline.as_rows()),
         Jsonb(timeline.summary), timeline.has_lyrics, source_path, time.time()))
    conn.commit()


def load(track_id: int, conn) -> Optional[dict[str, Any]]:
    ensure_schema(conn)
    row = conn.execute("SELECT mood_version, duration_s, segments, summary, has_lyrics"
                       " FROM track_moods WHERE track_id = %s", (track_id,)).fetchone()
    return dict(row) if row else None


def has_current(track_id: int, conn) -> bool:
    ensure_schema(conn)
    row = conn.execute("SELECT 1 FROM track_moods WHERE track_id = %s AND mood_version = %s",
                       (track_id, MOOD_VERSION)).fetchone()
    return row is not None


def at(stored: dict[str, Any], seconds: float) -> Optional[dict[str, Any]]:
    """The stored segment sounding at a position."""
    for seg in stored.get("segments") or []:
        if seg["start_s"] <= seconds < seg["end_s"]:
            return seg
    return None


def analyse_track(track_id: int, conn, audio_path: Optional[Path] = None) -> Optional[MoodTimeline]:
    """Analyse and store a library track's timeline from audio on disk."""
    from . import chord_timeline
    from .lyrics import parse_lrc

    path = audio_path or chord_timeline.local_audio(track_id, conn)
    if path is None:
        return None
    row = conn.execute("SELECT synced_lyrics, plain_lyrics, source FROM lyrics"
                       " WHERE track_id = %s AND kind = 'approved'", (track_id,)).fetchone()
    lines: list[tuple[float, str]] = []
    plain = ""
    if row:
        from .librarysearch import is_transcribed

        # A Whisper transcription is a guess at the words; its meaning is
        # the model's, not the song's (the same rule tone.py applies).
        if not is_transcribed(row.get("source") or ""):
            lines = [(t, x) for t, x in parse_lrc(row["synced_lyrics"] or "") if x.strip()]
            plain = row["plain_lyrics"] or ""
    timeline = analyse(str(path), lines, plain)
    if timeline is not None:
        save(track_id, timeline, conn, source_path=str(path))
    return timeline
