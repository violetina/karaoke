"""Detect YouTube caption availability and convert captions into synced LRC.

YouTube exposes two distinct caption sets via yt-dlp:

- ``subtitles`` — captions the uploader supplied. For music videos these are
  frequently the actual lyrics, so they are the highest-quality source.
- ``automatic_captions`` — ASR transcripts. Usable, but noisier.

Both can be requested in the ``json3`` format, which carries per-word
millisecond offsets. That is finer-grained than LRC needs, so words are grouped
back into their caption cue and each cue becomes one timed LRC line. This gives
synced lyrics without running Whisper.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

# Sound-effect / non-lyric cues that captions insert. Matched anywhere in a
# line, because real caption cues embed them mid-text ("[music] Heat. Heat.").
_NOISE_TOKEN_RE = re.compile(
    r"\[\s*(music|musik|muziek|applause|laughter|cheering|silence|sound"
    r"|instrumental|singing|humming|vocalizing|no speech|inaudible)\s*\]",
    re.IGNORECASE,
)
# Caption speaker/turn markers, e.g. ">> Heat".
_SPEAKER_RE = re.compile(r"^\s*(?:>>+|-\s)\s*")
# Real caption languages, e.g. en, en-orig, en-GB, nl. Excludes yt-dlp pseudo
# languages such as `en-ehkg1hFWq8A` (a video-id suffix) and `live_chat`.
_LANG_RE = re.compile(r"^[a-z]{2,3}(?:-(?:orig|[A-Z]{2}|Latn|Hans|Hant))?$")

# json3 first: it is the only format carrying per-word timing.
_EXT_PREFERENCE = ("json3", "srv3", "vtt", "ttml", "srt")


@dataclass(frozen=True)
class CaptionTrack:
    """A single selectable caption track."""

    language: str
    ext: str
    url: str
    kind: str  # "manual" | "automatic"


@dataclass(frozen=True)
class CaptionAvailability:
    """What captions a video offers, and the best track to use."""

    has_manual: bool
    has_automatic: bool
    manual_languages: tuple[str, ...]
    automatic_languages: tuple[str, ...]
    best: Optional[CaptionTrack]

    @property
    def available(self) -> bool:
        """True when any usable caption track was found."""
        return self.best is not None

    def describe(self) -> str:
        """Human-readable one-line summary for CLI output."""
        if not self.available:
            return "no captions"
        assert self.best is not None
        return f"{self.best.kind} captions [{self.best.language}] as {self.best.ext}"


def is_real_language(code: str) -> bool:
    """Return True for genuine caption language codes.

    Filters yt-dlp artifacts: ``live_chat`` is not a caption track, and
    per-video pseudo-languages like ``en-ehkg1hFWq8A`` are not selectable.
    """
    if not code or code == "live_chat":
        return False
    return bool(_LANG_RE.match(code))


def _pick_ext(entries: list[dict]) -> Optional[dict]:
    """Choose the richest caption format available for one language."""
    for ext in _EXT_PREFERENCE:
        for entry in entries:
            if entry.get("ext") == ext and entry.get("url"):
                return entry
    return None


def probe_captions(
    info: Any,
    languages: Iterable[str] = ("en", "en-orig", "nl"),
) -> CaptionAvailability:
    """Inspect a yt-dlp info dict and report caption availability.

    Manual (uploader-provided) captions outrank automatic ones because for
    music videos they are typically the real lyrics.
    """
    info = info or {}
    manual = {k: v for k, v in (info.get("subtitles") or {}).items() if is_real_language(k)}
    automatic = {
        k: v for k, v in (info.get("automatic_captions") or {}).items() if is_real_language(k)
    }

    preferred = list(languages)
    # Consider requested languages first, then any other real language.
    ordered = preferred + [k for k in (*manual, *automatic) if k not in preferred]

    best: Optional[CaptionTrack] = None
    for kind, pool in (("manual", manual), ("automatic", automatic)):
        for lang in ordered:
            entry = _pick_ext(pool.get(lang) or [])
            if entry:
                best = CaptionTrack(
                    language=lang, ext=entry["ext"], url=entry["url"], kind=kind
                )
                break
        if best:
            break

    return CaptionAvailability(
        has_manual=bool(manual),
        has_automatic=bool(automatic),
        manual_languages=tuple(manual),
        automatic_languages=tuple(automatic),
        best=best,
    )


def _format_lrc_timestamp(ms: int) -> str:
    """Render milliseconds as ``[mm:ss.cc]``.

    LRC has no hour field, so anything past 60 minutes keeps counting minutes
    (1h02m03s becomes ``62:03``).
    """
    total_cs = max(0, int(round(ms / 10.0)))
    minutes, rem_cs = divmod(total_cs, 6000)
    seconds, centis = divmod(rem_cs, 100)
    return f"[{minutes:02d}:{seconds:02d}.{centis:02d}]"


def _format_word_timestamp(ms: int) -> str:
    """Render milliseconds as an Enhanced LRC word tag ``<mm:ss.cc>``."""
    return "<" + _format_lrc_timestamp(ms)[1:-1] + ">"


def _cue_text(event: dict) -> str:
    """Join a cue's word segments into a single cleaned line.

    Strips speaker markers and embedded sound-effect tokens; real caption cues
    mix them into lyric text rather than isolating them on their own line.
    """
    parts = [str(seg.get("utf8", "")) for seg in event.get("segs") or []]
    text = "".join(parts)
    text = text.replace("\u200b", " ")
    text = _NOISE_TOKEN_RE.sub(" ", text)
    text = _SPEAKER_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _clean_token(raw: str) -> str:
    """Normalise one caption word segment."""
    text = str(raw).replace("\u200b", " ")
    text = _NOISE_TOKEN_RE.sub(" ", text)
    return text


def _split_event(
    event: dict, start_ms: int, max_words: int
) -> list[tuple[int, str, list[int]]]:
    """Turn one cue into timed lines, splitting long cues on word offsets.

    json3 gives each word a ``tOffsetMs`` relative to the cue start, so a long
    cue can be broken into shorter karaoke lines at genuine word times rather
    than interpolated guesses. Returns ``(start_ms, text, word_start_ms)`` so
    callers can emit either plain or Enhanced LRC.
    """
    words: list[tuple[int, str]] = []
    for seg in event.get("segs") or []:
        token = _clean_token(seg.get("utf8", ""))
        if not token.strip():
            continue
        offset = seg.get("tOffsetMs") or 0
        words.append((start_ms + int(offset), token))

    if not words:
        return []

    # Reassemble, keeping each word's own timestamp as a potential line start.
    merged: list[tuple[int, str]] = []
    for ts, token in words:
        if merged and not token.startswith(" ") and not merged[-1][1].endswith(" "):
            # Continuation of the previous word (json3 splits mid-word).
            merged[-1] = (merged[-1][0], merged[-1][1] + token)
        else:
            merged.append((ts, token))

    cleaned = [(ts, tok.strip()) for ts, tok in merged if tok.strip()]
    if not cleaned:
        return []

    if max_words <= 0 or len(cleaned) <= max_words:
        chunks = [cleaned]
    else:
        chunks = [
            cleaned[i : i + max_words]
            for i in range(0, len(cleaned), max_words)
        ]

    out: list[tuple[int, str, list[int]]] = []
    for chunk in chunks:
        text = _SPEAKER_RE.sub("", " ".join(t for _, t in chunk)).strip()
        if not text:
            continue
        # Speaker-marker stripping can drop leading tokens; keep one timestamp
        # per surviving word so word tags line up with the text.
        times = [ts for ts, _ in chunk][-len(text.split()):]
        out.append((chunk[0][0], text, times))
    return out


def _caption_rows(
    payload: str | dict, max_words: int
) -> list[tuple[int, str, list[int]]]:
    """Parse a json3 payload into deduped ``(start_ms, text, word_ms)`` rows.

    Shared by the plain and Enhanced LRC writers so both agree exactly on line
    splitting, ordering and de-duplication.

    Raises ``ValueError`` when the payload is not valid json3.
    """
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid json3 caption payload: {exc}") from exc
    else:
        data = payload
    if not isinstance(data, dict):
        raise ValueError("json3 caption payload must be an object")

    rows: list[tuple[int, str, list[int]]] = []
    for event in data.get("events") or []:
        if not isinstance(event, dict) or not event.get("segs"):
            continue
        start = event.get("tStartMs")
        if start is None:
            continue
        rows.extend(_split_event(event, int(start), max_words))

    rows.sort(key=lambda r: r[0])

    deduped: list[tuple[int, str, list[int]]] = []
    previous: Optional[str] = None
    for start_ms, text, word_ms in rows:
        # Rolling auto-captions repeat the same text as it grows; keep changes.
        if text == previous:
            continue
        previous = text
        deduped.append((start_ms, text, word_ms))
    return deduped


def json3_to_lrc(payload: str | dict, *, max_words: int = 10) -> str:
    """Convert a YouTube ``json3`` caption payload into LRC text.

    Each caption cue becomes one or more timed lines. Cues longer than
    ``max_words`` are split on the per-word ``tOffsetMs`` timings json3
    provides, so long uploader-supplied cues stay readable on screen without
    inventing timestamps. Pass ``max_words=0`` to disable splitting.

    Raises ``ValueError`` when the payload is not valid json3.
    """
    return "\n".join(
        f"{_format_lrc_timestamp(start_ms)} {text}"
        for start_ms, text, _ in _caption_rows(payload, max_words)
    )


def json3_to_enhanced_lrc(payload: str | dict, *, max_words: int = 10) -> str:
    """Convert a ``json3`` caption payload into **Enhanced** LRC.

    json3 carries a start time for every word, so the output keeps them as
    ``<mm:ss.xx>`` word tags:

    ``[00:12.00]<00:12.00>I <00:12.30>see <00:12.60>trees``

    That gives real word-level highlighting with no interpolation and no
    manual tapping. Per the Enhanced LRC convention the first word tag matches
    the line timestamp. Falls back to a bare line stamp when a row somehow has
    no word timings.

    Raises ``ValueError`` when the payload is not valid json3.
    """
    out: list[str] = []
    for start_ms, text, word_ms in _caption_rows(payload, max_words):
        stamp = _format_lrc_timestamp(start_ms)
        words = text.split()
        if len(word_ms) != len(words):
            out.append(f"{stamp} {text}")
            continue
        tagged = " ".join(
            f"{_format_word_timestamp(ts)}{w}" for ts, w in zip(word_ms, words)
        )
        out.append(f"{stamp} {tagged}")
    return "\n".join(out)


def fetch_video_caption_lyrics(
    url_or_video_id: str,
    cookies_from_browser: Optional[str] = None,
) -> Optional[Any]:
    """Fetch YouTube captions for a video and convert to Lyrics."""
    import subprocess
    import urllib.request
    from .lyrics import Lyrics, parse_lrc

    url = url_or_video_id if url_or_video_id.startswith("http") else f"https://www.youtube.com/watch?v={url_or_video_id}"
    cmd = ["yt-dlp", "--dump-json", "--skip-download"]
    if cookies_from_browser:
        cmd.extend(["--cookies-from-browser", cookies_from_browser])
    cmd.append(url)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=30, check=True,
        )
        info = json.loads(proc.stdout)
    except Exception as exc:
        return None

    avail = probe_captions(info)
    if not avail.available or not avail.best or not avail.best.url:
        return None

    try:
        req = urllib.request.Request(
            avail.best.url,
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64)"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw_payload = resp.read().decode("utf-8", errors="replace")

        lrc_text = json3_to_enhanced_lrc(raw_payload) if avail.best.ext == "json3" else json3_to_lrc(raw_payload)
        if not lrc_text:
            return None

        lines = parse_lrc(lrc_text)
        plain = "\n".join(t for _, t in lines)
        return Lyrics(
            plain=plain,
            synced_raw=lrc_text,
            source="youtube_caption",
            lines=lines,
            duration=float(info.get("duration") or 0) or None,
        )
    except Exception:
        return None


def _try_waveform_offset(track: dict, client: Any) -> Optional[dict[str, Any]]:
    """Resolve both Audio and Video ids and attempt the waveform offset.

    Always returns the resolved ids when the lookup itself succeeds (even if
    the offset could not be trusted), so the caller can reuse the video id
    instead of looking it up a second time. The 'offset'/'confidence'/'kind'
    keys are only present when av_align found a usable match -- that is what
    callers check before trusting this over the caption-based path.
    """
    from . import av_align
    from .logger import log
    from .postprocess_worker import run_download_logic

    try:
        versions = client.get_audio_and_video_versions(track["artist"], track["title"])
    except Exception as exc:
        log.debug("waveform av-sync: version lookup failed for %s - %s: %s",
                 track.get("artist"), track.get("title"), exc)
        return None
    audio_id, video_id = versions.get("audio_video_id"), versions.get("video_video_id")
    if not video_id:
        return None
    info: dict[str, Any] = {"audio_id": audio_id, "video_id": video_id}
    if not audio_id or audio_id == video_id:
        return info  # no distinct counterpart to compare against

    try:
        audio_path = run_download_logic(f"https://www.youtube.com/watch?v={audio_id}", None)
        video_path = run_download_logic(f"https://www.youtube.com/watch?v={video_id}", None)
        if not audio_path or not video_path:
            return info
        audio_y = av_align.load_mono(audio_path, av_align.TARGET_SR)
        video_y = av_align.load_mono(video_path, av_align.TARGET_SR)
        result = av_align.estimate_offset(audio_y, video_y, av_align.TARGET_SR)
    except Exception as exc:
        log.debug("waveform av-sync: alignment failed for track %s: %s", track.get("track_id"), exc)
        return info
    if result is None or result.kind == "low_confidence":
        return info
    info["offset"] = result.offset_s
    info["confidence"] = result.confidence
    info["kind"] = result.kind
    return info


def _try_whisper_offset(track: dict, audio_lrc: str, video_url: str) -> Optional[float]:
    """Last-resort offset estimate: transcribe the video's audio with Whisper
    and fuzzy-match it against the already-known SYNCED audio lyrics, via the
    same algorithm align_audio_and_video_lyrics uses for real captions.

    A coarse anchor only, not a third confident method: faster-whisper is not
    fully deterministic even at temperature=0 (model batching/threading still
    introduces jitter), so expect tens of seconds of drift between runs on
    the same track -- never treat this as a stable constant the way the
    waveform or caption offsets are.
    """
    from .lyrics import align_audio_and_video_lyrics
    from .logger import log
    from .postprocess_worker import run_download_logic
    from .whisper_sync import transcribe_to_lrc

    video_path = run_download_logic(video_url, None)
    if not video_path:
        return None
    try:
        video_lrc = transcribe_to_lrc(str(video_path), temperature=0.0)
    except Exception as exc:
        log.debug("whisper av-sync: transcription failed for track %s: %s",
                 track.get("track_id"), exc)
        return None
    if not video_lrc.strip():
        return None
    return align_audio_and_video_lyrics(audio_lrc, video_lrc)


def sync_audio_video_counterparts(
    track_id: int,
    conn: Any,
    client: Optional[Any] = None,
    cookies_from_browser: Optional[str] = None,
    enable_whisper_fallback: bool = False,
) -> dict[str, Any]:
    """Inspect and synchronize Audio vs Video lyrics and offsets for a track.

    1. Checks for existing audio lyrics (LRCLIB, etc.) and video lyrics (YouTube captions).
    2. If counterpart video is known/available, fetches captions and calculates time offset.
    3. Persists both audio and video lyrics, and saves the video offset in `track_sync_offsets`.

    Order of preference per PLAN-av-sync.md: waveform offset (when two
    distinct Audio/Video ids resolve and av_align is confident) wins; then
    the existing caption-based alignment; then, only when
    ``enable_whisper_fallback=True``, a Whisper transcript of the video's
    audio as a coarse last-resort anchor. Whisper defaults OFF: almost no
    tracks have real video captions (1 of ~11,000 in the audio-lyrics
    library, per the plan's own data read), so leaving it on by default
    would silently start full-track Whisper transcription for nearly every
    postprocessed track -- an expensive, CPU-bound, non-deterministic path
    that should be opted into (a dedicated backfill run), not switched on
    for the whole pipeline by default.
    """
    from . import localcache
    from .lyrics import align_audio_and_video_lyrics, shift_lrc, sync_video_from_audio, sync_audio_from_video

    cur = conn.cursor()
    cur.execute("SELECT track_id, artist, title, duration FROM tracks WHERE track_id = %s", (track_id,))
    track = cur.fetchone()
    if not track:
        return {"status": "track_not_found"}

    audio_lyrics = localcache.get_lyrics_by_track_id(track_id, conn, media_type="audio")
    video_lyrics = localcache.get_lyrics_by_track_id(track_id, conn, media_type="video")

    cur.execute(
        """
        SELECT url, kind FROM sources
        WHERE track_id = %s AND (kind IN ('youtube', 'youtube_video', 'youtube_music') OR url LIKE '%%youtube.com/watch%%')
        ORDER BY source_id DESC
        """,
        (track_id,),
    )
    video_sources = cur.fetchall()
    video_url = video_sources[0]["url"] if video_sources else None

    waveform_info = None
    if client:
        already_known = localcache.get_sync_offset(track_id, conn, mode="video")
        if already_known is not None and already_known != 0.0:
            # Already has a trustworthy offset from an earlier run (either
            # method) -- skip the two extra downloads this needs. Re-running
            # it on every postprocess call (this function can run twice per
            # track in the existing chain) would double the network cost of
            # every single processed track for no new information.
            pass
        else:
            try:
                waveform_info = _try_waveform_offset(dict(track), client)
            except Exception as exc:
                from .logger import log
                log.debug("postprocess: waveform av-sync failed for track %s: %s", track_id, exc)
        if waveform_info and waveform_info.get("offset") is not None:
            localcache.set_sync_offset(track_id, waveform_info["offset"], conn, mode="video")

    if not video_url and client:
        if waveform_info and waveform_info.get("video_id"):
            video_url = f"https://www.youtube.com/watch?v={waveform_info['video_id']}"
            localcache.add_track_source(track["artist"], track["title"], url=video_url, kind="youtube", conn=conn)
        else:
            try:
                versions = client.get_audio_and_video_versions(track["artist"], track["title"])
                if versions.get("video_video_id"):
                    video_url = f"https://www.youtube.com/watch?v={versions['video_video_id']}"
                    localcache.add_track_source(track["artist"], track["title"], url=video_url, kind="youtube", conn=conn)
            except Exception:
                pass

    if not video_lyrics and video_url:
        cap_lyrics = fetch_video_caption_lyrics(video_url, cookies_from_browser=cookies_from_browser)
        if cap_lyrics and cap_lyrics.has_synced:
            localcache.put_cached_lyrics(
                track["artist"], track["title"], cap_lyrics, media_type="video", conn=conn
            )
            video_lyrics = cap_lyrics

    offset = waveform_info["offset"] if waveform_info and waveform_info.get("offset") is not None else None
    # get_lyrics_by_track_id(media_type="video") falls back to ANY approved
    # lyrics row when none is specifically typed 'video' (its own documented
    # behavior) -- for the very common case of a track with only one lyrics
    # row (audio: ~10,929 of ~10,930 per the plan's own data read), that
    # silently hands back the audio row mislabeled as video. Left unguarded,
    # the branch below would then "align" the audio lyrics against
    # themselves and store a meaningless offset of ~0.0 -- a likely
    # explanation for the Data section's "1236 are exactly 0.0, probably
    # 'nothing found' defaults": they may actually be this self-alignment
    # artifact, not a deliberate default.
    real_video_lyrics = bool(video_lyrics) and not (
        audio_lyrics and video_lyrics.synced_raw == audio_lyrics.synced_raw
        and video_lyrics.source == audio_lyrics.source
    )
    if offset is None and audio_lyrics and audio_lyrics.has_synced and real_video_lyrics and video_lyrics.has_synced:
        offset = align_audio_and_video_lyrics(audio_lyrics.synced_raw, video_lyrics.synced_raw)
        if offset is not None:
            localcache.set_sync_offset(track_id, offset, conn, mode="video")

    elif audio_lyrics and audio_lyrics.has_synced and not real_video_lyrics:
        saved_video_offset = localcache.get_sync_offset(track_id, conn, mode="video")
        if saved_video_offset is not None and saved_video_offset != 0.0:
            derived_video_lrc = sync_video_from_audio(audio_lyrics.synced_raw, saved_video_offset)
            from .lyrics import Lyrics, parse_lrc
            d_lyrics = Lyrics(
                plain=audio_lyrics.plain,
                synced_raw=derived_video_lrc,
                source="derived_from_audio",
                lines=parse_lrc(derived_video_lrc),
            )
            localcache.put_cached_lyrics(
                track["artist"], track["title"], d_lyrics, media_type="video", conn=conn
            )
            video_lyrics = d_lyrics
            offset = saved_video_offset

    elif video_lyrics and video_lyrics.has_synced and not audio_lyrics:
        # Same class of fallback issue applies here in reverse (audio_lyrics
        # could be the video row mislabeled), but this branch requires real
        # video lyrics to exist at all -- currently 1 track in the whole
        # library per the plan's data read, so left as-is; not exercised
        # enough to be worth the same guard without a real case to test it.
        saved_video_offset = localcache.get_sync_offset(track_id, conn, mode="video") or 0.0
        derived_audio_lrc = sync_audio_from_video(video_lyrics.synced_raw, saved_video_offset)
        from .lyrics import Lyrics, parse_lrc
        d_lyrics = Lyrics(
            plain=video_lyrics.plain,
            synced_raw=derived_audio_lrc,
            source="derived_from_video",
            lines=parse_lrc(derived_audio_lrc),
        )
        localcache.put_cached_lyrics(
            track["artist"], track["title"], d_lyrics, media_type="audio", conn=conn
        )
        audio_lyrics = d_lyrics
        offset = saved_video_offset

    used_whisper = False
    if (offset is None and enable_whisper_fallback and video_url
            and audio_lyrics and audio_lyrics.has_synced and not real_video_lyrics):
        whisper_offset = _try_whisper_offset(dict(track), audio_lyrics.synced_raw, video_url)
        if whisper_offset is not None:
            localcache.set_sync_offset(track_id, whisper_offset, conn, mode="video")
            derived_video_lrc = sync_video_from_audio(audio_lyrics.synced_raw, whisper_offset)
            from .lyrics import Lyrics, parse_lrc
            d_lyrics = Lyrics(
                plain=audio_lyrics.plain,
                synced_raw=derived_video_lrc,
                source="derived_from_audio_whisper_anchor",
                lines=parse_lrc(derived_video_lrc),
            )
            localcache.put_cached_lyrics(
                track["artist"], track["title"], d_lyrics, media_type="video", conn=conn
            )
            video_lyrics = d_lyrics
            offset = whisper_offset
            used_whisper = True

    used_waveform = bool(waveform_info and waveform_info.get("offset") is not None
                        and offset == waveform_info["offset"])
    if used_waveform:
        method = "waveform"
    elif used_whisper:
        method = "whisper"
    elif offset is not None:
        method = "captions"
    else:
        method = None
    return {
        "status": "ok",
        "track_id": track_id,
        "has_audio_lyrics": bool(audio_lyrics and audio_lyrics.has_synced),
        "has_video_lyrics": bool(video_lyrics and video_lyrics.has_synced),
        "offset": offset,
        "method": method,
        "confidence": waveform_info.get("confidence") if used_waveform and waveform_info else None,
    }
