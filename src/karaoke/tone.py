"""What the words are like, as a second axis beside the sound.

Genre lives in the audio. This is the other half: attitude, which lives in the
text and which the audio cannot see. Together they say things neither says
alone -- "cynical pop" is a real description, and a track can sound cheerful
while its words are bleak.

**Only tone, deliberately.** Classifying the *genre* of lyrics was tried and
measured at 5% agreement with the audio genre across 83 tracks, with results
like Will Smith's "Miami" reading as psychedelic rock and Wet Leg's "Chaise
Longue" as hip hop at 0.194. That is not informative disagreement, it is noise,
and the reason is structural: the words of a pop song and a punk song are not
distinguishable in genre terms, because genre is a property of sound. Tone is a
property of text, which is why this axis works where that one did not.

Uses the sentence embedding the library already computes for lyric search, so
no new model and no new dependency.

Two things bound what this can cover, and both are worth knowing before
trusting a label:

- **Instrumentals have no tone**, by definition -- and they are exactly the
  tracks where the audio axis matters most, so the two axes are least
  available together where they would be most useful.
- **A Whisper transcription has no tone worth reading.** Its words are a guess,
  so the attitude of that guess is meaningless; :func:`classify_lyrics` refuses
  those rather than describing a hallucination.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .logger import log

# Attitudes rather than emotions: the question is what stance the words take,
# which is what makes a compound like "cynical pop" say something the genre
# does not. Deliberately small -- a long list of near-synonyms splits the
# score between them and nothing wins by a margin.
# Six, not ten, and the difference was measured rather than judged. A first
# version added "bleak and hopeless", "wistful and nostalgic", "playful and
# absurd" and "political and protesting" -- and those are near-neighbours of
# the ones kept, so they split the vote rather than adding resolution. Over 40
# tracks:
#
#     ten labels   clear 20/40   median margin 0.0241   p90 0.0629
#     six labels   clear 24/40   median margin 0.0312   p90 0.1270
#
# Half again as many decisions, and twice the margin at the top end. Nothing
# was lost that the survivors do not absorb: bleak lands on sad, wistful
# between sad and tender, playful on joyful, political on angry.
TONES = (
    "cynical and sarcastic",
    "angry and confrontational",
    "tender and romantic",
    "sad and mournful",
    "joyful and celebratory",
    "defiant and rebellious",
)

# Short names for display. "cynical pop" reads; "cynical and sarcastic pop"
# does not.
SHORT = {
    "cynical and sarcastic": "cynical",
    "angry and confrontational": "angry",
    "tender and romantic": "tender",
    "sad and mournful": "sad",
    "joyful and celebratory": "joyful",
    "defiant and rebellious": "defiant",
}

PROMPT = "lyrics that are {tone}"

# Scores are compressed -- across one track's ten labels the range was 0.215 to
# 0.348 -- so an absolute floor is nearly useless and the margin does the work.
MIN_SCORE = 0.20

# How far ahead the winner must be. Measured: "You Know I'm No Good" scored
# 0.427 playful against 0.421 tender, which is a coin toss reported as a
# judgement, and it happens to be the case the classifier got wrong.
MIN_MARGIN = 0.02

# Shorter than this is a fragment or a chorus stub, not enough text to read an
# attitude from.
MIN_CHARS = 300


@dataclass(frozen=True)
class ToneVerdict:
    """The attitude of a lyric, and how clear the reading was."""

    tone: str
    score: float
    runner_up: str = ""
    runner_up_score: float = 0.0

    @property
    def margin(self) -> float:
        return self.score - self.runner_up_score

    @property
    def clear(self) -> bool:
        return self.margin >= MIN_MARGIN

    @property
    def short(self) -> str:
        """The one-word form, for compounding with a genre."""
        return SHORT.get(self.tone, self.tone.split()[0])


def label_vectors(tones: tuple[str, ...] = TONES) -> dict[str, list[float]]:
    """Embed each tone once, for reuse across a run."""
    from .embed import embed_text

    out: dict[str, list[float]] = {}
    for tone in tones:
        try:
            out[tone] = embed_text(PROMPT.format(tone=tone))
        except Exception:
            log.debug("could not embed tone %r", tone, exc_info=True)
    return out


def _unit(vector: list[float]) -> list[float]:
    norm = sum(v * v for v in vector) ** 0.5
    return [v / norm for v in vector] if norm else list(vector)


def classify(lyrics: str, labels: dict[str, list[float]]) -> Optional[ToneVerdict]:
    """The attitude of a lyric, or None when it cannot be read."""
    from .embed import embed_text

    text = (lyrics or "").strip()
    if len(text) < MIN_CHARS or not labels:
        return None
    try:
        vector = _unit(embed_text(text[:2000]))
    except Exception:
        log.debug("could not embed lyrics for tone", exc_info=True)
        return None

    scored = sorted(
        ((tone, sum(a * b for a, b in zip(vector, _unit(lv))))
         for tone, lv in labels.items()),
        key=lambda pair: pair[1], reverse=True)
    if not scored:
        return None
    tone, score = scored[0]
    if score < MIN_SCORE:
        return None
    runner, runner_score = scored[1] if len(scored) > 1 else ("", 0.0)
    return ToneVerdict(tone=tone, score=score,
                       runner_up=runner, runner_up_score=runner_score)


def classify_lyrics(lyrics: str, source: str,
                    labels: dict[str, list[float]]) -> Optional[ToneVerdict]:
    """Tone, but only for words worth reading.

    A Whisper transcription is a guess at what was sung, so the attitude of
    that guess describes the model rather than the song.
    """
    from .librarysearch import is_transcribed

    if is_transcribed(source):
        log.debug("not reading tone from transcribed words")
        return None
    return classify(lyrics, labels)


def compound(genre_label: str, verdict: Optional[ToneVerdict]) -> str:
    """A genre qualified by its words: "cynical pop".

    Only when the tone is clear. A coin-toss tone attached to a genre reads as
    a finding rather than the noise it is, and the genre alone is the more
    honest answer.
    """
    if not genre_label:
        return ""
    if verdict is None or not verdict.clear:
        return genre_label
    return f"{verdict.short} {genre_label}"
