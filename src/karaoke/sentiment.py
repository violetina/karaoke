"""Lightweight lexicon sentiment/mood detection for lyric lines.

Pure and dependency-free (no external NLP model): classify a single lyric line
into one of a few coarse moods so the renderer can tint it. This is a vibe cue,
not real affect analysis — LRCLIB gives us the text, we score it against small
hand-built word sets. Unit-tested; the renderer maps the mood to a Rich style.
"""
from __future__ import annotations

import re

# Coarse mood buckets. Order matters for ties: anger/tender are more specific
# signals than a generic pos/neg, so they win when counts tie (see mood_of).
_POSITIVE = {
    "happy", "happier", "happiness", "joy", "joyful", "smile", "smiling",
    "laugh", "laughing", "shine", "shining", "sunshine", "sunny", "bright",
    "beautiful", "wonderful", "good", "great", "alive", "free", "freedom",
    "dance", "dancing", "celebrate", "party", "glad", "hope", "hopeful",
    "dream", "dreams", "high", "fly", "flying", "gold", "golden", "win",
    "winning", "best", "sweet", "sweeter", "paradise", "heaven", "glory",
}
_NEGATIVE = {
    "sad", "sadness", "cry", "crying", "cried", "tears", "tear", "lonely",
    "alone", "lost", "lose", "losing", "broken", "break", "breaking", "hurt",
    "hurting", "pain", "painful", "dark", "darkness", "cold", "empty", "hollow",
    "fall", "falling", "fell", "down", "low", "blue", "grey", "gray", "rain",
    "storm", "goodbye", "gone", "leave", "leaving", "left", "die", "dying",
    "dead", "death", "sorrow", "grief", "regret", "fear", "afraid", "shadow",
    "drown", "drowning", "fade", "fading", "numb", "silence", "nothing",
}
_ANGER = {
    "hate", "hatred", "rage", "angry", "anger", "mad", "fight", "fighting",
    "war", "burn", "burning", "fire", "blood", "kill", "killing", "scream",
    "screaming", "revenge", "enemy", "enemies", "destroy", "smash", "break",
    "wrath", "fury", "furious", "riot", "violence", "violent", "damn", "hell",
}
_TENDER = {
    "love", "loving", "loved", "lover", "beloved", "heart", "hearts", "kiss",
    "kissing", "hold", "holding", "embrace", "touch", "gentle", "tender",
    "warm", "warmth", "close", "darling", "baby", "honey", "dear", "sweetheart",
    "forever", "always", "care", "caring", "soul", "soulmate", "angel",
}

_WORD_RE = re.compile(r"[a-z']+")

# All valid moods; "neutral" is the fallback used for the intro / no clear signal.
MOODS = ("happy", "sad", "angry", "tender", "neutral")


def score_line(text: str) -> dict[str, int]:
    """Count mood-word hits in `text` (case-insensitive whole words)."""
    words = _WORD_RE.findall((text or "").lower())
    wset = words  # list; a word repeated counts repeatedly (intensity)
    return {
        "happy": sum(w in _POSITIVE for w in wset),
        "sad": sum(w in _NEGATIVE for w in wset),
        "angry": sum(w in _ANGER for w in wset),
        "tender": sum(w in _TENDER for w in wset),
    }


def mood_of(text: str) -> str:
    """Classify a lyric line into one mood in MOODS.

    Returns "neutral" when there's no signal. On ties the more specific buckets
    (angry, tender) beat the generic happy/sad, then happy beats sad, so a line
    with equal positive/negative words leans upbeat rather than bleak.
    """
    s = score_line(text)
    if not any(s.values()):
        return "neutral"
    # Priority for tie-breaking: specific emotions first, then valence.
    priority = ("angry", "tender", "happy", "sad")
    best = max(priority, key=lambda k: (s[k], -priority.index(k)))
    return best if s[best] > 0 else "neutral"
