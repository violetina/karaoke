"""Read the lyrics YouTube Music is already showing.

A track can be playing with its full lyrics on screen — the SONGTEKST tab,
attributed to LyricFind — while the library reports "no lyrics". LRCLIB does
not have every song, and YouTube captions are a different thing entirely
(a video's subtitle track, not the lyrics panel), so nothing in the pipeline
ever looked at what was in front of the user.

This reads that panel over the CDP connection the playback window already has
open for navigation. No new dependency, no API key, no scraping of anything
that is not already on screen.

Two limits worth stating plainly:

- **It only sees the track that is playing.** This is the open tab, not a
  service, so it cannot backfill a library on demand — it captures what is in
  front of you, when it is in front of you.
- **The text is unsynced.** LyricFind supplies words, not timings. That makes
  alignment against a transcription (:mod:`karaoke.lyric_align`) not an
  enhancement but the whole point: words from here, rhythm from Whisper.

The result is verified against the playing track before it is stored. The panel
lags a track change by a moment, and attributing one song's words to another is
the kind of mistake that is invisible afterwards.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

from .logger import log

# YouTube Music renders the lyrics tab as a description shelf. The attribution
# line ("Bron: LyricFind" / "Source: LyricFind") sits in a sibling element, so
# the body is read separately rather than stripped out of the whole shelf.
_LYRICS_JS = r"""(() => {
  const shelf = document.querySelector('ytmusic-description-shelf-renderer');
  if (!shelf) return JSON.stringify({present: false});
  const body = shelf.querySelector('div');
  const notes = Array.from(shelf.querySelectorAll('yt-formatted-string'))
                     .map(e => e.innerText || '')
                     .filter(t => /bron|source/i.test(t));
  return JSON.stringify({
    present: true,
    text: body ? (body.innerText || '') : '',
    attribution: notes[0] || '',
    url: location.href
  });
})()"""

# A lyric panel with fewer real lines than this is a placeholder, an error
# message, or the tab still loading.
MIN_LINES = 4

# The same description shelf renders the *artist biography* when a track has no
# lyrics, and it was being stored as one: a Wikipedia paragraph about the band,
# complete with a view count, saved against four tracks. Prose is easy to tell
# from lyrics by line length -- a sung line is short, a paragraph is not.
MAX_MEAN_LINE_CHARS = 90

# Real lyrics carry a provider ("Bron: LyricFind"). The biography carries none,
# which is the cheapest and most reliable signal that this is not a lyric tab.
# Prose gives itself away too.
_PROSE_MARKER = re.compile(
    r"weergaven|\bviews\b|van wikipedia|from wikipedia|creative commons|"
    r"https?://|\bCC-BY\b",
    re.IGNORECASE)

# The attribution sits inside the same element as the words, so it arrives as a
# final "line" and would otherwise be sung, timed and stored as lyrics.
_ATTRIBUTION_LINE = re.compile(r"^\s*(?:bron|source|quelle|fuente)\s*[:\-]",
                               re.IGNORECASE)


def _undouble(lines: list[str]) -> list[str]:
    """Collapse a line list that repeats itself wholesale.

    The panel can be read mid-render and yield the lyrics twice over. The
    result looks complete -- and the doubling is invisible in the text -- but
    only the first copy is ever sung, so the second is left unanchored and gets
    crammed into the closing seconds. Observed on a 21-line song stored as 42.

    Only an *exact* whole-list repetition is collapsed. Songs legitimately
    repeat a chorus, so anything short of the entire lyric appearing twice is
    left alone.
    """
    n = len(lines)
    for parts in (2, 3):
        if n >= parts * 2 and n % parts == 0:
            chunk = n // parts
            first = lines[:chunk]
            if all(lines[i * chunk:(i + 1) * chunk] == first
                   for i in range(1, parts)):
                return first
    return lines


@dataclass(frozen=True)
class PanelLyrics:
    """Lyrics scraped from the player's own lyrics tab."""

    text: str
    attribution: str = ""
    url: str = ""

    @property
    def lines(self) -> list[str]:
        """The lyric lines, without the provider attribution.

        "Bron: LyricFind" is rendered inside the same element as the words, so
        without this it becomes the last line of every song -- timed, stored,
        and displayed as something to sing.
        """
        kept = [line for line in self.text.splitlines()
                if line.strip() and not _ATTRIBUTION_LINE.match(line)]
        return _undouble(kept)

    @property
    def source_name(self) -> str:
        """Provider, lowercased — "lyricfind" for the usual case."""
        match = re.search(r"(?:bron|source)\s*[:\-]\s*(.+)$",
                          self.attribution or "", re.IGNORECASE)
        return (match.group(1).strip().lower() if match else "").replace(" ", "")


def read_panel() -> Optional[PanelLyrics]:
    """Read the lyrics tab of the playback window, or None."""
    from .player_open import _cdp_send

    reply = _cdp_send("Runtime.evaluate",
                      {"expression": _LYRICS_JS, "returnByValue": True})
    if not reply:
        return None
    try:
        data = json.loads(reply["result"]["result"]["value"])
    except (KeyError, TypeError, ValueError):
        return None
    if not data.get("present") or not (data.get("text") or "").strip():
        return None
    return PanelLyrics(text=data["text"], attribution=data.get("attribution", ""),
                       url=data.get("url", ""))


def looks_like_prose(lines: list[str]) -> bool:
    """Whether these lines are a paragraph rather than a lyric.

    The artist biography renders in the same shelf as the lyrics, so "the tab
    exists and has text" is not enough. Sung lines are short; a Wikipedia
    paragraph averages hundreds of characters and mentions its own source.
    """
    if not lines:
        return False
    mean = sum(len(ln) for ln in lines) / len(lines)
    if mean > MAX_MEAN_LINE_CHARS:
        return True
    return any(_PROSE_MARKER.search(ln) for ln in lines)


def usable(panel: Optional[PanelLyrics]) -> bool:
    """Whether a panel holds enough to be worth storing.

    An attribution is required. Real lyrics always name their provider, and
    its absence is what distinguished the biography that was stored against
    four tracks from the lyrics that were not there.
    """
    if not panel or len(panel.lines) < MIN_LINES:
        return False
    if not panel.source_name:
        log.debug("lyrics panel has no provider; not lyrics")
        return False
    return not looks_like_prose(panel.lines)


def lyrics_source(panel: PanelLyrics) -> str:
    """The ``lyrics.source`` value to record.

    Names the provider rather than the mechanism: "lyricfind" says where the
    words came from, which is what matters when judging them later, while how
    they were obtained is an implementation detail that would age badly.
    """
    provider = panel.source_name or "unknown"
    return f"ytmusic_panel_{provider}"


def for_playing(artist: str, title: str) -> Optional[PanelLyrics]:
    """Panel lyrics, but only if the player really is on this track.

    The panel lags a track change by a moment. Attributing one song's words to
    another is invisible once stored, so the player is asked what it is playing
    and the two must agree.
    """
    from . import detect, playerctl

    panel = read_panel()
    if not usable(panel):
        return None
    meta = playerctl.current_metadata()
    if meta is None:
        return None
    ref = playerctl.normalize_player_track(meta.artist, meta.title,
                                           meta.album, meta.url)
    if not detect.same_track(ref.artist, ref.title, artist, title):
        log.debug("lyrics panel shows %r - %r, not %r - %r",
                  ref.artist, ref.title, artist, title)
        return None
    return panel
