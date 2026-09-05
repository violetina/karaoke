"""Zero-shot genre labelling, from the CLAP embedding a track already has.

essentia's Discogs-EffNet models are the conventional answer and they need
TensorFlow, which will not install here: upstream ships no cp314 wheels and the
ebuild's ``libclang`` dependency does not exist in any repo. CLAP sidesteps that
entirely -- audio and text share a space, so classifying is embedding the
candidate labels and taking the nearest, with no model zoo and no taxonomy
download.

**The label set is the design.** A missing label does not produce "unknown", it
produces the nearest wrong answer: Modjo's *Lady* came back as "pop" purely
because "french house" was not offered. So :data:`GENRES` is chosen against
what this library actually holds -- heavy on rock and its subgenres, because
Red Hot Chili Peppers, The Slits, Dinosaur Jr., Sonic Youth, Primus, Kyuss and
Nirvana are its spine -- and wide enough elsewhere that a jazz or hip hop track
is not forced into a rock label.

Scores are relative rankings, not calibrated probabilities, so two guards
decide when to keep quiet:

- an absolute floor, below which nothing looked like anything;
- a margin over the runner-up, because "heavy metal 0.708, punk rock 0.689" is
  a genuine ambiguity and picking the first would misrepresent it.

Both mean :func:`classify` returns None rather than guessing, the same
discipline as :mod:`karaoke.lyric_language`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from .logger import log

# Chosen against this library's contents. Rock is deliberately fine-grained
# because most of the library is rock and "rock" alone would be useless; the
# rest is broad enough that a non-rock track has somewhere to land.
GENRES = (
    # rock and its neighbours
    "alternative rock", "punk rock", "post-punk", "grunge", "noise rock",
    "heavy metal", "stoner rock", "psychedelic rock", "progressive rock",
    "hard rock", "indie rock", "funk rock", "garage rock", "shoegaze",
    "post-rock", "hardcore punk",
    # everything else
    "folk", "blues", "country", "jazz", "soul", "funk", "reggae", "dub",
    "hip hop", "trip hop", "house", "techno", "drum and bass", "ambient",
    "synth pop", "pop", "classical", "spoken word",
)

# CLAP was trained on captions, so a sentence scores better than a bare word.
PROMPT = "This audio is a {genre} song."

# Below this, nothing resembled anything: the track is instrumental noise, a
# spoken interlude, or simply unlike every label offered. Set from the observed
# range -- labelling 120 cached tracks produced winners between 0.517 and
# 0.708, so 0.52 leaves the weakest unlabelled rather than filed under a label
# they barely touch.
MIN_SCORE = 0.52

# Labels that absorb uncertainty. Measured: "pop" won 39 of 120 tracks and
# "punk rock" 29, far beyond what the library contains, because both sit close
# to a great deal of music. A win on one of these with a thin margin is much
# weaker evidence than the same margin on a specific label, and callers should
# treat it as "probably rock, unsure which" rather than as a genre.
#
# Correcting this by subtracting each label's mean similarity was tried and
# rejected: it assumes a uniform prior over genres, and this library really is
# mostly rock, so it penalised the correct majority label. Modjo's "Lady" moved
# from "pop" to "house" (right), but Portishead moved from "trip hop" to "hip
# hop" and Will Smith from "hip hop" to "reggae" (both wrong). A healthier
# distribution is not the same as better answers.
BROAD_LABELS = frozenset({"pop", "punk rock", "alternative rock", "hard rock"})

# How far ahead the winner must be. Measured on real output: Melvins came back
# "heavy metal 0.708, punk rock 0.689", which is a real ambiguity for sludge
# rather than a clear answer, and reporting only the first would hide that.
MIN_MARGIN = 0.02


@dataclass(frozen=True)
class GenreVerdict:
    """A label, its runner-up, and how clear the decision was."""

    genre: str
    score: float
    runner_up: str = ""
    runner_up_score: float = 0.0

    @property
    def margin(self) -> float:
        return self.score - self.runner_up_score

    @property
    def clear(self) -> bool:
        """Whether the winner stood apart from the runner-up."""
        return self.margin >= MIN_MARGIN

    @property
    def broad(self) -> bool:
        """Whether the label is one that absorbs uncertain tracks."""
        return self.genre in BROAD_LABELS

    @property
    def confident(self) -> bool:
        """A clear win on a specific label.

        A thin win on "pop" is not the same claim as a clear win on "trip hop",
        and flattening the two into one label loses the difference.
        """
        return self.clear and not self.broad


def label_vectors(genres: tuple[str, ...] = GENRES) -> dict[str, list[float]]:
    """Embed each label once, for reuse across a whole run."""
    from . import clap_vector

    out: dict[str, list[float]] = {}
    for genre in genres:
        vector = clap_vector.embed_text(PROMPT.format(genre=genre))
        if vector is not None:
            out[genre] = vector
    return out


def rank(track_vector: list[float],
         labels: dict[str, list[float]]) -> list[tuple[str, float]]:
    """Every label scored against one track, best first."""
    if not track_vector or not labels:
        return []
    scored = [(genre, sum(a * b for a, b in zip(track_vector, vector)))
              for genre, vector in labels.items()]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored


def classify(track_vector: list[float],
             labels: dict[str, list[float]]) -> Optional[GenreVerdict]:
    """The best label for a track, or None when nothing convincingly fits.

    None is a real answer: a track that resembles no offered label is better
    left unlabelled than filed under whichever happened to be nearest.
    """
    scored = rank(track_vector, labels)
    if not scored:
        return None
    genre, score = scored[0]
    if score < MIN_SCORE:
        log.debug("no genre above %.2f (best %s at %.3f)", MIN_SCORE, genre, score)
        return None
    runner, runner_score = scored[1] if len(scored) > 1 else ("", 0.0)
    return GenreVerdict(genre=genre, score=score,
                        runner_up=runner, runner_up_score=runner_score)


def describe(verdict: Optional[GenreVerdict]) -> str:
    """One line for a log or a CLI."""
    if verdict is None:
        return "unlabelled"
    if verdict.clear:
        return f"{verdict.genre} ({verdict.score:.3f})"
    return (f"{verdict.genre} ({verdict.score:.3f}), "
            f"close to {verdict.runner_up} ({verdict.runner_up_score:.3f})")
