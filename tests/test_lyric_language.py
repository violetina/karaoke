"""Naming a lyric's language so Whisper is not left to guess it.

Nothing passed `language` to faster-whisper, so it detected from the opening of
the audio -- often an instrumental intro. Einstürzende Neubauten's stored words
are German, Polish and English fused into one line, which is a detector
changing its mind rather than a transcription of noise.

The asymmetry that shapes every decision here: a wrong language is *worse* than
none. It forces the model into a vocabulary that cannot match, and because
SequenceMatcher anchors on exact tokens, a German-looking transcription of Dutch
singing anchors nothing and every line falls back to interpolation. So "unsure"
is a real answer and the safe one.
"""
from __future__ import annotations

import pytest

from karaoke import lyric_language as ll


DUTCH = """
    Mensen in het algemeen zijn niet zo moeilijk te begrijpen
    ik weet niet wat ik moet zeggen maar het is wel zo
    en dan loop je door de straten van de stad
    met een hoofd vol dingen die je niet kan zeggen
    alles wat er is en alles wat er niet meer is
"""

GERMAN = """
    Ich weiß nicht was ich sagen soll und das ist auch gut so
    wir gehen durch die Straßen und die Nacht ist noch nicht vorbei
    mit einem Kopf voll Dinge die man nicht sagen kann
    alles was da ist und alles was nicht mehr da ist
"""

ENGLISH = """
    I don't know what I'm doing here and that is all there is
    they were walking down the street with all their things
    what would you have done when all the lights went out
    there is nothing more to say and nothing left to do
"""

FRENCH = """
    Je ne sais pas ce que je dois dire et c'est comme ça
    nous marchons dans les rues de la ville qui dort
    avec une tête pleine de choses que l'on ne peut pas dire
    tout ce qui est là et tout ce qui n'est plus
"""


# --- the languages that matter --------------------------------------------

def test_dutch_is_recognised():
    assert ll.detect(DUTCH) == "nl"


def test_german_is_recognised():
    assert ll.detect(GERMAN) == "de"


def test_dutch_is_not_mistaken_for_german():
    """The whole point. German is Whisper's most plausible wrong answer for
    Dutch, and it is wrong in the most damaging way: fluent and unanchorable."""
    assert ll.detect(DUTCH) != "de"
    assert ll.detect(GERMAN) != "nl"


def test_english_is_recognised():
    assert ll.detect(ENGLISH) == "en"


def test_french_is_recognised():
    assert ll.detect(FRENCH) == "fr"


# --- refusing to guess -----------------------------------------------------

def test_a_fragment_is_not_guessed_at():
    """Two words carry no evidence, and a wrong answer costs more than none."""
    assert ll.detect("Feel feel feel") is None


def test_empty_text_is_unsure():
    assert ll.detect("") is None
    assert ll.detect("   \n  ") is None


def test_a_lyric_of_only_content_words_is_unsure():
    """No function words, so nothing to measure -- GAUPA's lyric is like this."""
    text = "shelter shelter running hands running hands feel shelter " \
           "running feel hands shelter running feel"
    assert ll.detect(text) is None


def test_a_narrow_win_is_no_win():
    """Dutch and German share too much for a slim margin to mean anything."""
    mixed = DUTCH.split("\n")[1] + GERMAN.split("\n")[1]
    # Not asserting a specific answer: asserting it does not confidently pick
    # one when the evidence is split.
    rates = ll.score(mixed)
    ranked = sorted(rates.values(), reverse=True)
    if ranked[0] < ranked[1] * ll.MIN_MARGIN:
        assert ll.detect(mixed) is None


def test_whisper_artifacts_do_not_produce_a_language():
    """The Neubauten row: fused fragments from several languages at once."""
    text = "HerausforderDisobeylarationDisobeyedzyIt's the lawczywiscieDisobey not"
    assert ll.detect(text) in (None, "en")   # "the"/"not" are the only real words


# --- the shape of the rules ------------------------------------------------

def test_distinctive_words_belong_to_exactly_one_language():
    """Shared function words decide nothing; that is why they are excluded."""
    for code, words in ll.DISTINCTIVE.items():
        for other, other_words in ll.DISTINCTIVE.items():
            if other != code:
                assert not (words & other_words), f"{code} vs {other}"


def test_every_language_keeps_some_distinctive_words():
    """A language whose stopwords are all shared could never be chosen."""
    for code, words in ll.DISTINCTIVE.items():
        assert words, code


def test_the_codes_are_what_whisper_expects():
    assert set(ll.STOPWORDS) <= {"en", "nl", "de", "fr", "es", "it", "pt"}


def test_apostrophes_survive_tokenizing():
    """"don't" and "'t" are function words in two of these languages."""
    assert "don't" in ll.tokenize("Don't stop")


def test_describe_explains_the_verdict():
    text = ll.describe(DUTCH)
    assert text.startswith("nl")
    assert "nl " in text            # the rates are shown, not just the answer


def test_describe_says_unsure_rather_than_inventing():
    assert ll.describe("Feel feel feel").startswith("unsure")


# --- the traps that real lyrics contain ------------------------------------

def test_one_repeated_word_does_not_name_a_language():
    """Five "war"s made "Gimme Shelter" German before this.

    "War, children, it's just a shot away" -- a German function word that is an
    ordinary English noun, repeated in a chorus.
    """
    text = ("war children it's just a shot away " * 5) + \
           "rape murder it's just a shot away love sister it's just a kiss away"
    assert ll.detect(text) != "de"


def test_english_no_does_not_make_a_lyric_spanish():
    """Twenty "no"s made "Blood Rag" Spanish."""
    text = ("no no no no " * 10) + "I don't want to be the one that you are with"
    assert ll.detect(text) != "es"


def test_homographs_are_english_words_only():
    """Nothing is excluded merely for looking foreign.

    Dropping "la", "el" and "tiene" once cost a true positive: Los Natas'
    Spanish lyric had too few Spanish words left to name.
    """
    import re
    for word in ll.HOMOGRAPHS:
        assert re.fullmatch(r"[a-z']+", word), word
    assert "la" not in ll.HOMOGRAPHS
    assert "el" not in ll.HOMOGRAPHS


def test_a_real_spanish_lyric_still_resolves():
    text = ("y el sol de la mañana no me deja ver " 
            "porque todo lo que tiene es para ti "
            "cuando la noche viene con su luz muy lejos de aqui")
    assert ll.detect(text) == "es"


def test_several_different_function_words_are_required():
    assert ll.MIN_DISTINCT_TYPES >= 3
