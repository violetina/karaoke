"""LRCLIB synced-lyrics client and LRC parsing.

LRCLIB (https://lrclib.net) serves community synced lyrics for free with no API
key. `GET /api/get` matches on artist+track (+album+duration); on a miss we fall
back to `GET /api/search`.

LRC format lines look like: ``[mm:ss.xx] the lyric line``. A line may carry
several timestamps; each becomes its own (time, text) entry.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

import requests

from .config import settings

_TS = re.compile(r"\[(\d{1,2}):(\d{2})(?:[.:](\d{1,3}))?\]")
# Enhanced LRC per-word timestamps: <mm:ss.xx> before each word.
_WORD_TS = re.compile(r"<(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?>")


def _stamp_seconds(m: "re.Match[str]") -> float:
    """Convert a matched [mm:ss.xx] / <mm:ss.xx> stamp to seconds."""
    mm = int(m.group(1))
    ss = int(m.group(2))
    frac = m.group(3) or "0"
    ms = int(frac.ljust(3, "0")[:3])
    return mm * 60 + ss + ms / 1000.0

# Suffixes Spotify/streaming titles carry that LRCLIB's exact match chokes on,
# e.g. "The Wind is Whispering - Live", "Song (Remastered 2011)",
# "Track - Radio Edit", "Tune (feat. X)". We keep "(feat. ...)" off the title
# because LRCLIB indexes the primary artist only.
_TITLE_SUFFIX = re.compile(
    r"""\s*(?:
        [-–—]\s*(?:live|remaster(?:ed)?(?:\s+\d{4})?|radio\s+edit|
                   single\s+version|album\s+version|mono|stereo|
                   \d{4}\s+remaster(?:ed)?|re-?recorded(?:\s+\d{4})?|
                   acoustic|demo|edit|extended(?:\s+mix)?|bonus\s+track)
        |\(\s*(?:live|remaster(?:ed)?(?:\s+\d{4})?|radio\s+edit|
                 single\s+version|album\s+version|mono|stereo|
                 \d{4}\s+remaster(?:ed)?|re-?recorded(?:\s+\d{4})?|
                 acoustic|demo|edit|extended(?:\s+mix)?|bonus\s+track|
                 feat\.?[^)]*|ft\.?[^)]*|with[^)]*)\s*\)
    )\s*$""",
    re.IGNORECASE | re.VERBOSE,
)


def clean_title(title: str) -> str:
    """Strip trailing streaming-service suffixes (- Live, (Remastered), etc).

    Applied repeatedly so stacked suffixes like "Song - Live (Remastered 2011)"
    collapse to "Song". Returns the input unchanged if nothing matches.
    """
    prev = None
    out = title.strip()
    while out and out != prev:
        prev = out
        out = _TITLE_SUFFIX.sub("", out).strip()
    return out or title.strip()


# Decorations that YouTube/player page titles carry but real track titles don't.
# Unlike _TITLE_SUFFIX these can appear anywhere in the string, not just at the
# end (e.g. "Song (Official Video) [HD]" or "Song | YouTube Music").
_PAGE_DECORATION = re.compile(
    r"""(?:
        \s*[|]\s*(?:youtube(?:\s+music)?|topic)\s*(?=$|[|\-–—])
        |\s*[-–—]\s*youtube(?:\s+music)?\s*$
        |\s*[\(\[]\s*(?:official(?:\s+\w+)*|lyrics?(?:\s+video)?|
                        (?:full\s+)?album|full\s+album|audio|hd|hq|4k|
                        music\s+video|visualizer|lyric\s+video)\s*[\)\]]
    )""",
    re.IGNORECASE | re.VERBOSE,
)

# YouTube's auto-generated artist channels are named "<Artist> - Topic".
_ARTIST_TOPIC = re.compile(r"\s*[-–—]\s*topic\s*$", re.IGNORECASE)

# The same channel suffix, but stranded at the START of the title because the
# raw "<Artist> - Topic - <Song>" string was split on its first separator.
_TITLE_TOPIC_PREFIX = re.compile(r"^\s*topic\s*[-–—]\s*", re.IGNORECASE)

# A title that is nothing but a platform name carries no song at all.
_TITLE_IS_PLATFORM = re.compile(
    r"^\s*(?:youtube(?:\s+music)?|topic|spotify|soundcloud)\s*$", re.IGNORECASE
)


def clean_page_title(title: str) -> str:
    """Strip YouTube/player page decorations from a title.

    Removes "| YouTube Music", "(Official Video)", "[HD]", "(Full Album)" and
    friends, then collapses leftover separators. Complements :func:`clean_title`,
    which only handles trailing streaming-edition suffixes.
    """
    prev = None
    out = title.strip()
    while out and out != prev:
        prev = out
        out = _PAGE_DECORATION.sub(" ", out)
        out = _TITLE_TOPIC_PREFIX.sub("", out)
        out = re.sub(r"\s{2,}", " ", out).strip(" -–—|")
    return "" if _TITLE_IS_PLATFORM.match(out) else out


# Channel-name suffixes an uploader adds that are not part of the artist's
# name: "Queen Official", "ModjoOfficial", "ArtistVEVO". Deliberately narrow --
# "Music" and "Records" are not included, because plenty of acts genuinely
# carry them and stripping would rename the artist rather than clean it.
_ARTIST_CHANNEL_SUFFIX = re.compile(r"\s*[-–—]?\s*(?:official|vevo)\s*$",
                                    re.IGNORECASE)


def clean_artist(artist: str) -> str:
    """Strip channel decoration from an artist name.

    Handles YouTube's auto-generated "- Topic" and the "Official"/"VEVO"
    suffixes uploaders append to their channel names, which arrive as the
    artist when a track is learned from a browser tab.
    """
    out = _ARTIST_TOPIC.sub("", artist.strip()).strip()
    cleaned = _ARTIST_CHANNEL_SUFFIX.sub("", out).strip()
    # Never strip a name away entirely: an act actually called "Official" keeps
    # its name rather than becoming blank.
    return cleaned or out


@dataclass
class Lyrics:
    """Lyrics payload used by fetchers, cache lookups and players.

    `synced_raw` keeps the original LRC text for write-through caching, while
    `lines` is the parsed `(seconds, text)` representation used by renderers.
    """

    plain: str = ""
    synced_raw: str = ""          # original LRC text (cached verbatim)
    source: str = "none"          # lrclib | whisper | none
    lines: list[tuple[float, str]] = field(default_factory=list)  # (seconds, text)
    # Track length in seconds as reported by the lyrics source. Lets a caller
    # reject audio of obviously the wrong length (a full-album upload, a clip)
    # before transcribing against it. None when the source doesn't report one.
    duration: Optional[float] = None

    @property
    def has_synced(self) -> bool:
        """Whether timestamped lyric lines are available."""
        return bool(self.lines)


def parse_enhanced_lrc(
    lrc: str,
) -> tuple[list[tuple[float, str]], dict[int, float], dict[int, list[float]]]:
    """Parse LRC text, including Enhanced LRC word timings and end markers.

    Returns ``(lines, ends, word_times)``:

    - ``lines``   — time-sorted ``(seconds, text)``, as :func:`parse_lrc`.
    - ``ends``    — ``{line_index: end_seconds}`` from empty-text timestamps
      (``[00:14.00]`` on its own) or a trailing ``<mm:ss.xx>`` word tag. This
      is how an instrumental break after a line is expressed in plain LRC.
    - ``word_times`` — ``{line_index: [word_start_seconds, ...]}`` from
      Enhanced LRC ``<mm:ss.xx>`` tags.

    Enhanced LRC (``[00:12.00]<00:12.00>I <00:12.30>see``) is a widely adopted
    extension supported by AIMP, foobar2000/ESLyric, Kugou, QQ Music and
    NetEase. Plain LRC parses through unchanged, with empty ``ends``/
    ``word_times``.
    """
    # (start_time, text, word_times) in file order, before sorting.
    entries: list[tuple[float, str, list[float]]] = []
    # Pending end markers as absolute times; attached after sorting.
    end_stamps: list[float] = []

    for raw in lrc.splitlines():
        stamps = list(_TS.finditer(raw))
        if not stamps:
            continue
        body = _TS.sub("", raw).strip()

        word_stamps = list(_WORD_TS.finditer(body))
        text = _WORD_TS.sub(" ", body).strip()
        text = re.sub(r"\s+", " ", text)

        if not text:
            # No lyric text: an end marker for whatever line precedes it.
            for m in stamps:
                end_stamps.append(_stamp_seconds(m))
            continue

        times = [_stamp_seconds(m) for m in stamps]
        words = [_stamp_seconds(m) for m in word_stamps]

        # A trailing word tag with no word after it marks the line's end.
        trailing_end: Optional[float] = None
        if word_stamps and not body[word_stamps[-1].end():].strip():
            trailing_end = words.pop() if words else None

        for t in times:
            entries.append((t, text, list(words)))
            if trailing_end is not None:
                end_stamps.append(trailing_end)

    entries.sort(key=lambda e: e[0])
    lines = [(t, text) for t, text, _ in entries]
    word_times = {i: w for i, (_, _, w) in enumerate(entries) if w}

    # Attach each end marker to the last line that starts before it.
    ends: dict[int, float] = {}
    for stamp in end_stamps:
        idx = -1
        for i, (t, _) in enumerate(lines):
            if t < stamp:
                idx = i
            else:
                break
        if idx >= 0:
            ends[idx] = stamp

    return lines, ends, word_times


def parse_lrc_with_ends(
    lrc: str,
) -> tuple[list[tuple[float, str]], dict[int, float], dict[int, list[float]]]:
    """Alias of :func:`parse_enhanced_lrc` (kept for call-site clarity)."""
    return parse_enhanced_lrc(lrc)


def parse_lrc(lrc: str) -> list[tuple[float, str]]:
    """Parse LRC text into a time-sorted list of (seconds, text).

    Lines with multiple timestamps are expanded. Metadata-only lines (e.g.
    ``[ar: Artist]``) and empty-text stamps are skipped. Enhanced LRC word
    tags are stripped from the text.

    Use :func:`parse_enhanced_lrc` when you also need line ends or word
    timings.
    """
    lines, _, _ = parse_enhanced_lrc(lrc)
    return lines


def _to_lyrics(payload: dict) -> Lyrics:
    synced = payload.get("syncedLyrics") or ""
    plain = payload.get("plainLyrics") or ""
    lines = parse_lrc(synced) if synced else []
    try:
        duration = float(payload["duration"]) if payload.get("duration") else None
    except (TypeError, ValueError):
        duration = None
    return Lyrics(
        plain=plain,
        synced_raw=synced,
        source="lrclib" if (synced or plain) else "none",
        lines=lines,
        duration=duration,
    )


def _duration_matches(candidate_dur: Optional[float], target_dur: Optional[float],
                      tolerance_s: float = 15.0) -> bool:
    """Whether an LRCLIB entry's duration is compatible with the requested track.

    When both durations are known, rejects synced lyrics from a different edit
    or live performance (e.g. a 373s cut when the track is 456s). Also checks
    the last timestamp in synced lyrics when the entry's duration metadata was
    mis-tagged by the uploader.
    """
    if candidate_dur is None or target_dur is None:
        return True
    try:
        return abs(float(candidate_dur) - float(target_dur)) <= tolerance_s
    except (TypeError, ValueError):
        return True


def _synced_fits_duration(ly: Lyrics, target_dur: Optional[float],
                          tolerance_s: float = 45.0) -> bool:
    """Whether synced lyrics' span is plausible for ``target_dur``.

    Community LRCLIB entries sometimes copy the 6:12 Baby Snakes LRC onto a
    7:36 Zappa in New York FLAC entry (reporting duration=456 while the last
    lyric timestamp ends at 372s). Rejects synced lyrics whose duration header
    disagrees with ``target_dur``, whose timestamps run past the audio length,
    or whose last timestamp stops more than ``tolerance_s`` before the end of a
    multi-line track when the entry claims a matching duration.
    """
    if not ly.has_synced or target_dur is None or target_dur <= 0:
        return True
    if not _duration_matches(ly.duration, target_dur, tolerance_s=15.0):
        return False
    last_t = ly.lines[-1][0] if ly.lines else 0.0
    # Synced timestamps running past the audio length (+5s padding) cannot fit.
    if last_t > target_dur + 5.0:
        return False
    # If the LRC has substantial synced content (>= 10 lines) and claims a
    # duration matching target_dur, reject it if its last timestamp ends more
    # than 60s before target_dur AND < 85% of the track length (e.g. a 6:12 LRC
    # pasted onto a 7:36 track).
    if len(ly.lines) >= 10 and (target_dur - last_t) > 60.0 and last_t < 0.85 * target_dur:
        return False
    return True


def fetch_lrclib(
    artist: str,
    title: str,
    album: Optional[str] = None,
    duration: Optional[float] = None,
    *,
    timeout: float = 10.0,
    session: Optional[Any] = None,
) -> Lyrics:
    """Fetch lyrics for a track. Tries /api/get, then /api/search.

    If the exact title misses, retries once with a cleaned title (streaming
    suffixes like "- Live" / "(Remastered)" stripped), which LRCLIB indexes.
    """
    http: Any = session or requests
    base = settings.lrclib_base.rstrip("/")

    titles = [title]
    cleaned_t = clean_title(title)
    if cleaned_t and cleaned_t != title:
        titles.append(cleaned_t)
        
    page_cleaned = clean_page_title(title)
    if page_cleaned and page_cleaned not in titles:
        titles.append(page_cleaned)

    artists = [artist]
    cleaned_a = clean_artist(artist)
    if cleaned_a and cleaned_a != artist:
        artists.append(cleaned_a)

    for a in artists:
        for t in titles:
            params: dict[str, str] = {"artist_name": a, "track_name": t}
            if album:
                params["album_name"] = album
            if duration:
                params["duration"] = str(int(round(duration)))

            plain_fallback = None
            try:
                r = http.get(f"{base}/api/get", params=params, timeout=timeout)
                if r.status_code == 200:
                    ly = _to_lyrics(r.json())
                    if ly.has_synced and _synced_fits_duration(ly, duration):
                        return ly
                    if ly.plain:
                        plain_fallback = Lyrics(
                            plain=ly.plain,
                            source=ly.source,
                            duration=ly.duration,
                        )
            except requests.RequestException:
                pass

            # Fallback: search and take the best synced hit whose duration fits, else plain.
            try:
                r = http.get(
                    f"{base}/api/search",
                    params={"artist_name": a, "track_name": t},
                    timeout=timeout,
                )
                if r.status_code == 200:
                    results = r.json() or []
                    if results:
                        for cand in results:
                            if not cand.get("syncedLyrics"):
                                continue
                            ly = _to_lyrics(cand)
                            if ly.has_synced and _synced_fits_duration(ly, duration):
                                return ly
                        if plain_fallback is None:
                            best_plain = next(
                                (x for x in results if _duration_matches(x.get("duration"), duration) and (x.get("plainLyrics") or x.get("syncedLyrics"))),
                                results[0],
                            )
                            ly_plain = _to_lyrics(best_plain)
                            plain_fallback = Lyrics(
                                plain=ly_plain.plain,
                                source=ly_plain.source,
                                duration=ly_plain.duration,
                            )
            except requests.RequestException:
                pass
                
            if plain_fallback is not None:
                return plain_fallback

    return Lyrics()


def _format_stamp(secs: float, is_word: bool = False) -> str:
    secs = max(0.0, float(secs))
    mm = int(secs // 60)
    ss = int(secs % 60)
    ms = int(round((secs - int(secs)) * 100))
    if is_word:
        return f"<{mm:02d}:{ss:02d}.{ms:02d}>"
    return f"[{mm:02d}:{ss:02d}.{ms:02d}]"


def shift_lrc(lrc: str, offset_seconds: float) -> str:
    """Shift all line and word timestamps in LRC text by ``offset_seconds``.

    Positive offset shifts stamps forward in time (e.g. for a music video with an intro).
    Negative offset shifts stamps backward in time. Stamps are bounded at >= 0.0.
    """
    if not lrc:
        return ""
    out_lines: list[str] = []
    for line in lrc.splitlines():
        # Shift line timestamps [mm:ss.xx]
        def repl_line(m: re.Match) -> str:
            t = _stamp_seconds(m) + offset_seconds
            return _format_stamp(t, is_word=False)

        shifted = _TS.sub(repl_line, line)

        # Shift word timestamps <mm:ss.xx> (Enhanced LRC)
        def repl_word(m: re.Match) -> str:
            t = _stamp_seconds(m) + offset_seconds
            return _format_stamp(t, is_word=True)

        shifted = _WORD_TS.sub(repl_word, shifted)
        out_lines.append(shifted)
    return "\n".join(out_lines)


def sync_video_from_audio(audio_lrc: str, offset_seconds: float) -> str:
    """Derive video-synced LRC from audio LRC by shifting forward by ``offset_seconds``."""
    return shift_lrc(audio_lrc, offset_seconds)


def sync_audio_from_video(video_lrc: str, offset_seconds: float) -> str:
    """Derive audio-synced LRC from video LRC by shifting backward by ``offset_seconds``."""
    return shift_lrc(video_lrc, -offset_seconds)


def align_audio_and_video_lyrics(audio_lrc: str, video_lrc: str) -> Optional[float]:
    """Calculate the time offset in seconds (video_time - audio_time) between audio and video lyrics.

    Returns the median time delta across matching lines, or None if fewer than 2 matching lines are found.
    """
    from difflib import SequenceMatcher
    from statistics import median

    def parse_plain_lines(lrc: str) -> list[tuple[float, str]]:
        res: list[tuple[float, str]] = []
        for line in lrc.splitlines():
            m = _TS.search(line)
            if m:
                t = _stamp_seconds(m)
                txt = _TS.sub("", line)
                txt = _WORD_TS.sub("", txt).strip().lower()
                clean = re.sub(r"[^a-z0-9 ]", "", txt)
                if clean:
                    res.append((t, clean))
        return res

    a_lines = parse_plain_lines(audio_lrc)
    v_lines = parse_plain_lines(video_lrc)
    if not a_lines or not v_lines:
        return None

    diffs: list[float] = []
    for a_t, a_txt in a_lines:
        best_match_time = None
        best_ratio = 0.0
        for v_t, v_txt in v_lines:
            ratio = SequenceMatcher(None, a_txt, v_txt).ratio()
            if ratio > best_ratio and ratio >= 0.7:
                best_ratio = ratio
                best_match_time = v_t
        if best_match_time is not None:
            diffs.append(best_match_time - a_t)

    if len(diffs) < 2:
        return None
    return round(float(median(diffs)), 2)


def _cli() -> int:  # pragma: no cover - thin manual smoke test
    import argparse

    ap = argparse.ArgumentParser(description="Fetch LRCLIB lyrics")
    ap.add_argument("--artist", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--album")
    ap.add_argument("--duration", type=float)
    a = ap.parse_args()
    ly = fetch_lrclib(a.artist, a.title, a.album, a.duration)
    print(f"source={ly.source} synced={ly.has_synced} lines={len(ly.lines)}")
    for t, text in ly.lines[:8]:
        print(f"  [{t:7.2f}] {text}")
    if not ly.lines and ly.plain:
        print(ly.plain[:300])
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
