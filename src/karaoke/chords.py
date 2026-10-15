"""Chord detection, and the root motion that separates jazz from pop.

Key detection cannot see what makes jazz jazz. A ii-V-I does not change the
key -- that is what makes it a cadence rather than a modulation -- so a
fifteen-second key window averages straight over the most characteristic
gesture in the idiom. Measured against real records that showed up starkly:
Ella Fitzgerald singing Cole Porter scored identically to the Red Hot Chili
Peppers on every key-level statistic, because at the level of *key* they are
the same.

Chords change every bar or two, so this works at roughly one frame per
quarter second and asks a different question: not "what key is this" but
"where do the roots move". Descending-fifth motion -- the engine of ii-V-I
chains and the circle of fifths -- is what a pop song built on I-IV-V does
not do repeatedly, and what a standard does constantly.

Templates rather than a trained model: a triad has a known pitch-class
profile, the library has no chord labels to train on, and a template match is
inspectable when it gets something wrong.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .logger import log

SAMPLE_RATE = 22050

#: One frame per quarter second. Fast enough to place a chord inside a bar at
#: most tempos, slow enough that a passing tone does not become a chord.
FRAME_SECONDS = 0.25

#: A chord must hold this long to count. Below it the detector is tracking
#: melody, not harmony.
MIN_CHORD_SECONDS = 0.5

#: Bumped whenever the labels a track gets would change, so stored analyses
#: from an older detector can be found and redone.
CHORDS_VERSION = 2

#: The triad decides the root and the chord family. Extensions are decided
#: afterwards, per segment, by :func:`_extend` -- see the note there for why
#: they are not simply more templates.
_TRIADS = {
    "maj": (0, 4, 7),
    "min": (0, 3, 7),
    "dim": (0, 3, 6),
    "sus4": (0, 5, 7),
}

#: Log-prior per triad, added to its correlation. Major and minor are what
#: most music sits on; a suspended or diminished triad has to be clearly
#: heard. Without this, chroma noise from vocals and distortion made sus4 the
#: label for a fifth of all chords and scattered dim triads through blues.
TRIAD_PRIOR = {"maj": 0.0, "min": 0.0, "dim": -0.1, "sus4": -0.15}

#: Every label the detector can emit, as pitch-class offsets from the root.
#: Used to name chords and to find labels that are the same notes read from a
#: different root (C6 and Am7, or the four spellings of a dim7).
_CHORD_TEMPLATES = {
    "maj": (0, 4, 7),
    "min": (0, 3, 7),
    "dim": (0, 3, 6),          # Diminished triad (vii in major)
    "sus4": (0, 5, 7),         # Suspended 4th (pop/rock tensions)
    "7": (0, 4, 7, 10),        # Dominant 7th (key in blues, jazz, funk)
    "maj7": (0, 4, 7, 11),     # Major 7th (smooth jazz, neo-soul)
    "min7": (0, 3, 7, 10),     # Minor 7th (soul, funk, jazz)
    "dim7": (0, 3, 6, 9),      # Diminished 7th
    "m7b5": (0, 3, 6, 10),     # Half-diminished (ii in minor ii-V-i)
    "m6": (0, 3, 7, 9),        # Minor 6th (tonic minor in gypsy jazz / swing)
    "6": (0, 4, 7, 9),         # Major 6th (tonic major in gypsy jazz / swing)
    "6/9": (0, 4, 7, 9, 2),    # 6/9 chord (gypsy jazz resolution)
    "9": (0, 4, 7, 10, 2),     # Dominant 9th (funk, blues, gypsy jazz)
}

#: Candidate extensions per triad: semitone above the root -> resulting label.
_EXTENSIONS = {
    "maj": {10: "7", 11: "maj7", 9: "6"},
    "min": {10: "min7", 9: "m6"},
    "dim": {10: "m7b5", 9: "dim7"},
    "sus4": {},
}
#: A ninth on top of these becomes the five-note label.
_NINTHS = {"7": "9", "6": "6/9"}

_NOTE = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

#: Root motion down a perfect fifth, in semitones, as a positive interval
#: modulo 12. C -> F is +5; that is the same motion as descending a fifth.
DESCENDING_FIFTH = 5

#: Weight of the bass register when choosing a root. Enough to pick C over Am
#: when the bass plays C under {C, E, G, A}; small enough that a walking bass
#: passing through a chord tone does not relabel the chord on every beat.
BASS_WEIGHT = 0.2

#: Cost, in correlation units, of changing chord between two frames. Replaces
#: dropping short runs: a run too short to keep used to vanish and leave its
#: neighbours as two chords with a repeat between them; a switch cost lets the
#: surrounding chord absorb it instead.
SWITCH_PENALTY = 0.6

#: A frame whose best triad correlates below this is no chord -- silence,
#: drums, a spoken intro. Without it every such frame was forced into a label.
NO_CHORD_SCORE = 0.25

#: Expected leakage of a chord tone's overtones into chroma, as a share of the
#: tone's own energy: the 3rd harmonic lands a fifth up, the 5th harmonic a
#: major third up. For a C triad both land on B -- E's 3rd harmonic and G's 5th
#: -- which is why the previous detector heard maj7 on nearly every major
#: chord of a B.B. King record. Subtracted before an extension is judged.
OVERTONE_FIFTH = 0.2
OVERTONE_THIRD = 0.05

#: An extension is heard when its residual energy clears the noise floor by
#: this share of the triad's own height above that floor.
EXTENSION_THRESHOLD = 0.15


@dataclass
class Chord:
    """One detected chord and where it sits."""

    at_s: float
    root: int                  # pitch class 0-11
    quality: str               # a key of _CHORD_TEMPLATES
    strength: float
    end_s: float = 0.0

    @property
    def name(self) -> str:
        root_name = _NOTE[self.root]
        if self.quality == "maj":
            return root_name
        elif self.quality == "min":
            return f"{root_name}m"
        elif self.quality == "min7":
            return f"{root_name}m7"
        elif self.quality == "m6":
            return f"{root_name}m6"
        else:
            return f"{root_name}{self.quality}"


@dataclass
class ChordAnalysis:
    """A track's chord sequence and what its root motion looks like."""

    chords: list[Chord]
    duration_s: float
    #: Histogram of root motion intervals between consecutive chords, 0-11,
    #: normalised. Key-invariant: the same progression in any key is identical.
    motion: list[float]

    @property
    def fifth_ratio(self) -> float:
        """Share of chord changes that move down a fifth.

        A I-IV-V song produces some by construction -- I->IV and V->I are both
        down a fifth -- so this alone cannot tell a blues from a standard. A
        ii-V-I chain produces almost nothing else; see :mod:`karaoke.harmony`
        for the cadence-level reading that separates the two.
        """
        return self.motion[DESCENDING_FIFTH] if self.motion else 0.0

    @property
    def changes_per_minute(self) -> float:
        if self.duration_s <= 0:
            return 0.0
        return len(self.chords) / (self.duration_s / 60.0)

    @property
    def distinct_chords(self) -> int:
        return len({(c.root, c.quality) for c in self.chords})

    def at(self, seconds: float) -> Optional[Chord]:
        """The chord sounding at a moment, or None between chords."""
        for chord in self.chords:
            if chord.at_s <= seconds < (chord.end_s or chord.at_s):
                return chord
        return None


def _pitch_set(root: int, quality: str) -> frozenset[int]:
    return frozenset((root + i) % 12 for i in _CHORD_TEMPLATES[quality])


def _triad_matrix():
    """Mean-centred, unit-norm triad templates and their (root, quality) keys.

    Centred because a plain cosine against a binary template rewards size: on
    a flat chroma it scores sqrt(k/12) for a k-note template, so the more notes
    a template had, the more often it won regardless of what was played.
    Centring makes the score a correlation, where size earns nothing.
    """
    import numpy as np

    keys, rows = [], []
    for quality, intervals in _TRIADS.items():
        for root in range(12):
            vec = np.zeros(12, dtype=float)
            for i in intervals:
                vec[(root + i) % 12] = 1.0
            vec -= vec.mean()
            rows.append(vec / np.linalg.norm(vec))
            keys.append((root, quality))
    return keys, np.stack(rows)


def _viterbi(scores, penalty: float):
    """Best state path when every change of state costs ``penalty``."""
    import numpy as np

    n_frames, _ = scores.shape
    back = np.zeros(scores.shape, dtype=np.int32)
    total = scores[0].copy()
    for f in range(1, n_frames):
        best_prev = int(total.argmax())
        switch = total[best_prev] - penalty
        stay = total >= switch
        back[f] = np.where(stay, np.arange(len(total)), best_prev)
        total = np.where(stay, total, switch) + scores[f]
    path = np.empty(n_frames, dtype=np.int32)
    path[-1] = int(total.argmax())
    for f in range(n_frames - 1, 0, -1):
        path[f - 1] = back[f, path[f]]
    return path


def _extend(root: int, triad: str, chroma) -> str:
    """Decide the seventh, sixth and ninth on a segment's averaged chroma.

    Extensions are not templates competing with triads: whether a four-note
    template beats a three-note one depends on how much energy the rest of the
    chroma carries, so the old detector heard maj7 everywhere on dense
    records and no sevenths at all on clean ones. Here the triad is already
    known, the overtones its own tones throw into the other bins are removed,
    and each candidate note has to stand clear of what is left.
    """
    import numpy as np

    r = np.roll(chroma, -root)
    tones = _TRIADS[triad]
    leak = np.zeros(12)
    for t in tones:
        leak[(t + 7) % 12] += OVERTONE_FIFTH * r[t]
        leak[(t + 4) % 12] += OVERTONE_THIRD * r[t]
    residual = r - leak
    candidates = _EXTENSIONS[triad]
    rest = [i for i in range(12) if i not in tones and i not in candidates and i != 2]
    floor = float(np.median(residual[rest])) if rest else 0.0
    height = float(np.mean(r[list(tones)])) - floor
    if height <= 0:
        return triad

    def heard(i: int) -> bool:
        return residual[i] - floor > EXTENSION_THRESHOLD * height

    present = [i for i in candidates if heard(i)]
    if not present:
        return triad
    label = candidates[max(present, key=lambda i: residual[i])]
    if label in _NINTHS and heard(2):
        label = _NINTHS[label]
    return label


def _respell(root: int, quality: str, bass) -> tuple[int, str]:
    """Among labels with the same notes, the one whose root is in the bass.

    C6 and Am7 are both {C, E, G, A}; the four dim7s on C, D#, F# and A are
    one chord. Without the bass the first template in the table won every
    such tie, so C6 and m6 were never reported and every dim7 sat on C, C# or
    D -- which also put false root motion into the jazz statistics.
    """
    notes = _pitch_set(root, quality)
    readings = [(r, q) for q in _CHORD_TEMPLATES for r in range(12)
                if len(_CHORD_TEMPLATES[q]) == len(notes) and _pitch_set(r, q) == notes]
    if len(readings) < 2:
        return root, quality
    best = max(readings, key=lambda rq: bass[rq[0]])
    # Keep the reading already chosen unless the bass clearly prefers another.
    if bass[best[0]] > bass[root] * 1.15:
        return best
    return root, quality


def detect(audio_path: str, *, frame_seconds: float = FRAME_SECONDS,
           sample_rate: int = SAMPLE_RATE,
           harmonic: bool = False) -> Optional[ChordAnalysis]:
    """Detect the chord sequence of a track.

    ``harmonic`` runs percussive/harmonic separation first, which cleans the
    chroma but costs 22x the total runtime -- measured at 37.0s against 1.7s
    for the CQT itself, so it is 95% of the work. Off by default: over a
    library that is the difference between an hour and a day, and the chord
    labels it changes are mostly ones already too short to be kept.

    Returns None when the audio cannot be read or librosa is unavailable.
    """
    try:
        import librosa
        import numpy as np

        from .analyze import _as_wav
    except Exception:
        log.debug("librosa unavailable; no chord detection")
        return None

    try:
        with _as_wav(audio_path, sample_rate) as wav:
            if not wav:
                return None
            y, sr = librosa.load(wav, sr=sample_rate, mono=True)
    except Exception:
        log.debug("could not decode %s for chords", audio_path, exc_info=True)
        return None

    if y is None or len(y) < sr * 5:
        return None

    try:
        hop = max(512, int(sr * frame_seconds))
        # CQT-based chroma: pitch-aligned bins, which template matching needs.
        # chroma_stft is 13x faster again but its bins are not pitch-aligned,
        # so a triad template matches poorly.
        source = librosa.effects.harmonic(y, margin=3.0) if harmonic else y
        chroma = librosa.feature.chroma_cqt(y=source, sr=sr, hop_length=hop)
        # The bass register alone, C1 to B3: which note of the chord is lowest.
        bass = librosa.feature.chroma_cqt(y=source, sr=sr, hop_length=hop,
                                          fmin=librosa.note_to_hz("C1"),
                                          n_octaves=3)
    except Exception:
        log.debug("chroma failed for %s", audio_path, exc_info=True)
        return None

    n = min(chroma.shape[1], bass.shape[1])
    frames = chroma[:, :n].T                                  # frames x 12
    bass_frames = bass[:, :n].T
    peak = bass_frames.max(axis=1, keepdims=True)
    peak[peak == 0] = 1.0
    bass_frames = bass_frames / peak

    # Take the overtones out before choosing the triad, not only before the
    # extensions: every tone's 3rd harmonic lands a fifth up, which feeds the
    # stacked fourths and fifths of a sus4 and made it the label for a quarter
    # of all chords. np.roll by 7 moves each tone's energy onto its fifth.
    whitened = np.clip(frames
                       - OVERTONE_FIFTH * np.roll(frames, 7, axis=1)
                       - OVERTONE_THIRD * np.roll(frames, 4, axis=1), 0.0, None)

    keys, matrix = _triad_matrix()
    centred = whitened - whitened.mean(axis=1, keepdims=True)
    norms = np.linalg.norm(centred, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    scores = (centred / norms) @ matrix.T                     # frames x 48
    roots = np.array([k[0] for k in keys])
    prior = np.array([TRIAD_PRIOR[k[1]] for k in keys])
    scores = scores + BASS_WEIGHT * bass_frames[:, roots] + prior
    # One extra state for "no chord", with a flat score.
    scores = np.hstack([scores, np.full((n, 1), NO_CHORD_SCORE)])
    path = _viterbi(scores, SWITCH_PENALTY)
    no_chord = len(keys)

    frame_dur = hop / sr
    unit = frames / np.maximum(frames.max(axis=1, keepdims=True), 1e-9)
    chords: list[Chord] = []
    run_start = 0
    for i in range(1, n + 1):
        if i < n and path[i] == path[run_start]:
            continue
        state = int(path[run_start])
        if state != no_chord and (i - run_start) * frame_dur >= MIN_CHORD_SECONDS:
            root, triad = keys[state]
            quality = _extend(root, triad, unit[run_start:i].mean(axis=0))
            root, quality = _respell(root, quality, bass_frames[run_start:i].mean(axis=0))
            start_s, end_s = run_start * frame_dur, i * frame_dur
            strength = float(scores[run_start:i, state].mean())
            prev = chords[-1] if chords else None
            if (prev and (prev.root, prev.quality) == (root, quality)
                    and start_s - prev.end_s < MIN_CHORD_SECONDS):
                prev.end_s = end_s           # same chord either side of a blip
            else:
                chords.append(Chord(at_s=start_s, root=root, quality=quality,
                                    strength=strength, end_s=end_s))
        run_start = i

    duration = len(y) / sr
    motion = [0.0] * 12
    for prev, nxt in zip(chords, chords[1:]):
        step = (nxt.root - prev.root) % 12
        if step:                       # a repeat is not motion
            motion[step] += 1.0
    total = sum(motion) or 1.0
    motion = [m / total for m in motion]

    return ChordAnalysis(chords=chords, duration_s=duration, motion=motion)


def describe(analysis: ChordAnalysis, limit: int = 8) -> str:
    """A readable summary, for a TUI or a log."""
    seq = " ".join(c.name for c in analysis.chords[:limit])
    return (f"{analysis.distinct_chords} chords, "
            f"{analysis.changes_per_minute:.0f}/min, "
            f"fifths {analysis.fifth_ratio:.0%}  |  {seq}")
