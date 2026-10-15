"""Functional harmony: what a chord sequence *does*, not just which chords.

:mod:`karaoke.chords` labels chords and histograms their root motion. Root
motion alone cannot tell a blues from a standard: I->IV and V->I are both
down a fifth, so a twelve-bar blues and a ii-V-I chain land in the same slot.
What separates them is function -- which chord moves to which, and in what
order:

* **ii-V-I** -- a minor seventh, a dominant a fifth below it, then the tonic a
  fifth below that. The cadence of jazz standards. Its minor form runs
  half-diminished -> dominant -> minor.
* **Tritone substitution** -- a dominant resolving down a semitone instead of a
  fifth (Db7 -> C). Shares its guide tones with the V7 it replaces.
* **Secondary dominant** -- a dominant resolving down a fifth to a chord other
  than the tonic (A7 -> Dm is V7/ii).
* **Backdoor cadence** -- bVII7 -> I, the tonic approached from a whole step
  below.
* **Andalusian cadence** -- i, bVII, bVI, V descending: flamenco, "Hit the
  Road Jack", much of surf and metal.
* **Blues** -- time concentrated on I, IV and V, each carrying a flat seventh.
  A blues tonic is a dominant chord, which no major-key theory accounts for,
  and the melody's blue third makes it read as minor as often as major.

Everything here works on the chord sequence, so it is cheap: the audio has
already been decoded once by chord detection.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional

from .chords import ChordAnalysis, Chord

#: Qualities by function. A dominant is anything with a major third and a
#: flat seventh; a "minor two" is the chord a ii-V starts on.
DOMINANT = {"7", "9"}
MINOR_TWO = {"min7", "min"}
HALF_DIM_TWO = {"m7b5", "dim"}
MAJOR_TONIC = {"maj", "maj7", "6", "6/9"}
MINOR_TONIC = {"min", "min7", "m6"}
#: In a blues the I, IV and V carry a flat seventh. The I alone may read as
#: minor: the melody's blue third pulls the detector there about as often as
#: not. A minor seventh on IV or V is a minor-key ii or v, not a blues.
BLUES_TONIC = DOMINANT | {"min7"}

#: Semitones above the tonic -> numeral. Chromatic degrees are spelled flat,
#: the convention for rock and jazz (bIII, bVI, bVII), so one table serves
#: both modes: in A minor, C is bIII, which is what a player calls it.
_NUMERALS = {0: "I", 1: "bII", 2: "II", 3: "bIII", 4: "III", 5: "IV",
             6: "bV", 7: "V", 8: "bVI", 9: "VI", 10: "bVII", 11: "VII"}
_SUFFIX = {"maj": "", "min": "", "dim": "°", "sus4": "sus4", "7": "7",
           "maj7": "maj7", "min7": "7", "dim7": "°7", "m7b5": "ø7",
           "m6": "6", "6": "6", "6/9": "6/9", "9": "9"}
_LOWER = {"min", "min7", "m6", "dim", "dim7", "m7b5"}

#: Above this share of chord time on I, IV and V with flat sevenths, a track
#: is reported as a blues. Calibrated on the B.B. King records in the library
#: against Red Hot Chili Peppers and the Beatles.
BLUES_THRESHOLD = 0.35


@dataclass
class Key:
    tonic: int          # pitch class 0-11
    minor: bool


def estimate_key(analysis: ChordAnalysis) -> Optional[Key]:
    """The tonic the chords themselves point at.

    Weighted by duration, with cadence targets counting double: a song may
    spend longer on its IV than its I, but it resolves to the I. Used when the
    audio key detector is missing or disagrees -- it reads many blues as minor
    because of the blue third, where the chords say otherwise.
    """
    if not analysis.chords:
        return None
    weight = [0.0] * 12
    minor_time = [0.0] * 12
    for i, c in enumerate(analysis.chords):
        dur = max(c.end_s - c.at_s, 0.0)
        weight[c.root] += dur
        if c.quality in _LOWER:
            minor_time[c.root] += dur
        prev = analysis.chords[i - 1] if i else None
        if prev and prev.quality in DOMINANT and (prev.root - c.root) % 12 == 7:
            weight[c.root] += dur        # V -> I lands here
    # Songs open and close on the tonic far more often than anywhere else;
    # without this, equal time on every chord gave the tonic to whichever
    # pitch class came first in the table.
    for c in (analysis.chords[0], analysis.chords[-1]):
        weight[c.root] += 0.5 * max(c.end_s - c.at_s, 0.0)
    tonic = max(range(12), key=weight.__getitem__)
    return Key(tonic=tonic, minor=minor_time[tonic] > weight[tonic] / 2)


def key_from_text(text: str, analysis: Optional[ChordAnalysis] = None) -> Optional[Key]:
    """A Key from the audio detector's label ("A minor"), mode checked by chords.

    The audio detector places the tonic well but reads a blues's blue third as
    minor, so its mode is only kept when the chords on that tonic agree.
    """
    from . import musictheory

    parsed = musictheory.parse_key(text or "")
    if parsed is None:
        return None
    minor = parsed.mode == "minor"
    if analysis is not None and analysis.chords:
        on_tonic = [c for c in analysis.chords if c.root == parsed.tonic]
        if on_tonic:
            lower = sum(max(c.end_s - c.at_s, 0.0) for c in on_tonic if c.quality in _LOWER)
            total = sum(max(c.end_s - c.at_s, 0.0) for c in on_tonic)
            minor = lower > total / 2
    return Key(tonic=parsed.tonic, minor=minor)


def roman(chord: Chord, key: Key) -> str:
    """The chord's numeral in a key: ii7, V7, bVIImaj7, viiø7."""
    numeral = _NUMERALS[(chord.root - key.tonic) % 12]
    if chord.quality in _LOWER:
        numeral = numeral.lower()
    return numeral + _SUFFIX.get(chord.quality, chord.quality)


@dataclass
class HarmonySummary:
    key_tonic: int
    key_minor: bool
    ii_v_i: int = 0
    minor_ii_v_i: int = 0
    ii_v: int = 0
    tritone_subs: int = 0
    secondary_dominants: int = 0
    backdoor: int = 0
    andalusian: int = 0
    #: Share of chord time on dominant-quality chords.
    dominant_share: float = 0.0
    #: Share of chord time on I, IV and V carrying a flat seventh.
    blues_score: float = 0.0
    #: ii-V-I cadences of either mode per minute of music.
    cadences_per_minute: float = 0.0
    style: str = "unknown"

    def as_doc(self) -> dict[str, Any]:
        return asdict(self)


def _down_fifth(a: Chord, b: Chord) -> bool:
    return (b.root - a.root) % 12 == 5


def summarise(analysis: ChordAnalysis, key: Optional[Key] = None) -> Optional[HarmonySummary]:
    """Count the functional patterns in a chord sequence."""
    seq = analysis.chords
    key = key or estimate_key(analysis)
    if key is None or not seq:
        return None
    out = HarmonySummary(key_tonic=key.tonic, key_minor=key.minor)

    total = sum(max(c.end_s - c.at_s, 0.0) for c in seq) or 1.0
    dom_time = blues_time = 0.0
    primary = {key.tonic, (key.tonic + 5) % 12, (key.tonic + 7) % 12}
    for c in seq:
        dur = max(c.end_s - c.at_s, 0.0)
        if c.quality in DOMINANT:
            dom_time += dur
        allowed = BLUES_TONIC if c.root == key.tonic else DOMINANT
        if c.root in primary and c.quality in allowed:
            blues_time += dur
    out.dominant_share = round(dom_time / total, 3)
    out.blues_score = round(blues_time / total, 3)

    for a, b in zip(seq, seq[1:]):
        if a.quality not in DOMINANT:
            continue
        step = (b.root - a.root) % 12
        if step == 11 and (b.quality in MAJOR_TONIC or b.quality in MINOR_TONIC):
            out.tritone_subs += 1
        elif step == 5 and b.root != key.tonic:
            out.secondary_dominants += 1
        elif step == 2 and b.root == key.tonic and (a.root - key.tonic) % 12 == 10:
            out.backdoor += 1

    for a, b, c in zip(seq, seq[1:], seq[2:]):
        if b.quality not in DOMINANT or not _down_fifth(a, b):
            continue
        if a.quality in MINOR_TWO:
            out.ii_v += 1
            if _down_fifth(b, c) and c.quality in MAJOR_TONIC:
                out.ii_v_i += 1
        elif a.quality in HALF_DIM_TWO:
            out.ii_v += 1
            if _down_fifth(b, c) and c.quality in MINOR_TONIC:
                out.minor_ii_v_i += 1

    for a, b, c, d in zip(seq, seq[1:], seq[2:], seq[3:]):
        steps = [(x.root - a.root) % 12 for x in (b, c, d)]
        if steps == [10, 8, 7] and a.quality in MINOR_TONIC:
            out.andalusian += 1

    minutes = analysis.duration_s / 60.0 if analysis.duration_s > 0 else 0.0
    if minutes:
        out.cadences_per_minute = round((out.ii_v_i + out.minor_ii_v_i) / minutes, 2)
    out.style = _style(out)
    return out


def _style(s: HarmonySummary) -> str:
    """A coarse label from the counts, in order of how distinctive each is."""
    if s.cadences_per_minute >= 1.0 or (s.ii_v_i + s.minor_ii_v_i + s.tritone_subs) >= 4:
        return "jazz-functional"
    if s.blues_score >= BLUES_THRESHOLD:
        return "blues"
    if s.andalusian >= 2:
        return "andalusian"
    if s.dominant_share < 0.08:
        return "triadic"
    return "mixed"
