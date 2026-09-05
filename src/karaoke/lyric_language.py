"""Work out what language a lyric is in, so Whisper is not left guessing.

``transcribe_to_words`` accepts a ``language`` and nothing in the codebase ever
passed one, so faster-whisper detected it from the opening of the audio -- which
on music is regularly an instrumental intro with nothing to detect from. The
result is visible in the library: Einstürzende Neubauten's stored words are
German, Polish and English fused into one line, which is not a transcription of
noise but a detector changing its mind.

When the real lyrics are in hand -- which is exactly when alignment runs -- the
language is knowable from the text, and telling Whisper costs nothing.

A wrong language is worse than none, because it forces the model into a
vocabulary that cannot match. Dutch is the sharp case: Whisper's most plausible
wrong answer for Dutch is German, close enough to produce confident, well-formed,
useless words, which then anchor nothing at all because
:func:`difflib.SequenceMatcher` matches exact tokens. So this returns None
whenever the evidence is thin, and the caller keeps the old behaviour.

Stopwords rather than a dependency: function words are the most frequent tokens
in any text, they are short and closed-class, and a lyric is long enough for
their frequencies to separate cleanly. No model, no download, no new package.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Optional

_WORD = re.compile(r"[a-zà-öø-ÿœæß']+")

# Function words per language. Deliberately small: only what is frequent enough
# to show up in a few lines of song, and chosen so that the overlap between
# neighbouring languages is visible rather than hidden -- see DISTINCTIVE below,
# which is what actually decides.
STOPWORDS: dict[str, frozenset[str]] = {
    "en": frozenset("""
        the and you that was for are with his they this have from one had not
        but what all were when your there been would their she him been has
        into more some could them than then now only over also back after""".split()),
    "nl": frozenset("""
        de het een en van ik je niet dat is op te met voor zijn maar ook naar
        wij aan er om hem haar hun deze die dan nog wel geen door bij onder
        altijd nooit alles iets niets mensen algemeen""".split()),
    "de": frozenset("""
        der die das und ich nicht ein zu mit auf für dem den sich aber auch
        ist sind war wenn nur noch schon wie was wir ihr sie es im am vom
        über unter immer nie alles nichts""".split()),
    "fr": frozenset("""
        le la les des une un et est dans que qui pour pas plus avec sur ce
        cette nous vous ils elle son sa ses mais tout tous rien jamais
        toujours comme quand""".split()),
    "es": frozenset("""
        el la los las una uno y es en que de por para con no se su sus pero
        todo todos nada nunca siempre como cuando muy más este esta ese
        porque tiene""".split()),
}

# Function words in one language that are ordinary *content* words in another,
# and therefore prove nothing. Measured against the library rather than
# imagined: "Gimme Shelter" was called German on five occurrences of "war"
# ("War, children, it's just a shot away") and "Blood Rag" was called Spanish
# on twenty of "no". A word only earns a place in DISTINCTIVE if seeing it is
# actually evidence.
# Every entry is a word that is ordinary English; nothing is excluded merely
# for looking foreign. An earlier version also dropped "la", "el", "der" and
# "tiene", which are not English at all -- and that cost a true positive, since
# Los Natas' Spanish lyric then had too few Spanish words left to name.
# Over-pruning is not the safe direction: it just moves the error.
HOMOGRAPHS = frozenset("""
    war no is was so me be in an on do we he it at or if up us am
    die son van
""".split())

# Words that belong to exactly one of the languages above, minus the traps.
# The shared ones ("die" is both German and Dutch, "en" is both Dutch and
# French) say almost nothing and would otherwise let a neighbouring language
# win on borrowed evidence.
DISTINCTIVE: dict[str, frozenset[str]] = {
    code: frozenset(
        word for word in words
        if word not in HOMOGRAPHS
        and not any(word in other for name, other in STOPWORDS.items() if name != code)
    )
    for code, words in STOPWORDS.items()
}

# Below this share of distinctive hits there is not enough evidence to name a
# language, and guessing one is worse than leaving Whisper to detect it.
MIN_DISTINCTIVE_RATE = 0.02

# How far ahead the winner must be. Dutch and German share so much that a
# narrow win is not a win, and choosing German for a Dutch lyric produces
# fluent nonsense that anchors nothing.
MIN_MARGIN = 1.5

# Fewer tokens than this is a fragment, not a lyric.
MIN_TOKENS = 12

# How many *different* function words must appear. One word repeated is not
# evidence of a language, however often it repeats: before this, five "war"s
# made an English song German and twenty "no"s made another one Spanish. A
# lyric genuinely in a language uses several of its function words.
MIN_DISTINCT_TYPES = 3


def tokenize(text: str) -> list[str]:
    """Lowercased word tokens, apostrophes kept ("don't", "'t")."""
    return _WORD.findall((text or "").casefold())


def score(text: str) -> dict[str, float]:
    """Distinctive-stopword rate per language, highest first when read."""
    tokens = tokenize(text)
    if not tokens:
        return {}
    counts = Counter(tokens)
    total = len(tokens)
    return {code: sum(counts[w] for w in words) / total
            for code, words in DISTINCTIVE.items()}


def detect(text: str) -> Optional[str]:
    """The lyric's language as a Whisper code, or None when unsure.

    None is a real answer and the common safe one: it leaves the existing
    behaviour untouched, where naming the wrong language actively destroys the
    transcription.
    """
    tokens = tokenize(text)
    if len(tokens) < MIN_TOKENS:
        return None

    rates = score(text)
    if not rates:
        return None
    ranked = sorted(rates.items(), key=lambda kv: kv[1], reverse=True)
    best, best_rate = ranked[0]
    if best_rate < MIN_DISTINCTIVE_RATE:
        return None
    if len(set(tokens) & DISTINCTIVE[best]) < MIN_DISTINCT_TYPES:
        return None

    runner_rate = ranked[1][1] if len(ranked) > 1 else 0.0
    if runner_rate > 0 and best_rate < runner_rate * MIN_MARGIN:
        return None
    return best


def describe(text: str) -> str:
    """One line explaining the verdict, for a log or a CLI."""
    rates = score(text)
    if not rates:
        return "no tokens"
    ranked = sorted(rates.items(), key=lambda kv: kv[1], reverse=True)[:3]
    detail = "  ".join(f"{code} {rate:.3f}" for code, rate in ranked)
    return f"{detect(text) or 'unsure'}  ({detail})"
