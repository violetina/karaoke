"""Pure music-theory helpers: keys, relatives, reconciliation, Camelot wheel.

No audio dependencies — all string/number logic, fully unit-tested. This powers
the "we detected Am but the web says C major" reconciliation: those two are the
same tonal centre (relative keys), so we should recognise them as compatible
rather than flagging a disagreement.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# Canonical pitch classes (sharps). Index == semitone from C.
_SHARP = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
# Flat spellings for the same semitone index.
_FLAT = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]

# Enharmonic + common-input aliases -> semitone index.
_ALIASES = {
    "e#": 5, "b#": 0, "cb": 11, "fb": 4,
    "c##": 2, "abb": 7,
}

MODES = ("major", "minor")

# Rough character/vibe per mode — a creative cue, not academic theory.
_MODE_CHARACTER = {
    "major": "bright, resolved, uplifting",
    "minor": "darker, wistful, emotional",
}

# Camelot wheel: (semitone, mode) -> code. Used by DJs for harmonic mixing and
# a fun way to surface "compatible keys".
_CAMELOT_MAJOR = {
    0: "8B", 1: "3B", 2: "10B", 3: "5B", 4: "12B", 5: "7B",
    6: "2B", 7: "9B", 8: "4B", 9: "11B", 10: "6B", 11: "1B",
}
_CAMELOT_MINOR = {
    9: "8A", 10: "3A", 11: "10A", 0: "5A", 1: "12A", 2: "7A",
    3: "2A", 4: "9A", 5: "4A", 6: "11A", 7: "6A", 8: "1A",
}


@dataclass(frozen=True)
class Key:
    """A musical key: tonic pitch class (0-11) + mode (major/minor)."""

    tonic: int
    mode: str

    @property
    def name(self) -> str:
        """Human name like 'A minor' or 'C major' (prefers sharp spelling)."""
        return f"{_SHARP[self.tonic]} {self.mode}"

    @property
    def short(self) -> str:
        """Compact name like 'Am' or 'C'."""
        return f"{_SHARP[self.tonic]}{'m' if self.mode == 'minor' else ''}"

    @property
    def camelot(self) -> str:
        """Camelot wheel code (e.g. '8A' for A minor)."""
        table = _CAMELOT_MINOR if self.mode == "minor" else _CAMELOT_MAJOR
        return table.get(self.tonic, "?")

    @property
    def character(self) -> str:
        """A short vibe description of the mode."""
        return _MODE_CHARACTER.get(self.mode, "")


def _pitch_index(token: str) -> Optional[int]:
    """Resolve a note token like 'A', 'F#', 'Bb', 'Db' to a semitone index."""
    t = token.strip()
    if not t:
        return None
    low = t.lower()
    if low in _ALIASES:
        return _ALIASES[low]
    letter = t[0].upper()
    if letter not in "ABCDEFG":
        return None
    accidentals = t[1:]
    base = _SHARP.index(letter)
    for ch in accidentals:
        if ch in ("#", "\u266f"):
            base += 1
        elif ch in ("b", "B", "\u266d"):
            base -= 1
        else:
            return None
    return base % 12


def parse_key(text: str) -> Optional[Key]:
    """Parse a key label into a Key, or None if unrecognised.

    Accepts forms like 'Am', 'A minor', 'C', 'C major', 'F#m', 'Bb maj',
    'G#min', 'Dmaj'. Case- and spacing-insensitive.
    """
    if not text:
        return None
    s = text.strip()
    m = re.match(r"^([A-Ga-g])([#b\u266f\u266d]*)\s*(.*)$", s)
    if not m:
        return None
    note = m.group(1) + m.group(2)
    rest = m.group(3).strip().lower()
    idx = _pitch_index(note)
    if idx is None:
        return None
    # Determine mode from the remainder.
    if rest in ("m", "min", "minor", "-", "aeolian"):
        mode = "minor"
    elif rest in ("", "maj", "major", "+", "ionian"):
        mode = "major"
    elif rest.startswith("min"):
        mode = "minor"
    elif rest.startswith("maj"):
        mode = "major"
    else:
        return None
    return Key(idx, mode)


def relative_key(key: Key) -> Key:
    """Return the relative key (major<->minor sharing a key signature).

    C major -> A minor; A minor -> C major. The relative minor is 3 semitones
    below the major tonic.
    """
    if key.mode == "major":
        return Key((key.tonic - 3) % 12, "minor")
    return Key((key.tonic + 3) % 12, "major")


def parallel_key(key: Key) -> Key:
    """Return the parallel key (same tonic, opposite mode). A minor -> A major."""
    other = "minor" if key.mode == "major" else "major"
    return Key(key.tonic, other)


def keys_equivalent(a: Key, b: Key) -> bool:
    """True if two keys are the same OR relative to each other.

    Relatives share the same notes/key signature, so a detector reporting A
    minor and a website reporting C major are describing the same tonality.
    """
    return a == b or relative_key(a) == b


@dataclass(frozen=True)
class KeyReconciliation:
    """Outcome of reconciling a detected key with an online/reference key."""

    detected: Optional[Key]
    reference: Optional[Key]
    agree: bool            # same or relative
    relation: str          # exact | relative | parallel | conflict | partial | unknown
    resolved: Optional[Key]  # the key we choose to trust
    note: str


def reconcile_key(
    detected: Optional[Key],
    reference: Optional[Key],
    *,
    prefer_reference: bool = True,
) -> KeyReconciliation:
    """Reconcile a detected key against an online/reference key.

    Music theory is not rocket science: relatives (Am/C) are the same tonality,
    parallels (Am/A) share a tonic. We classify the relationship and pick a
    resolved key, preferring the human reference on a relative/exact match
    (sheet-music sources are usually the notated key).
    """
    if detected is None and reference is None:
        return KeyReconciliation(None, None, False, "unknown", None,
                                 "no key from either source")
    if reference is None:
        return KeyReconciliation(detected, None, False, "partial", detected,
                                 "only a detected key is available")
    if detected is None:
        return KeyReconciliation(None, reference, False, "partial", reference,
                                 "only a reference key is available")

    if detected == reference:
        return KeyReconciliation(detected, reference, True, "exact", reference,
                                 f"both agree on {reference.name}")
    if relative_key(detected) == reference:
        chosen = reference if prefer_reference else detected
        return KeyReconciliation(
            detected, reference, True, "relative", chosen,
            f"{detected.name} and {reference.name} are relative keys "
            f"(same notes); using {chosen.name}",
        )
    if parallel_key(detected) == reference:
        chosen = reference if prefer_reference else detected
        return KeyReconciliation(
            detected, reference, False, "parallel", chosen,
            f"{detected.name} vs {reference.name} share a tonic but differ in "
            f"mode; using {chosen.name}",
        )
    return KeyReconciliation(
        detected, reference, False, "conflict",
        reference if prefer_reference else detected,
        f"{detected.name} (detected) conflicts with {reference.name} (reference)",
    )


def compatible_keys(key: Key) -> list[Key]:
    """Harmonically-compatible keys (Camelot neighbours + relative/parallel).

    A small, creative "what mixes/modulates well" set: the relative key, the
    parallel key, and the dominant/subdominant of the same mode.
    """
    out = [
        relative_key(key),
        parallel_key(key),
        Key((key.tonic + 7) % 12, key.mode),  # dominant (up a fifth)
        Key((key.tonic + 5) % 12, key.mode),  # subdominant (down a fifth)
    ]
    seen: set[Key] = {key}
    unique: list[Key] = []
    for k in out:
        if k not in seen:
            seen.add(k)
            unique.append(k)
    return unique
