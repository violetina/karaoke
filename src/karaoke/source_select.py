"""Pick which YouTube result to align lyrics against.

Backfill used to take the first search hit unchecked, which is how a 71-minute
``Peggy Lee - Fever (Full Album)`` upload or a ``(Guitar Cover)`` could end up
as the audio a song's lyrics were transcribed from.

Selection is deliberately conservative:

- A candidate whose length disagrees with the lyrics source is **rejected**, not
  merely down-ranked. Wrong-length audio cannot produce usable timings.
- Everything surviving that is **scored**, preferring official audio.

``<Artist> - Topic`` channels are YouTube's auto-generated uploads of the
YouTube Music catalogue: the official master, with no video intro or outro to
push every timestamp late. They rank highest. (Note the inversion — "- Topic"
is noise in an *artist* field, but a strong positive signal in an *uploader*.)

Everything here is a pure function over already-fetched results, so it is
testable without touching the network.
"""
from __future__ import annotations

import re
from typing import Optional

# Duration tolerance. Releases legitimately differ by a few seconds between
# masters, and flat-extraction durations round; ±15s is loose enough for that
# while still rejecting a live cut, a radio edit or an album rip.
DURATION_TOLERANCE_S = 15.0

_TOPIC_UPLOADER = re.compile(r"\s-\s*topic\s*$", re.IGNORECASE)

# Official music videos. Not a *wrong* recording the way a cover or a live cut
# is — it is usually the same master — but YouTube Music plays a video id in
# video mode, and the video edit routinely carries a cold open, a spoken intro
# or a tacked-on outro that the audio release does not. Every timestamp after
# that shifts, and the shift differs between the video and audio versions of the
# same song, so a sync offset tuned against one is wrong against the other.
#
# Down-ranked rather than rejected: for plenty of songs the video is the only
# upload there is, and a shifted-but-real source beats no source. The penalty is
# smaller than the artist-channel bonus on purpose, so an official video still
# beats an anonymous re-upload of the audio — those are frequently the wrong
# master, and a wrong master cannot be corrected by an offset at all.
_MUSIC_VIDEO = re.compile(
    r"\b(?:official\s+(?:music\s+)?video|music\s+video|official\s+clip|"
    r"videoclip|video\s+oficial)\b",
    re.IGNORECASE,
)

# Markers of a recording that is not the studio original. Lyrics still exist for
# these, but the timings will not line up with the released version.
_NON_STUDIO = re.compile(
    r"\b(?:cover|covered|karaoke|instrumental|backing\s*track|live|remix|"
    r"reaction|tribute|lesson|tutorial|guitar\s*cover|drum\s*cover|"
    r"bass\s*cover|8\s*bit|sped\s*up|slowed|nightcore|full\s+album)\b",
    re.IGNORECASE,
)


def _tokens(text: str) -> set[str]:
    """Lowercase word tokens of length > 2, for loose comparison."""
    return {w for w in re.split(r"[^a-z0-9]+", (text or "").casefold()) if len(w) > 2}


def duration_ok(candidate_s: Optional[float], reference_s: Optional[float]) -> bool:
    """Whether a candidate's length is close enough to the reference.

    Unknown on either side is permitted — the check can only reject on evidence,
    never on absence of it, so behaviour is unchanged when nothing is known.
    """
    if candidate_s is None or reference_s is None:
        return True
    return abs(float(candidate_s) - float(reference_s)) <= DURATION_TOLERANCE_S


def score_candidate(candidate: dict, artist: str, title: str) -> float:
    """Score one search result. Higher is better; used only for ranking."""
    uploader = candidate.get("uploader") or ""
    cand_title = candidate.get("title") or ""
    score = 0.0

    # Official audio from YouTube's auto-generated artist channel.
    if _TOPIC_UPLOADER.search(uploader):
        score += 10.0

    # The artist's own channel ("Nirvana", "Peggy Lee").
    artist_tokens = _tokens(artist)
    if artist_tokens and artist_tokens & _tokens(uploader):
        score += 5.0

    # Title actually names the track.
    title_tokens = _tokens(title)
    if title_tokens:
        overlap = len(title_tokens & _tokens(cand_title)) / len(title_tokens)
        score += 4.0 * overlap

    # An explicitly official upload, when there is no Topic channel.
    if re.search(r"\bofficial\b", cand_title, re.IGNORECASE):
        score += 2.0

    # Not the studio original: lyrics may match, timings will not.
    if _NON_STUDIO.search(cand_title):
        score -= 8.0

    # Prefer the audio release over the music video: same song, but the video
    # edit's intro shifts every lyric timestamp. See _MUSIC_VIDEO.
    if _MUSIC_VIDEO.search(cand_title):
        score -= 3.0

    return score


def select_best_source(
    candidates: list[dict],
    artist: str,
    title: str,
    reference_duration: Optional[float] = None,
) -> Optional[dict]:
    """Return the best candidate to download, or None if none is acceptable.

    Rejects wrong-length candidates outright, then returns the highest scorer.
    Returning None is meaningful: better no audio than the wrong audio, since a
    bad pick is transcribed and stored as if it were the song.
    """
    usable = [c for c in candidates
              if c.get("url") and duration_ok(c.get("duration"), reference_duration)]
    if not usable:
        return None
    # max() is stable, so an equal score keeps YouTube's own relevance order.
    return max(usable, key=lambda c: score_candidate(c, artist, title))
