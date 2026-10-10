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
    # Dutch & commentary additions:
    "lachen", "gelach", "grappig", "lol", "grapje", "grapjes", "humor", "cheer",
    "feest", "feesten", "mooi", "prachtig", "geweldig", "blij", "blijdschap",
    "genieten", "super", "top", "fantastisch", "winnen", "winnaar", "juichen",
    "haha", "hahaha", "wow", "bizar", "ongelooflijk", "mooi", "subliem",
}
_NEGATIVE = {
    "sad", "sadness", "cry", "crying", "cried", "tears", "tear", "lonely",
    "alone", "lost", "lose", "losing", "broken", "break", "breaking", "hurt",
    "hurting", "pain", "painful", "dark", "darkness", "cold", "empty", "hollow",
    "fall", "falling", "fell", "down", "low", "blue", "grey", "gray", "rain",
    "storm", "goodbye", "gone", "leave", "leaving", "left", "die", "dying",
    "dead", "death", "sorrow", "grief", "regret", "fear", "afraid", "shadow",
    "drown", "drowning", "fade", "fading", "numb", "silence", "nothing",
    # Dutch & commentary additions:
    "verdriet", "huilen", "pijn", "jammer", "pech", "spijt", "donker", "koud",
    "alleen", "verloren", "verliezen", "moeilijk", "zwaar", "dood", "valpartij",
    "gevallen", "schade", "lekke", "probleem", "drama", "gevaar", "gevaarlijk",
}
_ANGER = {
    "hate", "hatred", "rage", "angry", "anger", "mad", "fight", "fighting",
    "war", "burn", "burning", "fire", "blood", "kill", "killing", "scream",
    "screaming", "revenge", "enemy", "enemies", "destroy", "smash", "break",
    "wrath", "fury", "furious", "riot", "violence", "violent", "damn", "hell",
    # Dutch & cross-talking / temperament additions:
    "boos", "kwaad", "woede", "ruzie", "oneens", "fout", "foutje", "onzin",
    "klopt", "niet", "wel", "discussie", "felle", "schreeuwen", "roepen",
    "stop", "ho", "homaar", "nee", "vechten", "strijd", "botsing", "boosheid",
}
_TENDER = {
    "love", "loving", "loved", "lover", "beloved", "heart", "hearts", "kiss",
    "kissing", "hold", "holding", "embrace", "touch", "gentle", "tender",
    "warm", "warmth", "close", "darling", "baby", "honey", "dear", "sweetheart",
    "forever", "always", "care", "caring", "soul", "soulmate", "angel",
    # Dutch additions:
    "liefde", "lief", "houden", "hart", "zoen", "kus", "warmte", "zacht",
    "rustig", "samen", "fijn", "dank", "bedankt", "vrienden", "vriend",
}
_CYNICAL = {
    "fake", "lying", "liar", "lies", "cheat", "cheating", "greedy", "money",
    "plastic", "cynical", "cynic", "bitter", "fool", "fools", "joke", "tricked",
    "trap", "trapped", "hollow", "sell", "sold", "puppet", "mask", "pretend",
    "sham", "game", "slaves", "waste", "useless", "hypocrite", "disguise",
    # Dutch additions:
    "nep", "leugen", "bedrog", "geld", "vies", "stom", "gekkigheid", "zot",
}
_NEGATORS = {"no", "not", "never", "don't", "dont", "cannot", "can't", "cant", "without", "hardly", "barely", "niet", "geen", "nooit"}

_WORD_RE = re.compile(r"[a-z']+")

# All valid moods; "neutral" is the fallback used for the intro / no clear signal.
MOODS = ("happy", "sad", "angry", "tender", "cynical", "neutral")


def score_line_contextual(text: str) -> dict[str, int]:
    """Count mood-word hits with contextual negation and cynicism detection."""
    words = _WORD_RE.findall((text or "").lower())
    s = {"happy": 0, "sad": 0, "angry": 0, "tender": 0, "cynical": 0}
    negated = False
    for i, w in enumerate(words):
        if w in _NEGATORS:
            negated = True
            continue
        
        # Check target bucket
        if w in _CYNICAL:
            s["cynical"] += 1
        elif w in _ANGER:
            s["angry"] += 1
        elif w in _TENDER:
            if negated:
                s["sad"] += 1  # e.g., "no love", "never tender" -> sad
            else:
                s["tender"] += 1
        elif w in _POSITIVE:
            if negated:
                s["cynical"] += 1 # e.g. "not happy", "never bright" -> cynical
            else:
                s["happy"] += 1
        elif w in _NEGATIVE:
            if negated:
                s["happy"] += 1  # e.g. "no tears", "not sad" -> happy
            else:
                s["sad"] += 1
        negated = False
    return s


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


def mood_of(text: str, rms: Optional[float] = None) -> str:
    """Classify a lyric line or speech snippet into one mood in MOODS.

    Returns "neutral" when there's no signal. On ties the more specific buckets
    (angry, tender) beat the generic happy/sad, then happy beats sad.
    """
    if not text:
        if rms and rms > 0.04:
            return "angry"
        elif rms and rms > 0.02:
            return "happy"
        return "neutral"

    words = _WORD_RE.findall((text or "").lower())
    s = score_line(text)

    # Exclamation or laughter indicators
    if "!" in text or any(w in ("haha", "hahaha", "lol", "wow", "bizar", "grapje") for w in words):
        s["happy"] += 2
    if any(w in ("niet", "wel", "fout", "onzin", "stop", "nee", "ruzie") for w in words) and ("?" in text or "!" in text):
        s["angry"] += 2

    if rms and rms > 0.04:
        s["angry"] += 2
    elif rms and rms > 0.02:
        s["happy"] += 1

    if not any(s.values()):
        return "neutral"
    # Priority for tie-breaking: specific emotions first, then valence.
    priority = ("angry", "tender", "happy", "sad")
    best = max(priority, key=lambda k: (s[k], -priority.index(k)))
    return best if s[best] > 0 else "neutral"
