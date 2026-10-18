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

MOOD_VERSION = 2

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

#: The sound's *character*, scored like the moods (calibrated CLAP prompts).
#: Four quadrant moods could not say what Tina heard: "energetic",
#: "theatrical", "festive chaos" and -- twice, for Hendrix -- "the blues,
#: frustrated", all came out "angry". Probed on her 16 judged songs, the
#: character's top two contained her word 10 times (chance ~3); "bluesy" won
#: on both Hendrix blues, "energetic"/"dramatic" on the Primer build-up,
#: "dreamy" on Red Star. Weak: festive, theatrical, and sad-vs-warm without
#: words -- those still lean on lyrics and harmony.
CHARACTER_PROMPTS: dict[str, tuple[str, ...]] = {
    "aggressive": ("aggressive angry heavy distorted music", "furious violent screaming music"),
    "energetic": ("energetic driving upbeat music", "powerful pumping energetic beat"),
    "dramatic": ("dramatic theatrical cinematic music", "grand dramatic build-up"),
    "festive": ("festive chaotic party brass music", "wild celebration dance music"),
    "bluesy": ("bluesy frustrated crying blues guitar", "slow aching electric blues"),
    "dreamy": ("dreamy ethereal spacey music", "floating atmospheric dreamy sound"),
    "romantic": ("romantic tender love song", "intimate warm romantic ballad"),
    "melancholic": ("melancholic sad lonely music", "mournful heartbroken slow song"),
    "calm": ("calm peaceful relaxed music", "quiet gentle soft music"),
    "uplifting": ("uplifting motivating hopeful music", "inspiring positive anthem"),
    "cheerful": ("cheerful happy bright playful music", "sunny feel-good pop"),
    "tense": ("tense dark ominous suspenseful music", "brooding threatening music"),
}
CHARACTERS = tuple(CHARACTER_PROMPTS)

#: Within each mood, which characters name a feeling, and what it is called.
#: The quadrant decides *where* on the plane; the character says *what kind*.
FEELINGS_BY_MOOD: dict[str, dict[str, str]] = {
    "angry": {"aggressive": "angry", "bluesy": "frustrated", "energetic": "intense",
              "dramatic": "dramatic", "tense": "tense"},
    "happy": {"cheerful": "cheerful", "energetic": "energetic", "festive": "festive",
              "uplifting": "uplifting", "dramatic": "dramatic"},
    "tender": {"romantic": "romantic", "dreamy": "dreamy", "calm": "calm",
               "uplifting": "hopeful"},
    "sad": {"melancholic": "melancholic", "bluesy": "blue", "dreamy": "wistful"},
}
#: Broad words that lose a near-tie to a specific one: Tina called "Let's Do
#: It" romantic, where calm led romantic by 0.03.
GENERIC_FEELINGS = {"calm", "cheerful", "energetic"}
NEAR_TIE_Z = 0.15
#: When no character stands out. Dark-and-intense is "intense", not "angry":
#: anger has to be heard (an aggressive sound) or said (confrontational words).
DEFAULT_FEELING = {"angry": "intense", "happy": "happy", "tender": "tender", "sad": "sad"}

#: A character must beat its library baseline by this much (z) to name the
#: feeling; below it the quadrant's default stands.
CHARACTER_MIN_Z = 0.3

#: Sound and words this far apart, in opposite directions, are a contrast
#: worth naming rather than averaging away -- bittersweet. Set high on
#: purpose: CLAP reads most soft acoustic music as warm, so a modest gap is
#: that bias, not a feeling. At 0.3 "The Will to Death" came out bittersweet;
#: Tina hears it as plainly sad.
CONTRAST_SOUND = 0.6
CONTRAST_WORDS = 0.5


# -- calibration ---------------------------------------------------------------

def _calibration_path() -> Path:
    from .config import settings

    # One file per version: a newer build must not overwrite the baselines a
    # running older worker reads, or the two would rebuild over each other.
    return Path(settings.data_dir) / f"mood_calibration.v{MOOD_VERSION}.json"


def _unit(rows):
    import numpy as np

    rows = np.asarray(rows, dtype=float)
    norms = np.linalg.norm(rows, axis=-1, keepdims=True)
    norms[norms == 0] = 1.0
    return rows / norms


#: Prompt matrices, embedded once per process. They depend only on the fixed
#: prompts; re-embedding them for every song cost ~3.9 s a song (1.4 s for
#: the moods, 2.5 s for the characters) -- most of a backfill's time.
_matrix_cache: dict[str, Any] = {}


def _cached(name: str, build):
    if name not in _matrix_cache:
        _matrix_cache[name] = build()
    return _matrix_cache[name]


def _audio_prompt_matrix():
    """Unit text vectors, one per mood, each the mean of its phrasings."""
    return _cached("audio", _build_audio_prompt_matrix)


def _build_audio_prompt_matrix():
    import numpy as np

    from . import clap_vector

    rows = []
    for mood in AUDIO_MOODS:
        vecs = [clap_vector.embed_text(p) for p in AUDIO_PROMPTS[mood]]
        rows.append(np.mean([v for v in vecs if v], axis=0))
    return _unit(rows)


def _character_prompt_matrix():
    """Unit text vectors for the sound characters, like the mood prompts."""
    return _cached("character", _build_character_prompt_matrix)


def _build_character_prompt_matrix():
    import numpy as np

    from . import clap_vector

    rows = []
    for name in CHARACTERS:
        vecs = [clap_vector.embed_text(p) for p in CHARACTER_PROMPTS[name]]
        rows.append(np.mean([v for v in vecs if v], axis=0))
    return _unit(rows)


def _tone_matrix():
    from .embed import embed_text

    return _cached("tone", lambda: _unit([embed_text(f"lyrics that are {tone}")
                                          for tone in TONE_VALENCE]))


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
    characters = _unit(vectors) @ _character_prompt_matrix().T

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
        "character_mean": characters.mean(axis=0).tolist(),
        "character_std": characters.std(axis=0).tolist(),
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
    #: The finer word for this stretch: "frustrated", "dreamy", "festive"...
    feeling: str = ""
    #: When sound and words pull opposite ways: "bittersweet" or "ironic".
    contrast: Optional[str] = None

    @property
    def label(self) -> str:
        return f"{self.mood}↔{self.alt}" if self.alt else self.mood

    @property
    def describe(self) -> str:
        """The feeling with its tension, e.g. "longing" or "cheerful, ironic"."""
        word = self.feeling or self.mood
        return f"{word}, {self.contrast}" if self.contrast else word


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


def feeling_for(mood: str, alt: Optional[str], character: Optional[dict[str, float]],
                context: Optional[dict[str, Any]] = None) -> str:
    """The finer word for a stretch, from its quadrant, sound character and context.

    ``context`` may carry ``tone`` (the lyrics' clear tone, e.g. "angry and
    confrontational") and ``blues`` (the chords say blues).
    """
    context = context or {}
    # Sad edging into tender is the in-between Tina called "longing"
    # ("I Put a Spell on You": sad <-> longing).
    if {mood, alt} == {"sad", "tender"}:
        return "longing"
    if mood in ("angry", "sad") and context.get("blues"):
        return "frustrated" if mood == "angry" else "blue"
    if mood == "angry" and (context.get("tone") or "").startswith("angry"):
        return "angry"
    if mood == "angry" and (context.get("tone") or "").startswith("defiant"):
        return "defiant"
    options = FEELINGS_BY_MOOD.get(mood, {})
    if character:
        ranked = sorted(((character.get(c, float("-inf")), name)
                         for c, name in options.items()), reverse=True)
        if ranked and ranked[0][0] >= CHARACTER_MIN_Z:
            best_z, best = ranked[0]
            if best in GENERIC_FEELINGS:
                for z, name in ranked[1:]:
                    if name not in GENERIC_FEELINGS and best_z - z <= NEAR_TIE_Z:
                        return name
            return best
    return DEFAULT_FEELING.get(mood, mood)


def contrast_for(sound_v: Optional[float], words_v: Optional[float],
                 context: Optional[dict[str, Any]] = None) -> Optional[str]:
    """Name a pull between sound and words instead of averaging it away.

    "ironic" needs the lyrics to read clearly cynical: a gap alone mostly
    meant *bittersweet* (warm sound, sad words), and the meaning model reads
    "Pow Wow" -- Tina: cheerful surface, war underneath -- as tender, so it
    cannot be found from the gap.
    """
    context = context or {}
    if (context.get("tone") or "").startswith("cynical"):
        return "ironic"
    if sound_v is None or words_v is None:
        return None
    if sound_v >= CONTRAST_SOUND and words_v <= -CONTRAST_WORDS:
        return "bittersweet"
    return None


def build(valence, arousal, *, window_s: float = WINDOW_S,
          duration_s: Optional[float] = None, has_lyrics: bool = False,
          character=None, sound_v=None, words_v=None,
          context: Optional[dict[str, Any]] = None) -> MoodTimeline:
    """Smoothed per-window valence/arousal -> segments, swings and borderliners.

    ``character`` (windows x CHARACTERS z-scores), ``sound_v`` and
    ``words_v`` (per window) are optional: with them, each segment also gets
    a feeling and, where sound and words disagree, a contrast.
    """
    import numpy as np
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

    # A segment is named by where it sits on average, not by the window
    # labels that formed it: hysteresis can carry a label across a stretch
    # whose own average lies in another quadrant ("Simple Song" at 3:00 was
    # labelled angry with a warm, calm mean). Renaming can make neighbours
    # equal, so merge and rename until nothing changes.
    runs = [(s, e) for _, s, e in _runs(labels)]
    while True:
        named = [(classify(float(v[s:e].mean()), float(a[s:e].mean()))[0], s, e)
                 for s, e in runs]
        merged: list[tuple[str, int, int]] = []
        for lab, s, e in named:
            if merged and merged[-1][0] == lab:
                merged[-1] = (lab, merged[-1][1], e)
            else:
                merged.append((lab, s, e))
        if len(merged) == len(runs):
            break
        runs = [(s, e) for _, s, e in merged]

    char = np.asarray(character, dtype=float) if character is not None else None
    snd = np.asarray(sound_v, dtype=float) if sound_v is not None else None
    wrd = np.asarray(words_v, dtype=float) if words_v is not None else None
    segments = []
    for lab, s, e in merged:
        sv, sa = float(v[s:e].mean()), float(a[s:e].mean())
        _, alt = classify(sv, sa)
        means = (dict(zip(CHARACTERS, char[s:e].mean(axis=0)))
                 if char is not None and len(char) >= e else None)
        segments.append(Segment(
            start_s=s * window_s, end_s=min(e * window_s, duration),
            mood=lab, valence=sv, arousal=sa, alt=alt,
            feeling=feeling_for(lab, alt, means, context),
            contrast=contrast_for(
                float(snd[s:e].mean()) if snd is not None and len(snd) >= e else None,
                float(wrd[s:e].mean()) if wrd is not None and len(wrd) >= e else None,
                context)))
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
    feel_time: dict[str, float] = {}
    contrast_time: dict[str, float] = {}
    for seg in segments:
        span = seg.end_s - seg.start_s
        if seg.feeling:
            feel_time[seg.feeling] = feel_time.get(seg.feeling, 0.0) + span
        if seg.contrast:
            contrast_time[seg.contrast] = contrast_time.get(seg.contrast, 0.0) + span
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
        "dominant_feeling": max(feel_time, key=feel_time.get) if feel_time else dominant,
        "feeling_arc": " → ".join(s.describe for s in segments),
        # A contrast counts for the song when it holds for at least a third.
        "contrast": (max(contrast_time, key=contrast_time.get)
                     if contrast_time and duration_s
                     and max(contrast_time.values()) / duration_s >= 1 / 3 else None),
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
    emb = _unit(np.concatenate(feats))
    z = ((emb @ _audio_prompt_matrix().T) - np.array(cal["audio_mean"])) / np.array(cal["audio_std"])
    char = ((emb @ _character_prompt_matrix().T) - np.array(cal["character_mean"])) \
        / np.array(cal["character_std"])
    rms = np.array([float(np.sqrt(np.mean(w ** 2))) for w in windows])
    return z, char, rms, len(y) / clap_vector.SAMPLE_RATE


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


_tone_labels: Optional[dict[str, list[float]]] = None


def _lyric_tone(text: str) -> Optional[str]:
    """The lyrics' tone when the classifier is clear about it, else None."""
    global _tone_labels
    from . import tone

    if len((text or "").strip()) < 80:
        return None
    if _tone_labels is None:
        _tone_labels = tone.label_vectors()
    verdict = tone.classify(text, _tone_labels)
    return verdict.tone if verdict is not None and verdict.clear else None


def analyse(audio_path: str, lyric_lines: list[tuple[float, str]],
            plain_lyrics: str = "", *, blues: bool = False) -> Optional[MoodTimeline]:
    """A track's mood timeline from its audio and (timed) lyrics.

    ``blues``: the chord analysis calls the track a blues, which turns dark
    intensity into frustration rather than anger.
    """
    import numpy as np

    scored = _audio_scores(audio_path)
    if scored is None:
        return None
    z, char, rms, duration = scored
    d = {m: z[:, i] for i, m in enumerate(AUDIO_MOODS)}
    db = 20 * np.log10(np.maximum(rms, 1e-6))
    loud = np.clip((db - np.median(db)) / LOUDNESS_DB, -1.5, 1.5)
    arousal = (d["angry"] + d["happy"] - d["sad"] - d["tender"]) / 2 + LOUDNESS_WEIGHT * loud
    audio_v = np.tanh((d["happy"] + d["tender"] - d["sad"] - d["angry"]) / 2)

    whole = "\n".join(t for _, t in lyric_lines) or plain_lyrics
    song_v = _meaning_valence([whole])[0] if whole else None
    context = {"tone": _lyric_tone(whole), "blues": blues}
    words = None
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
    return build(valence, arousal, duration_s=duration, has_lyrics=song_v is not None,
                 character=char, sound_v=audio_v, words_v=words, context=context)


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
    blues = False
    try:
        chords_row = conn.execute("SELECT harmony FROM track_chords WHERE track_id = %s",
                                  (track_id,)).fetchone()
        blues = bool(chords_row and (chords_row["harmony"] or {}).get("style") == "blues")
    except Exception:
        conn.rollback()
    timeline = analyse(str(path), lines, plain, blues=blues)
    if timeline is not None:
        save(track_id, timeline, conn, source_path=str(path))
    return timeline
