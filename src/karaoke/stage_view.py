"""Two-part cast architecture: live karaoke stage view and SSE lyrics prompter.

Provides a dedicated full-screen, high-visibility TV / Stage view served at
`/stage` (and `/tv`) on the control API, along with an SSE streaming endpoint
at `/api/stage/stream` broadcasting real-time playhead position, active lyric
lines, word-level highlights, audio rhythm/BPM, and up-next queue data.
"""
from __future__ import annotations

import asyncio
import html
import json
import time
from typing import Any, AsyncGenerator, Optional

from .logger import log
from .lyrics import parse_enhanced_lrc


#: track_id -> (checked_at, timeline or None). The stream ticks ten times a
#: second; a timeline changes only when one is first stored.
_chord_cache: dict[int, tuple[float, Optional[dict[str, Any]]]] = {}
_CHORD_RECHECK_S = 5.0


#: (checked_at, playhead) for the running recording's latest mark. Marks
#: arrive every 45s or so; reading the table ten times a second is waste.
_recording_cache: list[Any] = [0.0, None]
_RECORDING_RECHECK_S = 1.0


#: track_id -> (checked_at, stored mood timeline or None).
_mood_cache: dict[int, tuple[float, Optional[dict[str, Any]]]] = {}
_MOOD_RECHECK_S = 30.0
#: Tracks already queued for a timeline this process, so a tick does not
#: queue the same song ten times a second.
_mood_requested: set[int] = set()


def _mood_segment(track_id: Optional[int], artist: str, title: str,
                  position_s: float, duration: float) -> Optional[dict[str, Any]]:
    """The mood-timeline segment at the playhead, if the track has one.

    Never computes one here: that needs the CLAP model, which only the CLAP
    worker loads. A missing timeline is queued instead, once per track.
    """
    from . import chord_timeline, localcache, mood_timeline

    if track_id is None:
        return None
    checked_at, stored = _mood_cache.get(track_id, (0.0, None))
    if stored is None and time.time() - checked_at > _MOOD_RECHECK_S:
        try:
            with localcache.connect() as conn:
                stored = mood_timeline.load(track_id, conn)
        except Exception as exc:
            log.debug("stage_view: mood timeline lookup failed: %s", exc)
        _mood_cache[track_id] = (time.time(), stored)
        if stored is None and track_id not in _mood_requested and title:
            _mood_requested.add(track_id)
            try:
                from .postprocess_queue import enqueue_if_needed

                enqueue_if_needed(artist, title)
            except Exception:
                log.debug("stage_view: could not queue mood for track %s", track_id)
    # Same guard as the chords: a different edit would put moods at wrong times.
    if not stored or not chord_timeline.matches_playback(stored, duration):
        return None
    return mood_timeline.at(stored, position_s)


#: track_id -> overall lyric mood. Lyrics do not change while a track plays.
_track_mood_cache: dict[int, str] = {}


def _track_mood(track_id: Optional[int], lines: list[dict[str, Any]]) -> str:
    """The dominant mood of a track's lyrics as a whole, or "neutral"."""
    if track_id is None or not lines:
        return "neutral"
    if track_id not in _track_mood_cache:
        from .visuals import analyze_sentiment

        text = "\n".join(line["text"] for line in lines)
        _track_mood_cache[track_id] = analyze_sentiment(text).dominant
    return _track_mood_cache[track_id]


def _live_listener() -> Optional[dict[str, Any]]:
    """Song and playhead from radio mode or a running recording, if either.

    Neither has a player to ask: radio mode (`karaoke -r`, the TUI's mic mode)
    publishes its songrec anchor through :mod:`karaoke.live_state`, and record
    mode leaves identification marks in the database.
    """
    from . import live_state, localcache

    state = live_state.current()
    if state:
        pos = live_state.position(state)
        if pos is not None:
            return {"source": state["source"], "artist": state.get("artist") or "",
                    "title": state.get("title") or "", "position_s": pos}

    now = time.time()
    checked_at, cached = _recording_cache
    if now - checked_at > _RECORDING_RECHECK_S:
        try:
            with localcache.connect() as conn:
                cached = live_state.recording_playhead(conn, now=now)
        except Exception as exc:
            log.debug("stage_view: recording playhead lookup failed: %s", exc)
            cached = None
        _recording_cache[:] = [now, cached]
        checked_at = now
    if cached:
        return {**cached, "position_s": cached["position_s"] + (now - checked_at)}
    return None


def _chord_state(track_id: Optional[int], position_s: float,
                 duration: float) -> dict[str, Any]:
    """The chord under the playhead, filling the track's timeline if missing."""
    from . import chord_timeline, localcache

    empty = {"chord": None, "chord_roman": None, "next_chord": None,
             "next_chord_in": None, "harmony_style": None}
    if track_id is None:
        return empty
    checked_at, timeline = _chord_cache.get(track_id, (0.0, None))
    if timeline is None and time.time() - checked_at > _CHORD_RECHECK_S:
        try:
            with localcache.connect() as conn:
                timeline = chord_timeline.load(track_id, conn)
        except Exception as exc:
            log.debug("stage_view: chord timeline lookup failed: %s", exc)
        _chord_cache[track_id] = (time.time(), timeline)
        if timeline is None:
            chord_timeline.fill_in_background(track_id)
    if not timeline or not chord_timeline.matches_playback(timeline, duration):
        return empty
    out = chord_timeline.at(timeline, position_s)
    out["harmony_style"] = (timeline.get("harmony") or {}).get("style")
    return out


def get_stage_state() -> dict[str, Any]:
    """Gather live playback, lyrics, queue, and audio analysis state for the stage display."""
    from . import localcache, player_open, playerctl

    # 1. Determine playback state and position
    artist = ""
    title = ""
    album = ""
    url = ""
    duration = 0.0
    position_s = 0.0
    status = "Stopped"
    is_casting = False
    art_url = ""

    # Check kiosk browser playback first (has sub-frame precision & cast state)
    try:
        b_state = player_open.browser_playback(timeout=0.08)
    except Exception:
        b_state = None

    if b_state and b_state.get("present"):
        position_s = float(b_state.get("position") or 0.0)
        duration = float(b_state.get("duration") or 0.0)
        url = str(b_state.get("url") or "")
        is_casting = bool(b_state.get("casting"))
        status = "Paused" if b_state.get("paused") else "Playing"

    # Query MPRIS for artist/title/art
    active_player = playerctl.playing_player() or ""
    meta = playerctl.current_metadata(active_player) if active_player else None
    if meta:
        artist = meta.artist or ""
        title = meta.title or ""
        album = meta.album or ""
        if not url:
            url = meta.url or ""
        if duration <= 0.0 and meta.duration:
            duration = float(meta.duration)
        if position_s <= 0.0 and active_player:
            try:
                pos = playerctl.position(active_player)
                if pos is not None:
                    position_s = float(pos)
            except Exception:
                pass
        if active_player:
            art_url = playerctl.art_url(active_player) or ""
            status = playerctl.status(active_player) or status

    # A desktop player that is actually playing wins. Otherwise a live
    # listener may know what the room is hearing -- and a paused browser tab
    # left in MPRIS must not hide it.
    sync_source = "player" if (artist or title) else ""
    if status != "Playing":
        live = _live_listener()
        if live is not None:
            artist, title = live["artist"], live["title"]
            album, url, art_url = "", "", ""
            # Unknown: a songrec match carries no length. The chord overlay
            # treats that as nothing to compare rather than a mismatch.
            duration = 0.0
            position_s = live["position_s"]
            status = "Playing"
            sync_source = live["source"]

    # 2. Look up lyrics and audio analysis from local database
    lines: list[dict[str, Any]] = []
    cached_ly = None
    # Reuse the player's timeline so the dashboard detects instrumental breaks
    # exactly the way the TUI does (LyricTimeline.in_gap / gap_progress).
    timeline = None
    bpm: Optional[float] = None
    key: Optional[str] = None
    energy: Optional[float] = None
    chord_cpm: Optional[float] = None
    fifth_ratio: Optional[float] = None
    track_id: Optional[int] = None

    if artist and title:
        try:
            with localcache.connect() as conn:
                # 0a. Prefer URL match first (canonical for browser tabs where MPRIS artist is the uploader)
                if url:
                    found = localcache.find_track_by_url(url, conn)
                    if found:
                        track_id = found[0]

                # 0b. Check if title is in "Artist - Title" format (common when player reports uploader as artist)
                if track_id is None and any(sep in title for sep in (" - ", " – ", " — ")):
                    import re
                    parts = re.split(r"\s*[-–—]\s*", title, maxsplit=1)
                    if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                        # If the split matches a known library track, adopt the clean artist and title
                        clean_a, clean_t = parts[0].strip(), parts[1].strip()
                        split_tid = localcache.find_track_id(clean_a, clean_t, conn) or localcache.find_track_id_relaxed(clean_a, clean_t, conn)
                        if split_tid is not None:
                            track_id = split_tid
                            artist = clean_a
                            title = clean_t

                if track_id is None:
                    track_id = localcache.find_track_id(artist, title, conn) or localcache.find_track_id_relaxed(artist, title, conn)

                # 0c. If artist is an unmatched uploader/channel handle, fall back to a unique resolved track with this title
                if track_id is None:
                    track_id = localcache.find_track_id_for_unmatched_uploader(artist, title, conn)

                # If track_id was found by URL or relaxed match, canonicalize artist and title from the database!
                if track_id is not None:
                    cur = conn.cursor()
                    cur.execute("SELECT artist, title FROM tracks WHERE track_id = %s", (track_id,))
                    t_row = cur.fetchone()
                    if t_row and t_row["artist"] and t_row["title"]:
                        # If current artist looks like a raw uploader handle and db has the real artist in title or artist
                        artist = t_row["artist"]
                        title = t_row["title"]

                if track_id is not None:
                    av_mode: Optional[str] = None
                    try:
                        raw_av_mode = player_open.cdp_get_av(timeout=0.08)
                        if raw_av_mode in ("audio", "video"):
                            av_mode = raw_av_mode
                    except Exception:
                        pass
                    cached_ly = localcache.get_lyrics_by_track_id(track_id, conn, media_type=av_mode)
                    if cached_ly and cached_ly.synced_raw:
                        parsed_lines, ends, word_times = parse_enhanced_lrc(cached_ly.synced_raw)
                        for idx, (t, txt) in enumerate(parsed_lines):
                            lines.append({
                                "index": idx,
                                "time": round(t, 2),
                                "end": round(ends[idx], 2) if idx in ends else None,
                                "text": txt,
                                "words": [round(wt, 2) for wt in word_times[idx]] if idx in word_times else [],
                            })
                    elif cached_ly and cached_ly.lines:
                        for idx, (t, txt) in enumerate(cached_ly.lines):
                            lines.append({
                                "index": idx,
                                "time": round(t, 2),
                                "end": None,
                                "text": txt,
                                "words": [],
                            })
                    elif cached_ly and cached_ly.plain:
                        # Fallback for plain unsynced lyrics: supply lines so dashboard displays the text
                        plain_lines = [l.strip() for l in cached_ly.plain.splitlines() if l.strip()]
                        for idx, txt in enumerate(plain_lines):
                            lines.append({
                                "index": idx,
                                "time": None,
                                "end": None,
                                "text": txt,
                                "words": [],
                            })

                    if cached_ly and (cached_ly.synced_raw or cached_ly.lines):
                        try:
                            from .player import timeline_from_lyrics
                            timeline = timeline_from_lyrics(cached_ly)
                        except Exception as exc:
                            log.debug("stage_view: timeline build failed: %s", exc)

                    # Look up BPM, key, energy
                    try:
                        from . import track_analysis
                        track_analysis.ensure_schema(conn)
                        row = conn.execute(
                            "SELECT bpm, detected_key, energy FROM track_analysis WHERE track_id = %s",
                            (track_id,),
                        ).fetchone()
                        if row:
                            bpm = float(row["bpm"]) if row["bpm"] is not None else None
                            key = str(row["detected_key"]) if row["detected_key"] else None
                            energy = float(row["energy"]) if row["energy"] is not None else None
                    except Exception:
                        pass

        except Exception as exc:
            log.debug("stage_view: db lookup failed: %s", exc)

    if track_id is not None:
        try:
            from . import osclient
            from .key_progression import PROGRESSION_INDEX, doc_id
            from opensearchpy.exceptions import NotFoundError
            client = osclient.client()
            if client:
                try:
                    resp = client.get(index=PROGRESSION_INDEX, id=doc_id(track_id))
                    if resp and resp.get("found"):
                        src = resp["_source"]
                        chord_cpm = src.get("chord_changes_per_minute")
                        fifth_ratio = src.get("fifth_ratio")
                except NotFoundError:
                    pass
        except Exception as exc:
            log.debug("stage_view: opensearch chord lookup failed: %s", exc)

    # 3. Look up active upcoming queue items
    upcoming_queue: list[dict[str, Any]] = []
    try:
        active_q = localcache.load_active_queue()
        if active_q and active_q.get("rows"):
            rows = active_q["rows"]
            cur_idx = int(active_q.get("current_index", 0))

            # Anchor "UP NEXT" on the song that is actually playing, not on the
            # stored current_index -- that index only advances when the local
            # queue is being followed, so after a manual/YT-Music play it goes
            # stale and UP NEXT lists tracks relative to the wrong row (even
            # skipping an unplayed row 0). Find the now-playing track in the
            # queue by track_id, then by artist+title, and list what follows it.
            def _norm(s: Any) -> str:
                return str(s or "").strip().casefold()

            anchor = None
            for i, r in enumerate(rows):
                if track_id is not None and r.get("track_id") == track_id:
                    anchor = i
                    break
            if anchor is None and (artist or title):
                for i, r in enumerate(rows):
                    if _norm(r.get("artist")) == _norm(artist) and _norm(r.get("title")) == _norm(title):
                        anchor = i
                        break

            if anchor is not None:
                # Playing song is in the queue: show the rows after it.
                start = anchor + 1
            else:
                # Playing song is not in the queue (manual/external play): the
                # current_index row has not been consumed, so include it.
                start = cur_idx

            for r in rows[start: start + 4]:
                upcoming_queue.append({
                    "artist": r.get("artist", ""),
                    "title": r.get("title", ""),
                })
    except Exception:
        pass

    # 4. Find active line index and countdown to next line
    active_line_idx = -1
    next_line_in: Optional[float] = None
    for idx, line in enumerate(lines):
        t = line["time"]
        end_t = line.get("end")
        if t <= position_s:
            if end_t is None or position_s <= end_t:
                active_line_idx = idx
        elif t > position_s:
            next_line_in = round(t - position_s, 1)
            break

    # Instrumental break: mirror the TUI's rest marker. The timeline flags the
    # stretch between the (capped) end of the active line and the next line's
    # start when it is at least GAP_MIN_S long, so the prompter can show a
    # musical-note countdown instead of a frozen lyric during the break.
    in_gap = False
    gap_progress = 0.0
    if timeline is not None:
        try:
            if timeline.in_gap(position_s):
                in_gap = True
                gap_progress = round(timeline.gap_progress(position_s), 3)
        except Exception as exc:
            log.debug("stage_view: gap detection failed: %s", exc)
    
    mood = "neutral"
    mood_alt: Optional[str] = None
    mood_feeling: Optional[str] = None
    mood_contrast: Optional[str] = None
    mood_source = ""
    genre = ""
    # The track's mood timeline (sound + lyric meaning, per ten seconds) is
    # the best answer when it exists; the line and song lexicon below are the
    # fallback for tracks not analysed yet.
    segment = _mood_segment(track_id, artist, title, position_s, duration)
    if segment is not None:
        mood, mood_alt, mood_source = segment["mood"], segment.get("alt"), "timeline"
        # Version 2 timelines carry the finer word and any sound/words tension.
        mood_feeling, mood_contrast = segment.get("feeling") or None, segment.get("contrast")
    elif active_line_idx >= 0 and active_line_idx < len(lines):
        try:
            from karaoke.visuals import mood_of
            mood = mood_of(lines[active_line_idx]["text"])
            mood_source = "line" if mood != "neutral" else ""
        except Exception:
            pass

    if mood == "neutral":
        try:
            from . import live_caption
            hist = live_caption.get_caption_history()
            if hist:
                from .sentiment import mood_of as speech_mood_of
                # Words only: the room mic's loudness says how intense the
                # room is, not what it feels (see sentiment.mood_of).
                speech_m = speech_mood_of(hist[-1].get("text", ""))
                if speech_m != "neutral":
                    mood, mood_source = speech_m, "room"
        except Exception:
            pass

    if mood == "neutral":
        # Most lines carry no mood word (71% measured), so the song's overall
        # lyric mood is the steady background between the lines that do.
        mood = _track_mood(track_id, lines)
        mood_source = "song" if mood != "neutral" else ""

    if track_id is not None:
        try:
            with localcache.connect() as conn:
                g = localcache.genre_for(track_id, conn)
                if isinstance(g, dict):
                    genre = str(g.get("genre") or "")
                elif g:
                    genre = str(g)
        except Exception:
            pass

    return {
        "artist": artist,
        "title": title,
        "album": album,
        "art_url": art_url,
        "duration": round(duration, 2),
        "position_s": round(position_s, 2),
        "status": status,
        "casting": is_casting,
        "bpm": bpm,
        "key": key,
        "energy": energy,
        "mood": mood,
        # Set when the stretch sits on a boundary: show both, not a guess.
        "mood_alt": mood_alt,
        "mood_feeling": mood_feeling,
        "mood_contrast": mood_contrast,
        "mood_source": mood_source,
        "genre": genre,
        "sync_source": sync_source,
        "chord_cpm": chord_cpm,
        "fifth_ratio": fifth_ratio,
        **_chord_state(track_id, position_s, duration),
        "active_line_index": active_line_idx,
        "next_line_in": next_line_in,
        "in_gap": in_gap,
        "gap_progress": gap_progress,
        "has_synced": bool(cached_ly and (cached_ly.synced_raw or cached_ly.lines)),
        "lines": lines,
        "upcoming_queue": upcoming_queue,
        "timestamp": time.time(),
    }


async def stage_event_stream(interval: float = 0.1) -> AsyncGenerator[str, None]:
    """Yield Server-Sent Events (SSE) broadcasting live stage state at 10Hz."""
    while True:
        try:
            state = get_stage_state()
            payload = json.dumps(state)
            yield f"data: {payload}\n\n"
        except Exception as exc:
            log.debug("stage_event_stream error: %s", exc)
            yield f"data: {json.dumps({'status': 'error', 'detail': str(exc)})}\n\n"
        await asyncio.sleep(interval)


def render_stage_html() -> str:
    """Render the responsive, full-screen Stage View HTML page."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Karaoke Stage View · Live Prompter</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;600;800;900&family=JetBrains+Mono:wght@400;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-dark: #07090e;
      --bg-card: rgba(18, 22, 34, 0.75);
      --border: rgba(255, 255, 255, 0.08);
      --accent: #00f2fe;
      --accent-glow: rgba(0, 242, 254, 0.4);
      --accent-alt: #ff007f;
      --text: #ffffff;
      --text-muted: rgba(255, 255, 255, 0.45);
      --text-dim: rgba(255, 255, 255, 0.2);
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background-color: var(--bg-dark);
      background-image: radial-gradient(circle at 50% 10%, rgba(0, 242, 254, 0.08) 0%, transparent 60%),
                        radial-gradient(circle at 80% 90%, rgba(255, 0, 127, 0.06) 0%, transparent 50%);
      color: var(--text);
      font-family: 'Outfit', sans-serif;
      height: 100vh;
      overflow: hidden;
      display: flex;
      flex-direction: column;
      user-select: none;
    }

    /* Top Bar Header */
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 1.5rem 2.5rem;
      background: var(--bg-card);
      backdrop-filter: blur(20px);
      border-bottom: 1px solid var(--border);
      z-index: 10;
    }
    .track-info {
      display: flex;
      align-items: center;
      gap: 1.25rem;
    }
    .track-art {
      width: 4rem;
      height: 4rem;
      border-radius: 0.75rem;
      background: rgba(255, 255, 255, 0.05);
      object-fit: cover;
      box-shadow: 0 4px 20px rgba(0, 0, 0, 0.4);
      display: none;
    }
    .track-titles h1 {
      font-size: 1.75rem;
      font-weight: 800;
      letter-spacing: -0.02em;
      line-height: 1.1;
      max-width: 50vw;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .track-titles h2 {
      font-size: 1.1rem;
      color: var(--text-muted);
      font-weight: 600;
      margin-top: 0.2rem;
    }
    .header-badges {
      display: flex;
      align-items: center;
      gap: 0.75rem;
    }
    .badge {
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.85rem;
      font-weight: 700;
      padding: 0.4rem 0.85rem;
      border-radius: 2rem;
      background: rgba(255, 255, 255, 0.05);
      border: 1px solid var(--border);
      display: flex;
      align-items: center;
      gap: 0.4rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    .badge.live {
      background: rgba(0, 242, 254, 0.12);
      border-color: rgba(0, 242, 254, 0.3);
      color: var(--accent);
    }
    .badge.cast {
      background: rgba(255, 0, 127, 0.12);
      border-color: rgba(255, 0, 127, 0.3);
      color: var(--accent-alt);
      display: none;
    }
    .pulse-dot {
      width: 0.5rem;
      height: 0.5rem;
      border-radius: 50%;
      background: currentColor;
      box-shadow: 0 0 10px currentColor;
    }

    /* Main Lyrics Prompter */
    main {
      flex: 1;
      display: flex;
      flex-direction: column;
      justify-content: center;
      align-items: center;
      padding: 2rem 4rem;
      position: relative;
      overflow: hidden;
    }
    #lyrics-container {
      width: 100%;
      max-width: 1400px;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 1.5rem;
      text-align: center;
      transition: transform 0.3s cubic-bezier(0.2, 0, 0, 1);
    }
    .lyric-line {
      font-size: 2rem;
      font-weight: 600;
      color: var(--text-dim);
      transition: all 0.35s ease;
      line-height: 1.35;
      max-width: 90%;
    }
    .lyric-line.prev {
      font-size: 2.2rem;
      color: var(--text-muted);
      opacity: 0.6;
      transform: translateY(10px);
    }
    .lyric-line.active {
      font-size: 4rem;
      font-weight: 900;
      color: #ffffff;
      text-shadow: 0 0 35px var(--accent-glow), 0 0 15px var(--accent);
      transform: scale(1.04);
      opacity: 1;
    }
    .lyric-line.active .word-highlight {
      color: var(--accent);
      text-shadow: 0 0 25px var(--accent);
    }
    .lyric-line.next {
      font-size: 2.2rem;
      color: var(--text-muted);
      opacity: 0.7;
      transform: translateY(-10px);
    }
    .lyric-line.upcoming {
      font-size: 1.8rem;
      color: var(--text-dim);
      opacity: 0.35;
    }

    .interlude-banner {
      font-family: 'JetBrains Mono', monospace;
      font-size: 1.1rem;
      color: var(--accent);
      background: rgba(0, 242, 254, 0.08);
      border: 1px solid rgba(0, 242, 254, 0.2);
      padding: 0.6rem 1.4rem;
      border-radius: 2rem;
      margin-top: 1rem;
      display: none;
      animation: pulse 2s infinite ease-in-out;
    }
    @keyframes pulse {
      0%, 100% { opacity: 0.7; transform: scale(1); }
      50% { opacity: 1; transform: scale(1.03); }
    }

    /* Rhythm Beat Visualizer */
    .rhythm-bar {
      position: absolute;
      top: 0;
      left: 0;
      right: 0;
      height: 3px;
      background: linear-gradient(90deg, var(--accent), var(--accent-alt));
      opacity: 0.3;
      transform-origin: center;
      transition: transform 0.1s ease-out;
    }

    /* Footer: Progress & Queue */
    footer {
      padding: 1.25rem 2.5rem;
      background: var(--bg-card);
      backdrop-filter: blur(20px);
      border-top: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      gap: 0.75rem;
      z-index: 10;
    }
    .progress-container {
      display: flex;
      align-items: center;
      gap: 1rem;
    }
    .time-label {
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.9rem;
      color: var(--text-muted);
      min-width: 3.5rem;
    }
    .progress-track {
      flex: 1;
      height: 6px;
      background: rgba(255, 255, 255, 0.08);
      border-radius: 3px;
      overflow: hidden;
      position: relative;
    }
    .progress-fill {
      height: 100%;
      width: 0%;
      background: linear-gradient(90deg, var(--accent), var(--accent-alt));
      border-radius: 3px;
      box-shadow: 0 0 12px var(--accent-glow);
      transition: width 0.15s linear;
    }
    .up-next-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 0.95rem;
      color: var(--text-muted);
    }
    .up-next-label {
      font-weight: 700;
      color: var(--text);
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }
    .shortcut-hint {
      font-size: 0.8rem;
      color: var(--text-dim);
    }
  </style>
</head>
<body>
  <div class="rhythm-bar" id="rhythm-bar"></div>

  <header>
    <div class="track-info">
      <img id="track-art" class="track-art" src="" alt="Album Art">
      <div class="track-titles">
        <h1 id="track-title">Waiting for playback…</h1>
        <h2 id="track-artist">Start a song on Karaoke or cast from YouTube Music</h2>
      </div>
    </div>
    <div class="header-badges">
      <div class="badge cast" id="cast-badge">
        <span class="pulse-dot"></span> Casting to Device
      </div>
      <div class="badge" id="keybpm-badge" style="display: none;">
        <span id="bpm-val">--</span> BPM · <span id="key-val">--</span>
      </div>
      <div class="badge live">
        <span class="pulse-dot"></span> Stage Mode
      </div>
    </div>
  </header>

  <main>
    <div id="lyrics-container">
      <div class="lyric-line prev" id="line-prev"></div>
      <div class="lyric-line active" id="line-active">Karaoke Stage Ready</div>
      <div class="lyric-line next" id="line-next"></div>
      <div class="lyric-line upcoming" id="line-upcoming"></div>
    </div>
    <div class="interlude-banner" id="interlude-banner">♪ Instrumental Break ♪</div>
  </main>

  <footer>
    <div class="progress-container">
      <span class="time-label" id="time-current">0:00</span>
      <div class="progress-track">
        <div class="progress-fill" id="progress-fill"></div>
      </div>
      <span class="time-label" id="time-total">0:00</span>
    </div>
    <div class="up-next-row">
      <div class="up-next-label">
        <span>Up Next:</span>
        <span id="up-next-track" style="font-weight: 500; color: var(--text-muted);">Queue empty</span>
      </div>
      <div class="shortcut-hint">Press [F] for Fullscreen</div>
    </div>
  </footer>

  <script>
    let currentState = null;
    let interpolatedPos = 0;
    let lastEventTime = performance.now();

    function formatTime(s) {
      if (!s || isNaN(s) || s < 0) return "0:00";
      const m = Math.floor(s / 60);
      const sec = Math.floor(s % 60);
      return `${m}:${sec.toString().padStart(2, '0')}`;
    }

    // Connect to SSE stream
    const evtSource = new EventSource("/api/stage/stream");

    evtSource.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        currentState = data;
        interpolatedPos = data.position_s || 0;
        lastEventTime = performance.now();
        updateUI(data);
      } catch (err) {
        console.error("Failed to parse SSE stage state:", err);
      }
    };

    evtSource.onerror = () => {
      document.getElementById("track-title").textContent = "Connecting to Stage Stream…";
    };

    function updateUI(data) {
      if (!data.title) {
        document.getElementById("track-title").textContent = "Waiting for music…";
        document.getElementById("track-artist").textContent = "Pick a song in Karaoke or cast YouTube Music";
        document.getElementById("line-active").textContent = "Ready for Next Song";
        document.getElementById("line-prev").textContent = "";
        document.getElementById("line-next").textContent = "";
        document.getElementById("line-upcoming").textContent = "";
        document.getElementById("keybpm-badge").style.display = "none";
        document.getElementById("cast-badge").style.display = "none";
        document.getElementById("track-art").style.display = "none";
        return;
      }

      document.getElementById("track-title").textContent = data.title;
      document.getElementById("track-artist").textContent = data.artist || "Unknown Artist";

      // Album art
      const artImg = document.getElementById("track-art");
      if (data.art_url) {
        let newSrc = "/api/art?url=" + encodeURIComponent(data.art_url);
        // Only update if URL actually changed to prevent flicker
        if (!artImg.src.endsWith(newSrc)) {
            artImg.src = newSrc;
        }
        artImg.style.display = "block";
      } else {
        artImg.style.display = "none";
      }

      // Cast badge
      const castBadge = document.getElementById("cast-badge");
      castBadge.style.display = data.casting ? "flex" : "none";

      // Key & BPM badge
      const kbBadge = document.getElementById("keybpm-badge");
      if (data.bpm || data.key) {
        kbBadge.style.display = "flex";
        document.getElementById("bpm-val").textContent = data.bpm ? Math.round(data.bpm) : "--";
        document.getElementById("key-val").textContent = data.key || "--";
      } else {
        kbBadge.style.display = "none";
      }

      // Up-Next
      const nextEl = document.getElementById("up-next-track");
      if (data.upcoming_queue && data.upcoming_queue.length > 0) {
        const u = data.upcoming_queue[0];
        nextEl.textContent = `${u.artist} - ${u.title}`;
      } else {
        nextEl.textContent = "(None queued)";
      }

      // Duration & Progress
      document.getElementById("time-total").textContent = formatTime(data.duration);
    }

    // High-frequency animation loop for smooth lyrics & progress interpolation
    function renderLoop(now) {
      if (currentState && currentState.status === "Playing") {
        const dt = (now - lastEventTime) / 1000;
        const currentPos = interpolatedPos + dt;

        document.getElementById("time-current").textContent = formatTime(currentPos);
        if (currentState.duration > 0) {
          const pct = Math.min(100, (currentPos / currentState.duration) * 100);
          document.getElementById("progress-fill").style.width = `${pct}%`;
        }

        // Rhythm bar beat pulse
        if (currentState.bpm) {
          const beatPeriod = 60 / currentState.bpm;
          const phase = (currentPos % beatPeriod) / beatPeriod;
          const scale = 1 + 0.5 * Math.sin(phase * Math.PI);
          document.getElementById("rhythm-bar").style.transform = `scaleY(${scale})`;
        }

        // Update lyric lines
        renderLyrics(currentPos);
      }
      requestAnimationFrame(renderLoop);
    }
    requestAnimationFrame(renderLoop);

    function renderLyrics(pos) {
      if (!currentState || !currentState.lines || currentState.lines.length === 0) {
        document.getElementById("line-active").textContent = currentState && currentState.title ? "♪ Playing ♪" : "Ready";
        document.getElementById("line-prev").textContent = "";
        document.getElementById("line-next").textContent = "";
        document.getElementById("line-upcoming").textContent = "";
        document.getElementById("interlude-banner").style.display = "none";
        return;
      }

      const lines = currentState.lines;
      let activeIdx = -1;
      let nextTime = null;

      for (let i = 0; i < lines.length; i++) {
        const l = lines[i];
        if (l.time <= pos) {
          if (!l.end || pos <= l.end) {
            activeIdx = i;
          }
        } else {
          nextTime = l.time;
          break;
        }
      }

      const prevEl = document.getElementById("line-prev");
      const activeEl = document.getElementById("line-active");
      const nextEl = document.getElementById("line-next");
      const upEl = document.getElementById("line-upcoming");
      const interludeEl = document.getElementById("interlude-banner");

      if (activeIdx >= 0) {
        prevEl.textContent = activeIdx > 0 ? lines[activeIdx - 1].text : "";
        activeEl.textContent = lines[activeIdx].text;
        nextEl.textContent = activeIdx + 1 < lines.length ? lines[activeIdx + 1].text : "";
        upEl.textContent = activeIdx + 2 < lines.length ? lines[activeIdx + 2].text : "";
        interludeEl.style.display = "none";
      } else {
        // Instrumental break / waiting for first line
        prevEl.textContent = "";
        activeEl.textContent = "♪";
        nextEl.textContent = lines.length > 0 ? lines[0].text : "";
        upEl.textContent = lines.length > 1 ? lines[1].text : "";
        if (nextTime && (nextTime - pos) > 2.5) {
          interludeEl.textContent = `♪ Next verse in ${(nextTime - pos).toFixed(1)}s ♪`;
          interludeEl.style.display = "inline-block";
        } else {
          interludeEl.style.display = "none";
        }
      }
    }

    // Fullscreen toggle on 'F'
    window.addEventListener("keydown", (e) => {
      if (e.key === "f" || e.key === "F") {
        if (!document.fullscreenElement) {
          document.documentElement.requestFullscreen().catch(() => {});
        } else {
          document.exitFullscreen().catch(() => {});
        }
      }
    });
  </script>
</body>
</html>"""


def render_coverart_html() -> str:
    """Render a dedicated full-screen cover art view that pulses to the beat."""
    return """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Karaoke Cover Art</title>
  <style>
    body {
      margin: 0; padding: 0; background: #000; overflow: hidden;
      display: flex; align-items: center; justify-content: center; height: 100vh;
    }
    #art {
      width: 80vmin; height: 80vmin; object-fit: cover; border-radius: 2vmin;
      box-shadow: 0 10px 50px rgba(0,0,0,0.8);
      transition: transform 0.05s ease-out;
    }
    .pulse { transform: scale(1.05); }
  </style>
</head>
<body>
  <img id="art" src="" style="display:none;" />
  <script>
    const artEl = document.getElementById('art');
    let lastPulse = 0;
    
    const es = new EventSource('/api/stage/stream');
    es.onmessage = (event) => {
      const data = JSON.parse(event.data);
      if (data.status === 'error') return;
      
      if (data.art_url) {
        if (artEl.src !== data.art_url) {
          artEl.src = "/api/art?url=" + encodeURIComponent(data.art_url);
          artEl.style.display = 'block';
        }
      } else {
        artEl.style.display = 'none';
      }
      
      if (data.status === 'Playing' && data.bpm) {
        const beatSec = 60.0 / data.bpm;
        const phase = (data.position_s / beatSec) % 1.0;
        // Pulse at the start of the beat
        if (phase < 0.15 && Date.now() - lastPulse > (beatSec * 0.8 * 1000)) {
           artEl.classList.add('pulse');
           lastPulse = Date.now();
           setTimeout(() => artEl.classList.remove('pulse'), Math.min(150, beatSec * 500));
        }
      }
    };
  </script>
</body>
</html>
"""

def render_dancers_html() -> str:
    """Render a CSS/JS dancer visualization reacting to music metrics."""
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Karaoke Dancers</title>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@700&display=swap" rel="stylesheet">
  <style>
    body {
      margin: 0; padding: 0; background: #07090e; overflow: hidden;
      display: flex; flex-direction: column; height: 100vh; color: #fff;
      font-family: 'JetBrains Mono', monospace;
    }
    #stage-container {
      flex: 1; display: flex; flex-direction: column; justify-content: flex-end;
      padding-bottom: 15vh; position: relative;
    }
    .layer {
      position: absolute; left: 0; width: 100%; display: flex;
      justify-content: center; align-items: flex-end; gap: 4vw;
    }
    .bg-layer { bottom: 35vh; opacity: 0.3; transform: scale(0.6); z-index: 1; gap: 2vw; }
    .fg-layer { bottom: 10vh; z-index: 10; }
    
    .dancer-wrapper {
      transition: transform 0.2s ease-out, margin 0.3s ease-in-out;
      display: flex; align-items: flex-end; justify-content: center;
    }
    .pose {
      white-space: pre; font-size: 1vw; line-height: 1; text-align: center;
      transition: transform 0.08s ease-out, color 0.3s;
    }
    
    /* Foreground styles */
    .fg-layer .pose { font-size: 2vw; text-shadow: 0 0 20px rgba(0, 242, 254, 0.5); }
    .jazz-style .fg-layer .pose { color: #ff007f; text-shadow: 0 0 20px rgba(255, 0, 127, 0.5); }
    
    /* Outer dancers pushed back */
    .dancer-wrapper.outer {
      transform: scale(0.55) translateY(-30px);
      opacity: 0.7;
      z-index: 5;
      margin: 0 4vw; /* spacing from center */
    }
    
    /* Inner duet dancers */
    .dancer-wrapper.inner {
      z-index: 15;
      margin: 0 2vw;
    }
    
    /* Together cue */
    .fg-layer.together .dancer-wrapper.inner {
      margin: 0 -4vw; /* Overlap them */
    }
    .fg-layer.together .dancer-wrapper.inner.right .pose {
      transform: rotate(180deg) translateY(20%); /* Upside down and shifted to interlock */
      color: #00f2fe;
    }
    .jazz-style .fg-layer.together .dancer-wrapper.inner.right .pose {
      color: #ff007f;
    }
    
    .hop .pose { transform: translateY(-15%); }
    
    #hud {
      position: absolute;
      top: 2rem;
      right: 2rem;
      display: flex;
      flex-direction: column;
      align-items: flex-end;
      gap: 0.5rem;
      z-index: 50;
      opacity: 0.8;
      transition: opacity 0.3s;
    }
    #hud img {
      width: 120px;
      height: 120px;
      border-radius: 8px;
      object-fit: cover;
      box-shadow: 0 4px 15px rgba(0,0,0,0.5);
      display: none;
    }
    #hud .sentiment {
      font-size: 1rem;
      font-weight: bold;
      text-transform: uppercase;
      letter-spacing: 2px;
      text-shadow: 0 2px 4px rgba(0,0,0,0.8);
      color: #00f2fe;
    }

    .fg-layer.together .hop .dancer-wrapper.inner.right .pose {
      transform: rotate(180deg) translateY(5%);
    }
  </style>
</head>
<body>
  <div id="hud">
    <img id="hud-art" src="" alt="Cover Art" />
    <div id="hud-sentiment" class="sentiment"></div>
  </div>
  <div id="stage-container">
    <div id="bg" class="layer bg-layer"></div>
    <div id="fg" class="layer fg-layer"></div>
  </div>
  <script>

    let POSES = [
      " o \n/|\\\n/ \\",
      "\\o/\n | \n/ \\"
    ];
    let LIBRARY = null;
    let ACTIVE_CLIP = null;
    
    // Fetch the dance pack
    fetch('/api/dance-library')
      .then(res => res.json())
      .then(data => {
         if (data.clips) {
           LIBRARY = data;
           console.log("Loaded dance library with", data.clip_count, "clips");
         }
      })
      .catch(err => console.error("Failed to load dance library", err));
      
    function mirrorPose(pose) {
      return pose.split('\n').map(line => {
        return line.split('').reverse().map(c => {
          if (c === '/') return '\\';
          if (c === '\\') return '/';
          return c;
        }).join('');
      }).join('\n');
    }
    
    const bgContainer = document.getElementById('bg');
    const fgContainer = document.getElementById('fg');
    const stageContainer = document.getElementById('stage-container');
    
    const NUM_BG = 12;
    const bgDancers = [];
    const fgDancers = [];
    
    for(let i=0; i<NUM_BG; i++) {
       let wrapper = document.createElement('div');
       wrapper.className = 'dancer-wrapper';
       let poseEl = document.createElement('div');
       poseEl.className = 'pose';
       wrapper.appendChild(poseEl);
       bgContainer.appendChild(wrapper);
       bgDancers.push({el: wrapper, poseEl: poseEl, phase: i * 1.618});
    }
    
    // Foreground: 4 dancers (Outer, Inner Left, Inner Right, Outer)
    const fgRoles = ['outer left', 'inner left', 'inner right', 'outer right'];
    fgRoles.forEach((role, i) => {
       let wrapper = document.createElement('div');
       wrapper.className = 'dancer-wrapper ' + role;
       let poseEl = document.createElement('div');
       poseEl.className = 'pose';
       wrapper.appendChild(poseEl);
       fgContainer.appendChild(wrapper);
       fgDancers.push({el: wrapper, poseEl: poseEl, phase: i * 1.618, isRight: role.includes('right')});
    });
    
    const es = new EventSource('/api/stage/stream');
    es.onmessage = (event) => {
      const data = JSON.parse(event.data);
      if (data.status !== 'Playing') return;
      
      const bpm = data.bpm || 90;
      const beatSec = 60.0 / Math.max(bpm, 1);
      const beats = data.position_s / beatSec;
      const energy = data.energy !== null ? data.energy : 0.5;
      const mood = data.mood || 'neutral';
      
      // Update HUD
      const hudArt = document.getElementById('hud-art');
      if (data.art_url) {
         hudArt.src = '/api/art?url=' + encodeURIComponent(data.art_url);
         hudArt.style.display = 'block';
      } else {
         hudArt.style.display = 'none';
      }
      
      const hudSentiment = document.getElementById('hud-sentiment');
      const genreStr = data.genre ? data.genre : '';
      hudSentiment.textContent = mood + (genreStr ? ' • ' + genreStr : '');

      
      // Determine style based on chords
      const isJazz = data.fifth_ratio > 0.4 && data.chord_cpm > 10;
      if (isJazz) stageContainer.className = 'jazz-style';
      else stageContainer.className = '';
      
      
      let poseIndices = [0];
      if (LIBRARY) {
         // Find a solo clip that matches mood or feeling
         // Map mood/energy to feeling
         let targetFeeling = 'joyful';
         if (energy > 0.8) targetFeeling = 'excited';
         if (mood === 'tender') targetFeeling = 'tender';
         if (mood === 'sad') targetFeeling = 'melancholy';
         
         const validClips = LIBRARY.clips.filter(c => c.mode === 'solo' && (c.feeling === targetFeeling || c.feeling === 'joyful'));
         if (validClips.length > 0) {
            ACTIVE_CLIP = validClips[0];
            POSES = ACTIVE_CLIP.frames;
            poseIndices = POSES.map((_, idx) => idx);
         }
      } else {
         poseIndices = [0, 1];
      }
      
      const speed = 1.0 + energy * 0.8;
      
      // Together cue: phrase > 0.70
      const phrase = (beats / 8.0) % 1.0;
      const isTogether = phrase > 0.70;
      if (isTogether) fgContainer.classList.add('together');
      else fgContainer.classList.remove('together');
      
// Update foreground
      fgDancers.forEach((d, i) => {
         const dBeats = beats * speed + d.phase;
         const pidx = Math.floor(dBeats * 2) % poseIndices.length;
         let poseStr = POSES[poseIndices[pidx] % POSES.length];
         
         // Inner right dancer mirrors the inner left dancer during duet
         if (d.isRight) poseStr = mirrorPose(poseStr);
         
         d.poseEl.innerText = poseStr;
         
         const within = dBeats % 1.0;
         if (bpm >= 100 && within < 0.2) d.el.classList.add('hop');
         else d.el.classList.remove('hop');
      });
      
      // Update background (slower, ambient)
      bgDancers.forEach((d, i) => {
         const dBeats = beats * 0.5 + d.phase;
         const pidx = Math.floor(dBeats) % POSES.length;
         d.poseEl.innerText = POSES[pidx];
      });
    };
  </script>
</body>
</html>
"""



def render_mood_html() -> str:
    """Render a full-screen mood visualizer page with room mic vibe, feeling art, and randomized cover pixels."""
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Karaoke Mood Visualizer · Vibe & Sound Reactive</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@500;700;800&family=Outfit:wght@600;800;900&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #06080d;
      --card-bg: rgba(13, 17, 27, 0.85);
      --accent: #00f2fe;
      --accent-pink: #ff007f;
      --accent-gold: #ffbe0b;
      --border: rgba(255, 255, 255, 0.12);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0; padding: 0; background: var(--bg); overflow: hidden;
      display: flex; flex-direction: column; justify-content: center; align-items: center;
      height: 100vh; color: #fff; font-family: 'JetBrains Mono', monospace;
      user-select: none;
    }
    
    /* Ambient glow behind visualizer */
    #ambient-glow {
      position: absolute;
      width: 90vmin;
      height: 90vmin;
      border-radius: 50%;
      background: radial-gradient(circle, rgba(0, 242, 254, 0.15) 0%, rgba(255, 0, 127, 0.08) 50%, transparent 70%);
      filter: blur(40px);
      pointer-events: none;
      z-index: 0;
      transition: transform 0.1s ease-out, background 0.8s ease;
    }

    #stage-container {
      position: relative;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      z-index: 1;
    }

    #canvas-wrap {
      position: relative;
      padding: 10px;
      background: rgba(255, 255, 255, 0.03);
      border: 1px solid var(--border);
      border-radius: 18px;
      box-shadow: 0 16px 50px rgba(0, 0, 0, 0.8), 0 0 40px rgba(0, 242, 254, 0.15);
      backdrop-filter: blur(12px);
    }

    #mood-canvas {
      width: min(86vw, 1180px);
      height: auto;
      aspect-ratio: 16 / 9;
      max-height: 76vh;
      border-radius: 12px;
      background: #070910;
      display: block;
    }
    #mood-canvas.mode-mosaic {
      width: min(76vmin, 640px);
      height: min(76vmin, 640px);
      aspect-ratio: 1 / 1;
      image-rendering: pixelated;
      image-rendering: crisp-edges;
    }

    #mode-btn {
      cursor: pointer;
      transition: all 0.2s ease;
      color: #fff;
      pointer-events: auto;
      user-select: none;
    }
    #mode-btn:hover {
      border-color: var(--accent);
      background: rgba(0, 242, 254, 0.15);
      transform: translateY(-1px);
    }

    /* In-canvas floating mode switch pill (visible in both /mood and /mood/embedded) */
    #canvas-mode-switch {
      position: absolute;
      top: 12px;
      right: 12px;
      z-index: 12;
      display: inline-flex;
      gap: 4px;
      padding: 4px;
      background: rgba(6, 8, 15, 0.72);
      border: 1px solid rgba(255, 255, 255, 0.16);
      border-radius: 999px;
      backdrop-filter: blur(8px);
      pointer-events: auto;
      user-select: none;
    }
    .cmode-pill {
      appearance: none;
      border: 0;
      background: transparent;
      color: rgba(255, 255, 255, 0.65);
      font-family: 'JetBrains Mono', monospace;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.05em;
      padding: 4px 10px;
      border-radius: 999px;
      cursor: pointer;
      transition: all 0.16s ease;
    }
    .cmode-pill:hover {
      color: #fff;
      background: rgba(255, 255, 255, 0.10);
    }
    .cmode-pill.active {
      color: #06080d;
      background: var(--accent);
      box-shadow: 0 0 12px rgba(0, 242, 254, 0.45);
    }

    /* Chord under the playhead, laid over the image. HTML rather than drawn
       into the canvas: the canvas is 64x64 scaled up, so text drawn there
       would be as blocky as the pixels. */
    #chord-overlay {
      position: absolute;
      left: 22px;
      bottom: 22px;
      padding: 8px 14px 10px;
      background: rgba(5, 8, 14, 0.62);
      border: 1px solid var(--border);
      border-radius: 12px;
      backdrop-filter: blur(6px);
      pointer-events: none;
      opacity: 0;
      transition: opacity 0.4s ease;
    }
    #chord-overlay.visible { opacity: 1; }
    #chord-name {
      font-family: 'Outfit', sans-serif;
      font-size: clamp(28px, 7vmin, 64px);
      font-weight: 700;
      line-height: 1;
      color: #fff;
      text-shadow: 0 0 18px rgba(0, 242, 254, 0.55);
    }
    #chord-name.changed { animation: chord-pop 0.35s ease-out; }
    @keyframes chord-pop {
      from { transform: scale(1.12); color: var(--accent); }
      to { transform: scale(1); color: #fff; }
    }
    #chord-roman {
      margin-top: 4px;
      font-size: clamp(12px, 2.2vmin, 18px);
      color: var(--accent-gold);
      letter-spacing: 0.06em;
    }
    #chord-next {
      margin-top: 2px;
      font-size: clamp(10px, 1.7vmin, 14px);
      color: rgba(255, 255, 255, 0.6);
    }

    /* Top HUD / Mic control pill */
    #hud-top {
      position: absolute;
      top: 24px;
      display: flex;
      align-items: center;
      gap: 16px;
      z-index: 9999;
      pointer-events: auto;
    }

    .hud-pill {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 999px;
      padding: 8px 18px;
      display: flex;
      align-items: center;
      gap: 10px;
      font-size: 0.85rem;
      letter-spacing: 1px;
      box-shadow: 0 6px 20px rgba(0,0,0,0.5);
      backdrop-filter: blur(8px);
      pointer-events: auto;
    }

    #mic-btn {
      cursor: pointer;
      transition: all 0.2s ease;
      color: #fff;
      pointer-events: auto;
      user-select: none;
    }
    #mic-btn:hover {
      border-color: var(--accent);
      background: rgba(0, 242, 254, 0.15);
      transform: translateY(-1px);
    }
    #mic-btn.active {
      border-color: var(--accent-pink);
      color: #fff;
      box-shadow: 0 0 18px rgba(255, 0, 127, 0.35);
    }

    #vu-meter {
      display: inline-flex;
      gap: 2px;
      align-items: center;
      height: 12px;
    }
    .vu-bar {
      width: 3px;
      height: 100%;
      background: rgba(255, 255, 255, 0.2);
      border-radius: 1px;
      transition: background 0.05s ease;
    }
    .vu-bar.lit {
      background: var(--accent);
      box-shadow: 0 0 6px var(--accent);
    }
    .vu-bar.peak {
      background: var(--accent-pink);
      box-shadow: 0 0 8px var(--accent-pink);
    }

    /* Track & Mood info */
    #info-box {
      margin-top: 1.5rem;
      text-align: center;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 6px;
    }
    #sentiment {
      font-family: 'Outfit', sans-serif;
      font-size: 2.2rem;
      font-weight: 900;
      text-transform: uppercase;
      letter-spacing: 5px;
      background: linear-gradient(135deg, #00f2fe 0%, #4facfe 50%, #ff007f 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      text-shadow: 0 4px 20px rgba(0, 242, 254, 0.4);
    }
    #track-info {
      font-size: 0.95rem;
      color: rgba(255, 255, 255, 0.7);
      max-width: 80vw;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      font-weight: 500;
    }
    #sub-stats {
      margin-top: 4px;
      display: flex;
      gap: 14px;
      font-size: 0.75rem;
      color: rgba(255, 255, 255, 0.4);
      letter-spacing: 1.5px;
      text-transform: uppercase;
    }
    .stat-val { color: var(--accent); font-weight: bold; }
  </style>
</head>
<body>
  <div id="ambient-glow"></div>

  <div id="hud-top">
    <div id="mic-btn" class="hud-pill" onclick="toggleMic()">
      <span id="mic-icon">🎙️</span>
      <span id="mic-label">ROOM MIC [R]</span>
      <div id="vu-meter">
        <div class="vu-bar"></div>
        <div class="vu-bar"></div>
        <div class="vu-bar"></div>
        <div class="vu-bar"></div>
        <div class="vu-bar"></div>
        <div class="vu-bar"></div>
        <div class="vu-bar"></div>
        <div class="vu-bar"></div>
      </div>
    </div>
    <div id="mode-btn" class="hud-pill" onclick="toggleVisualMode()">
      <span id="mode-label">MODE: RINGS [M]</span>
    </div>
  </div>

  <div id="stage-container">
    <div id="canvas-wrap">
      <canvas id="mood-canvas" width="1280" height="720"></canvas>
      <div id="canvas-mode-switch" role="group" aria-label="Visualizer mode">
        <button type="button" class="cmode-pill" data-mode="mix" onclick="setVisualMode('mix')" title="Blend pixel mosaic + generative rings [M]">MIX</button>
        <button type="button" class="cmode-pill" data-mode="rings" onclick="setVisualMode('rings')" title="Generative mood rings &amp; chord wave [M]">RINGS</button>
        <button type="button" class="cmode-pill" data-mode="mosaic" onclick="setVisualMode('mosaic')" title="Classic 64x64 pixel animation [M]">PIXELS</button>
      </div>
      <div id="chord-overlay" aria-live="off">
        <div id="chord-name"></div>
        <div id="chord-roman"></div>
        <div id="chord-next"></div>
      </div>
    </div>

    <div id="info-box">
      <div id="sentiment">WAITING FOR MUSIC...</div>
      <div id="track-info">Queue songs to begin</div>
      <div id="sub-stats">
        <span>BPM: <span id="val-bpm" class="stat-val">--</span></span>
        <span>WPM: <span id="val-wpm" class="stat-val">--</span></span>
        <span>ENERGY: <span id="val-energy" class="stat-val">--</span></span>
        <span>VIBE: <span id="val-vibe" class="stat-val">0%</span></span>
      </div>
    </div>
  </div>

  <script>
    // --- Audio / Mic Room Vibe Engine ---
    let audioCtx = null;
    let micStream = null;
    let analyser = null;
    let micDataArray = null;
    let micActive = false;
    let roomVibeLevel = 0.0;    // 0.0 to 1.0 smoothed room volume
    let roomBassLevel = 0.0;    // low freq energy
    let roomHighsLevel = 0.0;   // crowd/cheering/vocals
    let micPeakImpulse = 0.0;   // sudden transients / claps

    const micBtn = document.getElementById('mic-btn');
    const micLabel = document.getElementById('mic-label');
    const vuBars = Array.from(document.querySelectorAll('.vu-bar'));

    // Click & keyboard shortcut 'r' to capture room mic
    micBtn.addEventListener('click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      toggleMic();
    });

    window.addEventListener('keydown', (e) => {
      if (e.key === 'r' || e.key === 'R') {
        if (e.target && (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA')) return;
        toggleMic();
      }
    });

    // Auto-enable mic on load / first user interaction
    window.addEventListener('DOMContentLoaded', () => {
      toggleMic();
    });
    document.addEventListener('click', (e) => {
      if (!micActive && e.target !== micBtn && !micBtn.contains(e.target)) {
        toggleMic();
      }
    }, { once: true });

    async function toggleMic() {
      if (micActive) {
        if (micStream) {
          micStream.getTracks().forEach(t => t.stop());
        }
        if (audioCtx && audioCtx.state !== 'closed') {
          audioCtx.close();
        }
        micActive = false;
        micBtn.classList.remove('active');
        micLabel.textContent = 'ENABLE ROOM MIC [R]';
        vuBars.forEach(b => b.className = 'vu-bar');
        return;
      }

      try {
        const AudioContextClass = window.AudioContext || window.webkitAudioContext;
        audioCtx = new AudioContextClass();
        if (audioCtx.state === 'suspended') {
          await audioCtx.resume();
        }
        micStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });
        const source = audioCtx.createMediaStreamSource(micStream);
        analyser = audioCtx.createAnalyser();
        analyser.fftSize = 128;
        analyser.smoothingTimeConstant = 0.65;
        source.connect(analyser);

        micDataArray = new Uint8Array(analyser.frequencyBinCount);
        micActive = true;
        micBtn.classList.add('active');
        micLabel.textContent = 'ROOM MIC ACTIVE [R]';
      } catch (err) {
        console.warn('Microphone access failed or denied:', err);
        micLabel.textContent = 'MIC UNAVAILABLE';
        setTimeout(() => { if (!micActive) micLabel.textContent = 'ENABLE ROOM MIC [R]'; }, 3000);
      }
    }

    function updateMicMetrics() {
      if (!micActive || !analyser) {
        // Subtle idle breathing when mic not active
        roomVibeLevel = roomVibeLevel * 0.95;
        roomBassLevel = roomBassLevel * 0.95;
        roomHighsLevel = roomHighsLevel * 0.95;
        micPeakImpulse = micPeakImpulse * 0.9;
        return;
      }

      analyser.getByteFrequencyData(micDataArray);
      const binCount = micDataArray.length;
      
      let sum = 0;
      let bassSum = 0;
      let highsSum = 0;
      const bassBins = Math.max(1, Math.floor(binCount * 0.25));
      const midBins = Math.floor(binCount * 0.65);

      for (let i = 0; i < binCount; i++) {
        const val = micDataArray[i];
        sum += val;
        if (i < bassBins) bassSum += val;
        else if (i >= midBins) highsSum += val;
      }

      const rawLevel = sum / (binCount * 255);
      const rawBass = bassSum / (bassBins * 255);
      const rawHighs = highsSum / ((binCount - midBins) * 255);

      // Fast attack, smooth decay
      const attack = 0.35;
      const decay = 0.15;
      roomVibeLevel = rawLevel > roomVibeLevel 
        ? (roomVibeLevel * (1 - attack) + rawLevel * attack) 
        : (roomVibeLevel * (1 - decay) + rawLevel * decay);

      roomBassLevel = rawBass > roomBassLevel 
        ? (roomBassLevel * 0.6 + rawBass * 0.4) 
        : (roomBassLevel * 0.85 + rawBass * 0.15);

      roomHighsLevel = rawHighs > roomHighsLevel 
        ? (roomHighsLevel * 0.6 + rawHighs * 0.4) 
        : (roomHighsLevel * 0.85 + rawHighs * 0.15);

      // Detect sharp transient spikes (singing accents, cheers, claps)
      const instantDelta = rawLevel - roomVibeLevel;
      if (instantDelta > 0.15) {
        micPeakImpulse = Math.min(1.0, micPeakImpulse + instantDelta * 2.0);
      } else {
        micPeakImpulse *= 0.88;
      }

      // Update VU bars
      const numBars = vuBars.length;
      const litCount = Math.round(roomVibeLevel * numBars * 1.4);
      vuBars.forEach((bar, idx) => {
        if (idx < litCount) {
          bar.className = (idx >= numBars - 2) ? 'vu-bar peak' : 'vu-bar lit';
        } else {
          bar.className = 'vu-bar';
        }
      });
    }

    // --- Visualizer Rendering & Layering ---
    const moodCanvas = document.getElementById('mood-canvas');
    const moodCanvasCtx = moodCanvas.getContext('2d', { willReadFrequently: true });
    const sentimentDiv = document.getElementById('sentiment');
    const trackInfoDiv = document.getElementById('track-info');
    const valBpm = document.getElementById('val-bpm');
    const valWpm = document.getElementById('val-wpm');
    const valEnergy = document.getElementById('val-energy');
    const valVibe = document.getElementById('val-vibe');
    const ambientGlow = document.getElementById('ambient-glow');

    let currentState = null;
    let lastEventTime = performance.now();
    let currentMood = 'neutral';
    let currentFeeling = 'neutral';
    let currentArtUrl = '';
    let currentLoadSeed = Date.now();
    let lastBeatCount = 0;

    // Off-screen canvas buffers
    const GRID_SIZE = 64;
    const feelingBuffer = document.createElement('canvas');
    feelingBuffer.width = GRID_SIZE; feelingBuffer.height = GRID_SIZE;
    const feelingCtx = feelingBuffer.getContext('2d');
    let feelingLoaded = false;

    const coverBuffer = document.createElement('canvas');
    coverBuffer.width = GRID_SIZE; coverBuffer.height = GRID_SIZE;
    const coverCtx = coverBuffer.getContext('2d');
    let coverLoaded = false;

    // --- Visual Mode Switch (MIX: Pixels+Rings, RINGS: Generative, MOSAIC: Classic 64x64 Pixels) ---
    const IS_EMBEDDED = window.location.pathname.includes('embedded');
    const VALID_MODES = ['mix', 'rings', 'mosaic'];
    const urlMode = new URLSearchParams(window.location.search).get('mode');
    let savedMode = null;
    try { savedMode = localStorage.getItem('karaoke_mood_visual_mode'); } catch (e) {}
    let visualMode = VALID_MODES.includes(urlMode)
      ? urlMode
      : (VALID_MODES.includes(savedMode) ? savedMode : 'mix');
    const modeLabel = document.getElementById('mode-label');

    // Offscreen 64x64 mosaic buffer so MIX mode can composite crisp pixel art with rings
    const mosaicBuffer = document.createElement('canvas');
    mosaicBuffer.width = 64;
    mosaicBuffer.height = 64;
    const mosaicCtx = mosaicBuffer.getContext('2d');

    function syncCanvasGeometry() {
      if (!moodCanvas) return;
      if (visualMode === 'mosaic') {
        if (moodCanvas.width !== 64 || moodCanvas.height !== 64) {
          moodCanvas.width = 64;
          moodCanvas.height = 64;
        }
        moodCanvas.style.imageRendering = 'pixelated';
        moodCanvas.classList.add('mode-mosaic');
        return;
      }
      moodCanvas.style.imageRendering = 'auto';
      moodCanvas.classList.remove('mode-mosaic');
      // Match internal buffer aspect ratio to the displayed CSS box so circles are never squashed
      let targetW = IS_EMBEDDED ? 720 : 1280;
      let targetH = 720;
      const rect = moodCanvas.getBoundingClientRect();
      if (rect && rect.width > 40 && rect.height > 40) {
        const aspect = rect.width / rect.height;
        targetH = 720;
        targetW = Math.max(360, Math.min(1920, Math.round(720 * aspect)));
      }
      if (Math.abs(moodCanvas.width - targetW) > 4 || Math.abs(moodCanvas.height - targetH) > 4) {
        moodCanvas.width = targetW;
        moodCanvas.height = targetH;
      }
    }

    function applyVisualModeUI() {
      const labels = { mix: 'MODE: MIX [M]', rings: 'MODE: RINGS [M]', mosaic: 'MODE: PIXELS [M]' };
      if (modeLabel) {
        modeLabel.textContent = labels[visualMode] || 'MODE: MIX [M]';
      }
      document.querySelectorAll('.cmode-pill').forEach(btn => {
        btn.classList.toggle('active', btn.getAttribute('data-mode') === visualMode);
      });
      syncCanvasGeometry();
    }

    window.addEventListener('resize', () => {
      if (visualMode !== 'mosaic') syncCanvasGeometry();
    });

    function setVisualMode(mode) {
      if (!VALID_MODES.includes(mode)) return;
      visualMode = mode;
      try { localStorage.setItem('karaoke_mood_visual_mode', visualMode); } catch (e) {}
      applyVisualModeUI();
    }

    function toggleVisualMode() {
      const idx = VALID_MODES.indexOf(visualMode);
      setVisualMode(VALID_MODES[(idx + 1) % VALID_MODES.length]);
    }

    window.addEventListener('storage', (e) => {
      if (e.key === 'karaoke_mood_visual_mode' && VALID_MODES.includes(e.newValue)) {
        visualMode = e.newValue;
        applyVisualModeUI();
      }
    });

    window.addEventListener('keydown', (e) => {
      if (e.key === 'm' || e.key === 'M') {
        if (e.target && (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA')) return;
        toggleVisualMode();
      }
    });

    // --- Generative Mood-Still Color & Harmonic Math ---
    const NOTE_SEMITONES = {
      'c': 0, 'c#': 1, 'db': 1,
      'd': 2, 'd#': 3, 'eb': 3,
      'e': 4, 'fb': 4, 'e#': 5,
      'f': 5, 'f#': 6, 'gb': 6,
      'g': 7, 'g#': 8, 'ab': 8,
      'a': 9, 'a#': 10, 'bb': 10,
      'b': 11, 'cb': 11, 'b#': 0
    };

    function parsePitchClass(text) {
      if (!text) return null;
      const m = String(text).match(new RegExp('^\\s*([A-Ga-g])([#b♯♭]?)'));
      if (!m) return null;
      const note = m[1].toLowerCase();
      const acc = (m[2] || '').replace('♯', '#').replace('♭', 'b');
      const key = note + acc;
      return (key in NOTE_SEMITONES) ? NOTE_SEMITONES[key] : (note in NOTE_SEMITONES ? NOTE_SEMITONES[note] : null);
    }

    function keyHueFromStr(keyStr) {
      if (!keyStr) return 0.68; // G# minor / 1A default
      const pc = parsePitchClass(keyStr);
      if (pc === null) return 0.68;
      return ((pc % 12) + 12) % 12 / 12.0;
    }

    function isMinorKey(keyStr) {
      if (!keyStr) return true;
      const s = String(keyStr).toLowerCase();
      if (s.includes('minor')) return true;
      if (s.includes('major')) return false;
      return new RegExp('^\\s*[a-g][#b]?m\\b').test(s);
    }

    function moodHueShift(mood) {
      if (!mood) return 0.0;
      const shifts = {
        'tense': 0.06, 'energetic': 0.09, 'angry': 0.15, 'aggressive': 0.14,
        'sad': -0.06, 'melancholy': -0.04, 'mournful': -0.05,
        'calm': 0.01, 'peaceful': 0.02, 'dreamy': 0.04, 'tender': 0.03,
        'happy': 0.08, 'upbeat': 0.07, 'playful': 0.06, 'energized': 0.09
      };
      return shifts[String(mood).toLowerCase().trim()] || 0.0;
    }

    function chordHueOffset(roman) {
      if (!roman) return 0.0;
      const base = String(roman).replace(/[b#m°769]/gi, '').trim().toUpperCase();
      const shifts = { 'I': 0.0, 'II': -0.02, 'III': -0.01, 'IV': 0.04, 'V': 0.06, 'VI': -0.02, 'VII': 0.08 };
      return shifts[base] || 0.0;
    }

    function chordFrequency(name) {
      if (!name) return 261.63;
      let pc = parsePitchClass(name);
      if (pc === null) pc = 0;
      return 440.0 * Math.pow(2.0, (pc - 9.0) / 12.0);
    }

    function progressionShape(chordName) {
      if (!chordName) return { mode: 'stable', rays: 0.0, ringMod: 0.0, wave: 'sine' };
      const n = String(chordName).trim().toLowerCase();
      if (n.includes('°') || n.includes('dim')) {
        return { mode: 'jagged', rays: 0.14, ringMod: 0.14, wave: 'sawtooth' };
      }
      if (n.includes('7') || n.includes('9') || n.includes('11') || n.includes('13')) {
        if (n.includes('m') && !n.includes('maj')) {
          return { mode: 'sway', rays: 0.04, ringMod: 0.05, wave: 'sine' };
        }
        return { mode: 'rich', rays: 0.08, ringMod: 0.06, wave: 'sawtooth' };
      }
      if (n.includes('m') && !n.includes('maj')) {
        return { mode: 'sway', rays: 0.03, ringMod: 0.05, wave: 'sine' };
      }
      return { mode: 'stable', rays: 0.0, ringMod: 0.0, wave: 'flat' };
    }

    function hsvToRgb(h, s, v) {
      h = ((h % 1.0) + 1.0) % 1.0;
      s = Math.min(1.0, Math.max(0.0, s));
      v = Math.min(1.0, Math.max(0.0, v));
      if (s === 0) {
        const c = Math.round(v * 255);
        return [c, c, c];
      }
      const f = (h * 6.0) - Math.floor(h * 6.0);
      const i = Math.floor(h * 6.0) % 6;
      const p = v * (1.0 - s);
      const q = v * (1.0 - s * f);
      const t = v * (1.0 - s * (1.0 - f));
      const map = [
        [v, t, p], [q, v, p], [p, v, t],
        [p, q, v], [t, p, v], [v, p, q]
      ][i];
      return [Math.round(map[0] * 255), Math.round(map[1] * 255), Math.round(map[2] * 255)];
    }

    function rgbaStr(rgb, a) {
      return `rgba(${rgb[0]}, ${rgb[1]}, ${rgb[2]}, ${Math.min(1, Math.max(0, a)).toFixed(3)})`;
    }

    // Deterministic 36 ray angles (seeded)
    const RAY_ANGLES = (() => {
      const out = [];
      let s = 1337;
      for (let i = 0; i < 36; i++) {
        s = (s * 16807) % 2147483647;
        out.push(((s - 1) / 2147483646) * Math.PI * 2);
      }
      return out;
    })();

    // Smoothed animation state (interpolated at 60 FPS so chord/mood shifts glide)
    let smoothHue = 0.68;
    let smoothEnergy = 0.55;
    let smoothFreq = 261.63;
    let smoothRingMod = 0.0;
    let smoothRayDensity = 0.0;

    function lerpAngleHue(curr, target, t) {
      let diff = ((target - curr + 0.5) % 1.0 + 1.0) % 1.0 - 0.5;
      return ((curr + diff * t) % 1.0 + 1.0) % 1.0;
    }

    function activeHighlightedWord(state, posS) {
      if (!state || !Array.isArray(state.lines)) return '';
      const idx = state.active_line_index;
      if (typeof idx !== 'number' || idx < 0 || idx >= state.lines.length) return '';
      const line = state.lines[idx];
      if (!line || !line.text) return '';
      const stripRe = new RegExp('^[|·•\\-–—,:;.!?()\\[\\]"\']+|[|·•\\-–—,:;.!?()\\[\\]"\']+$', 'g');
      const cleanWord = (w) => String(w || '').replace(stripRe, '');
      const words = String(line.text).trim().split(new RegExp('\\s+')).map(cleanWord).filter(w => w.length >= 2);
      if (!words.length) return '';
      const times = Array.isArray(line.words) ? line.words : [];
      if (times.length === words.length) {
        let wIdx = 0;
        for (let i = 0; i < times.length; i++) {
          if (posS >= times[i]) wIdx = i;
        }
        return words[wIdx] || '';
      }
      const t0 = typeof line.time === 'number' ? line.time : posS;
      const t1 = typeof line.end === 'number' && line.end > t0 ? line.end : (t0 + 3.5);
      const frac = Math.min(0.999, Math.max(0.0, (posS - t0) / Math.max(0.2, t1 - t0)));
      return words[Math.floor(frac * words.length)] || '';
    }

    function normalizeGenreStr(g) {
      if (!g) return '';
      if (typeof g === 'object') return String(g.genre || '');
      const s = String(g).trim();
      if (s.startsWith('{') && s.includes("'genre'")) {
        const m = s.match(new RegExp("'genre'\\s*:\\s*'([^']+)'"));
        return m ? m[1] : '';
      }
      return s;
    }

    function renderGenerativeRingsFrame(ctx, W, H, posS, beatProgress, beatPulse, soundPower) {
      const st = currentState || {};
      const isEmbedded = H === W || window.location.pathname.includes('embedded');
      const rawEnergy = (st.energy !== null && st.energy !== undefined) ? Number(st.energy) : 0.55;
      const bpm = Number(st.bpm) || 120.0;
      const keyStr = st.key || 'G# minor';
      const minor = isMinorKey(keyStr);
      const moodStr = st.mood_feeling || st.mood || currentMood || 'neutral';
      const chordNameStr = st.chord || '';
      const chordRomanStr = st.chord_roman || '';
      const shape = progressionShape(chordNameStr);

      const targetHue = (keyHueFromStr(keyStr) + moodHueShift(moodStr) + chordHueOffset(chordRomanStr) + 1.0) % 1.0;
      const targetFreq = chordFrequency(chordNameStr || chordRomanStr || keyStr);

      // Smooth 60 FPS state interpolation (~250ms time constant)
      smoothHue = lerpAngleHue(smoothHue, targetHue, 0.08);
      smoothEnergy = smoothEnergy + (rawEnergy - smoothEnergy) * 0.08;
      smoothFreq = smoothFreq + (targetFreq - smoothFreq) * 0.10;
      smoothRingMod = smoothRingMod + (shape.ringMod - smoothRingMod) * 0.10;
      smoothRayDensity = smoothRayDensity + (shape.rays - smoothRayDensity) * 0.10;

      const energy = Math.min(1.0, Math.max(0.15, smoothEnergy + (micActive ? roomVibeLevel * 0.25 : 0)));
      let fieldSat = Math.min(0.95, Math.max(0.22, 0.42 + 0.36 * energy));
      let fieldVal = Math.min(0.65, Math.max(0.14, 0.18 + 0.20 * energy));
      if (minor) {
        fieldSat *= 0.88;
        fieldVal *= 0.84;
      }

      const cx = W * 0.5;
      const cy = isEmbedded ? H * 0.5 : H * 0.44;
      const minDim = Math.min(W, H);
      const maxR = Math.max(W, H) * 0.72;

      // 1) Key-tinted radial background field
      const colCore = hsvToRgb(smoothHue, Math.min(0.90, fieldSat * 1.15), Math.min(0.42, fieldVal * 1.25));
      const colMid = hsvToRgb(smoothHue + 0.03, fieldSat, fieldVal * 0.78);
      const colDeep = hsvToRgb(smoothHue - 0.04, fieldSat * 0.85, Math.max(0.06, fieldVal * 0.36));

      const bgCore = [
        Math.round(28 * 0.55 + colCore[0] * 0.45),
        Math.round(22 * 0.55 + colCore[1] * 0.45),
        Math.round(62 * 0.55 + colCore[2] * 0.45)
      ];
      const bgMid = [
        Math.round(16 * 0.55 + colMid[0] * 0.45),
        Math.round(18 * 0.55 + colMid[1] * 0.45),
        Math.round(38 * 0.55 + colMid[2] * 0.45)
      ];
      const bgDeep = [
        Math.round(7 * 0.65 + colDeep[0] * 0.35),
        Math.round(9 * 0.65 + colDeep[1] * 0.35),
        Math.round(16 * 0.65 + colDeep[2] * 0.35)
      ];

      const bgGrad = ctx.createRadialGradient(cx, cy, 4, cx, cy, maxR);
      bgGrad.addColorStop(0.0, rgbaStr(bgCore, 1.0));
      bgGrad.addColorStop(0.52, rgbaStr(bgMid, 1.0));
      bgGrad.addColorStop(1.0, rgbaStr(bgDeep, 1.0));
      ctx.fillStyle = bgGrad;
      ctx.fillRect(0, 0, W, H);

      // 1b) In MIX mode, composite the 64x64 sound-reactive pixel mosaic inside the ring field
      if (visualMode === 'mix' && (feelingLoaded || coverLoaded)) {
        ctx.save();
        const mosRadius = minDim * (isEmbedded ? 0.47 : 0.43);
        ctx.beginPath();
        ctx.arc(cx, cy, mosRadius, 0, Math.PI * 2);
        ctx.clip();
        ctx.imageSmoothingEnabled = false;
        ctx.globalAlpha = Math.min(0.76, 0.46 + soundPower * 0.28);
        const mosSize = mosRadius * 2.0;
        ctx.drawImage(mosaicBuffer, cx - mosRadius, cy - mosRadius, mosSize, mosSize);
        // Soft radial vignette at the mosaic perimeter so pixel tiles blend into the dark field
        const edgeGrad = ctx.createRadialGradient(cx, cy, mosRadius * 0.48, cx, cy, mosRadius);
        edgeGrad.addColorStop(0.0, 'rgba(7, 9, 16, 0.0)');
        edgeGrad.addColorStop(0.75, 'rgba(7, 9, 16, 0.22)');
        edgeGrad.addColorStop(1.0, rgbaStr(bgMid, 0.88));
        ctx.globalAlpha = 1.0;
        ctx.fillStyle = edgeGrad;
        ctx.fillRect(cx - mosRadius, cy - mosRadius, mosSize, mosSize);
        ctx.restore();
      }

      // Continuous beat phase & surge
      const ringFreq = (0.6 + (bpm / 120.0)) + smoothRingMod * 0.8;
      const phase = (posS * (bpm / 60.0) * 0.32) % 1000.0;
      const phaseR = phase * ringFreq / 1.6;
      const surge = Math.min(1.0, 0.52 + 0.48 * Math.max(energy * 0.75, beatPulse * 0.85 + (micActive ? micPeakImpulse * 0.5 : 0)));

      // 2) Radiating starburst rays
      const colRay = hsvToRgb(smoothHue + 0.04, Math.min(0.85, fieldSat * 0.95), Math.min(0.78, 0.38 + 0.34 * energy));
      const rayLenBase = minDim * (0.44 + 0.10 * surge + smoothRayDensity * 0.25);
      const rayAlphaBase = 0.045 + 0.045 * energy + smoothRayDensity * 0.08;
      ctx.save();
      ctx.lineCap = 'round';
      for (let i = 0; i < RAY_ANGLES.length; i++) {
        const a = RAY_ANGLES[i] + Math.sin(posS * 0.18 + i) * 0.025;
        const breathe = 0.82 + 0.18 * Math.sin(phaseR * 1.4 + i * 0.7);
        const rLen = rayLenBase * breathe;
        const x2 = cx + Math.cos(a) * rLen;
        const y2 = cy + Math.sin(a) * rLen;
        const rGrad = ctx.createLinearGradient(cx, cy, x2, y2);
        rGrad.addColorStop(0.0, rgbaStr(colRay, rayAlphaBase * 1.4));
        rGrad.addColorStop(0.6, rgbaStr(colRay, rayAlphaBase * 0.6));
        rGrad.addColorStop(1.0, rgbaStr(colRay, 0.0));
        ctx.strokeStyle = rGrad;
        ctx.lineWidth = (minDim / 1080) * (10 + (i % 3) * 4);
        ctx.beginPath();
        ctx.moveTo(cx, cy);
        ctx.lineTo(x2, y2);
        ctx.stroke();
      }
      ctx.restore();

      // 3) Off-center orbiting crimson/terracotta tension flare
      const scaleFactor = minDim / 1080.0;
      const flareOrbitX = cx + (115.0 * scaleFactor) * Math.cos(phaseR * 0.85);
      const flareOrbitY = cy + (95.0 * scaleFactor) * Math.sin(phaseR * 0.95);
      const flareSpotR = (235.0 * scaleFactor) * (0.9 + 0.2 * surge);
      const colFlare = [255, 92, 84];
      const flareSpotGrad = ctx.createRadialGradient(flareOrbitX, flareOrbitY, 2, flareOrbitX, flareOrbitY, flareSpotR);
      flareSpotGrad.addColorStop(0.0, rgbaStr(colFlare, 0.36 + 0.22 * energy));
      flareSpotGrad.addColorStop(0.45, rgbaStr(colFlare, 0.16 + 0.12 * energy));
      flareSpotGrad.addColorStop(1.0, rgbaStr(colFlare, 0.0));
      ctx.fillStyle = flareSpotGrad;
      ctx.beginPath();
      ctx.arc(flareOrbitX, flareOrbitY, flareSpotR, 0, Math.PI * 2);
      ctx.fill();

      // 4) 9 Concentric circular rings + beat-flare halos
      const colRing = hsvToRgb(smoothHue + 0.07, Math.min(0.92, fieldSat * 1.15), Math.min(0.84, 0.48 + 0.34 * energy));
      const colPulse = hsvToRgb(smoothHue - 0.08, Math.min(0.88, 0.55 + 0.30 * energy), Math.min(0.94, 0.62 + 0.28 * energy));
      const RING_CNT = 9;
      const stepScale = (minDim * 0.050) * (1.0 + 0.25 * smoothRingMod);

      for (let i = RING_CNT - 1; i >= 0; i--) {
        const baseR = (i + 1) * stepScale;
        const arg = phaseR * Math.PI * 1.35 - i * (0.85 + smoothRingMod);
        let osc = 0.74 + 0.26 * Math.sin(arg);
        if (shape.wave === 'sawtooth') {
          const t = ((arg % (2 * Math.PI)) + 2 * Math.PI) % (2 * Math.PI) / (2 * Math.PI);
          osc = 0.72 + 0.28 * (0.65 * t + 0.35 * Math.sin(arg));
        } else if (shape.wave === 'triangle') {
          const t = ((arg % (2 * Math.PI)) + 2 * Math.PI) % (2 * Math.PI) / (2 * Math.PI);
          const tri = 1.0 - 4.0 * Math.abs(t - 0.5);
          osc = 0.72 + 0.28 * (tri * 0.5 + 0.5);
        } else if (shape.wave === 'flat') {
          osc = 0.78 + 0.12 * Math.sin(arg * 0.7 + i * 0.9);
        }

        const r = Math.max(4, baseR * osc * (1.0 + beatPulse * 0.04));
        const bw = (9.0 + (i % 4) * 3.0) * scaleFactor;
        const weight = ((RING_CNT + 1 - i) / RING_CNT) * (0.55 + 0.35 * surge);
        const flareW = 0.48 * (0.6 + 0.4 * surge) * (0.5 + 0.5 * Math.cos(i * 2.3 + phaseR * 2.6));

        // Wide warm beat-flare halo
        ctx.beginPath();
        ctx.arc(cx, cy, r, 0, Math.PI * 2);
        ctx.strokeStyle = rgbaStr(colFlare, Math.min(0.55, flareW * (0.35 + 0.25 * energy)));
        ctx.lineWidth = bw * 4.2;
        ctx.stroke();

        // Main key-coloured ring band
        ctx.beginPath();
        ctx.arc(cx, cy, r, 0, Math.PI * 2);
        ctx.strokeStyle = rgbaStr(colRing, Math.min(0.75, weight * (0.48 + 0.30 * energy)));
        ctx.lineWidth = bw * 1.9;
        ctx.stroke();

        // Crisp inner pulse highlight
        ctx.beginPath();
        ctx.arc(cx, cy, r, 0, Math.PI * 2);
        ctx.strokeStyle = rgbaStr(colPulse, Math.min(0.65, weight * flareW * 0.75));
        ctx.lineWidth = Math.max(1.5, bw * 0.75);
        ctx.stroke();
      }

      // 5) Central glowing orb & white-hot core disc
      const coreR = (142.0 * scaleFactor) * (0.52 + 0.48 * surge);
      const glowR = coreR * 3.3;
      const colGlow = [235, 170, 210];
      const colCoreHot = [255, 238, 210];

      const glowGrad = ctx.createRadialGradient(cx, cy, 1, cx, cy, glowR);
      glowGrad.addColorStop(0.0, rgbaStr(colGlow, 0.42 + 0.18 * energy));
      glowGrad.addColorStop(0.45, rgbaStr(colGlow, 0.16 + 0.10 * energy));
      glowGrad.addColorStop(1.0, rgbaStr(colGlow, 0.0));
      ctx.fillStyle = glowGrad;
      ctx.beginPath();
      ctx.arc(cx, cy, glowR, 0, Math.PI * 2);
      ctx.fill();

      const coreGrad = ctx.createRadialGradient(cx, cy, 1, cx, cy, coreR);
      coreGrad.addColorStop(0.0, rgbaStr([255, 248, 232], 0.92));
      coreGrad.addColorStop(0.42, rgbaStr(colCoreHot, 0.65 + 0.20 * energy));
      coreGrad.addColorStop(1.0, rgbaStr(colCoreHot, 0.0));
      ctx.fillStyle = coreGrad;
      ctx.beginPath();
      ctx.arc(cx, cy, coreR, 0, Math.PI * 2);
      ctx.fill();

      // 6) Harmonic chord waveform ribbon across the frame
      if (chordNameStr || chordRomanStr || st.status === 'Playing') {
        const waveHue = ((smoothFreq / 440.0) + chordHueOffset(chordRomanStr) + 1.0) % 1.0;
        const waveRgb = hsvToRgb(waveHue, 0.88, 0.95);
        const playheadX = W * (0.5 + 0.36 * Math.sin(posS * 0.55));
        const cycles = 3.5 + (smoothFreq / 110.0) * 1.8;
        const amp = H * (0.042 + 0.024 * surge);

        ctx.save();
        // Outer neon halo ribbon
        ctx.beginPath();
        const stepX = Math.max(3, Math.floor(W / 280));
        for (let x = 0; x <= W; x += stepX) {
          const ph = (x / W) * cycles * Math.PI * 2.0 - posS * 3.0;
          let osc = Math.sin(ph) + 0.25 * Math.sin(ph * 2.0 + 0.6);
          if (shape.wave === 'sawtooth') {
            // Smooth 3-harmonic Fourier sawtooth so high-tension chords look edgy without vertical glitches
            osc = 0.65 * Math.sin(ph) - 0.30 * Math.sin(2.0 * ph) + 0.16 * Math.sin(3.0 * ph);
          } else if (shape.wave === 'triangle') {
            const t = ((ph % (2 * Math.PI)) + 2 * Math.PI) % (2 * Math.PI) / (2 * Math.PI);
            osc = 1.0 - 4.0 * Math.abs(t - 0.5);
          }
          const dPlay = (x - playheadX) / (W * 0.42);
          const env = 0.48 + 0.52 * Math.exp(-(dPlay * dPlay));
          const y = cy + amp * osc * env;
          if (x === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        }
        ctx.strokeStyle = rgbaStr(waveRgb, 0.26);
        ctx.lineWidth = 10 * scaleFactor;
        ctx.stroke();

        // Inner bright ribbon core
        ctx.strokeStyle = rgbaStr(waveRgb, 0.82);
        ctx.lineWidth = 2.6 * scaleFactor;
        ctx.stroke();
        ctx.restore();
      }

      // 7) Soft lower vignette + in-canvas title card on full /mood page
      if (!isEmbedded) {
        const vigGrad = ctx.createLinearGradient(0, H * 0.56, 0, H);
        vigGrad.addColorStop(0.0, 'rgba(7, 9, 16, 0.0)');
        vigGrad.addColorStop(1.0, 'rgba(7, 9, 16, 0.55)');
        ctx.fillStyle = vigGrad;
        ctx.fillRect(0, H * 0.56, W, H * 0.44);

        const titleText = String(st.title || 'KARAOKE STANDBY').toUpperCase();
        const artistText = String(st.artist || '');
        const hiWord = activeHighlightedWord(st, posS).toUpperCase();
        const cleanGenre = normalizeGenreStr(st.genre);
        const metaParts = [];
        if (artistText) metaParts.push(artistText);
        if (st.bpm) metaParts.push(`${Math.round(st.bpm)} BPM`);
        if (st.key) metaParts.push(String(st.key));
        if (cleanGenre) metaParts.push(cleanGenre.toLowerCase());
        if (st.energy !== null && st.energy !== undefined) metaParts.push(`energy ${Math.round(st.energy * 100)}%`);
        if (chordNameStr) metaParts.push(chordRomanStr && chordRomanStr !== chordNameStr ? `${chordNameStr} (${chordRomanStr})` : chordNameStr);
        const metaText = metaParts.join('  ·  ');

        ctx.save();
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';

        let titleSize = Math.round(52 * scaleFactor);
        ctx.font = `800 ${titleSize}px 'Outfit', sans-serif`;
        const maxTextW = W * 0.78;
        while (titleSize > 26 && ctx.measureText(titleText).width > maxTextW) {
          titleSize -= 4;
          ctx.font = `800 ${titleSize}px 'Outfit', sans-serif`;
        }
        const tw = ctx.measureText(titleText).width;

        const wordSize = Math.round(25 * scaleFactor);
        let metaSize = Math.round(18 * scaleFactor);
        ctx.font = `600 ${metaSize}px 'JetBrains Mono', monospace`;
        while (metaText && metaSize > 12 && ctx.measureText(metaText).width > maxTextW) {
          metaSize -= 2;
          ctx.font = `600 ${metaSize}px 'JetBrains Mono', monospace`;
        }
        const mw = metaText ? ctx.measureText(metaText).width : 0;

        const panelPadX = 48 * scaleFactor;
        const hasSubRow = Boolean(hiWord || metaText);
        const panelH = (hiWord && metaText ? 150 : (hasSubRow ? 116 : 82)) * scaleFactor;
        const panelW = Math.min(W - 60, Math.max(W * 0.44, Math.max(tw, mw) + panelPadX * 2));
        const panelX = (W - panelW) * 0.5;
        const panelY = H - panelH - 22 * scaleFactor;

        // Translucent rounded panel
        ctx.fillStyle = 'rgba(6, 7, 15, 0.64)';
        ctx.strokeStyle = 'rgba(255, 255, 255, 0.10)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        if (ctx.roundRect) {
          ctx.roundRect(panelX, panelY, panelW, panelH, 16 * scaleFactor);
        } else {
          ctx.rect(panelX, panelY, panelW, panelH);
        }
        ctx.fill();
        ctx.stroke();

        // Title line (vertically centered if no sub-rows)
        let curY = hasSubRow
          ? (panelY + (hiWord && metaText ? 36 : 42) * scaleFactor)
          : (panelY + panelH * 0.5);
        ctx.font = `800 ${titleSize}px 'Outfit', sans-serif`;
        ctx.fillStyle = 'rgba(0, 0, 0, 0.72)';
        ctx.fillText(titleText, cx + 2, curY + 3);
        ctx.fillStyle = 'rgb(255, 238, 210)';
        ctx.fillText(titleText, cx, curY);

        // Active lyric word (if any)
        if (hiWord) {
          curY = panelY + (metaText ? 82 : 80) * scaleFactor;
          ctx.font = `700 ${wordSize}px 'Outfit', sans-serif`;
          ctx.fillStyle = 'rgba(0, 0, 0, 0.7)';
          ctx.fillText(hiWord, cx + 2, curY + 2);
          ctx.fillStyle = 'rgb(218, 192, 242)';
          ctx.fillText(hiWord, cx, curY);
        }

        // Metadata row
        if (metaText) {
          curY = panelY + panelH - 26 * scaleFactor;
          ctx.font = `500 ${metaSize}px 'JetBrains Mono', monospace`;
          ctx.fillStyle = 'rgba(0, 0, 0, 0.7)';
          ctx.fillText(metaText, cx + 2, curY + 2);
          ctx.fillStyle = 'rgb(202, 192, 236)';
          ctx.fillText(metaText, cx, curY);
        }
        ctx.restore();
      }
    }

    // Stable random seed & pseudo-random permutation table for album cover pixels
    let pixelPermutation = new Int32Array(GRID_SIZE * GRID_SIZE);
    let pixelThresholds = new Float32Array(GRID_SIZE * GRID_SIZE);
    let pixelJitter = new Float32Array(GRID_SIZE * GRID_SIZE * 2);

    function reseedPixelRandomizer(seed) {
      let s = seed % 2147483647;
      if (s <= 0) s += 2147483646;
      function rnd() {
        s = (s * 16807) % 2147483647;
        return (s - 1) / 2147483646;
      }

      const total = GRID_SIZE * GRID_SIZE;
      for (let i = 0; i < total; i++) {
        pixelPermutation[i] = i;
        pixelThresholds[i] = rnd();
        // Random spatial jitter offset for mosaic scattering
        pixelJitter[i * 2] = (rnd() - 0.5) * 8.0;
        pixelJitter[i * 2 + 1] = (rnd() - 0.5) * 8.0;
      }
      // Shuffle pixel permutation so album art is heavily scrambled/randomized
      for (let i = total - 1; i > 0; i--) {
        const j = Math.floor(rnd() * (i + 1));
        const tmp = pixelPermutation[i];
        pixelPermutation[i] = pixelPermutation[j];
        pixelPermutation[j] = tmp;
      }
    }
    reseedPixelRandomizer(1337);

    // Load feeling image (background)
    function loadFeelingImage(mood) {
      const img = new Image();
      img.onload = () => {
        feelingCtx.clearRect(0, 0, GRID_SIZE, GRID_SIZE);
        feelingCtx.drawImage(img, 0, 0, GRID_SIZE, GRID_SIZE);
        feelingLoaded = true;
      };
      img.onerror = () => { feelingLoaded = false; };
      img.src = `/api/mood-feeling-image?mood=${encodeURIComponent(mood)}&t=${currentLoadSeed}`;
    }

    // Load album cover (foreground pixels)
    function loadCoverImage(mood, artUrl) {
      const img = new Image();
      img.onload = () => {
        coverCtx.clearRect(0, 0, GRID_SIZE, GRID_SIZE);
        // Draw into 64x64 buffer with pixelated quality
        coverCtx.imageSmoothingEnabled = false;
        coverCtx.drawImage(img, 0, 0, GRID_SIZE, GRID_SIZE);
        coverLoaded = true;
      };
      img.onerror = () => { coverLoaded = false; };
      img.src = `/api/mood-cover-image?mood=${encodeURIComponent(mood)}&art_url=${encodeURIComponent(artUrl || '')}&t=${currentLoadSeed}`;
    }

    const chordOverlay = document.getElementById('chord-overlay');
    const chordName = document.getElementById('chord-name');
    const chordRoman = document.getElementById('chord-roman');
    const chordNext = document.getElementById('chord-next');

    function renderChord(data) {
      const playing = data.status === 'Playing';
      const chord = playing ? data.chord : null;
      chordOverlay.classList.toggle('visible', Boolean(chord));
      if (!chord) return;
      if (chordName.textContent !== chord) {
        chordName.textContent = chord;
        chordName.classList.remove('changed');
        void chordName.offsetWidth;            // restart the pop animation
        chordName.classList.add('changed');
      }
      chordRoman.textContent = data.chord_roman || '';
      chordNext.textContent = (data.next_chord && data.next_chord_in !== null)
        ? `next ${data.next_chord} · ${data.next_chord_in.toFixed(1)}s` : '';
    }

    // Server-Sent Events stream from Karaoke stage
    const evtSource = new EventSource('/api/stage/stream');
    evtSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        currentState = data;
        lastEventTime = performance.now();
        renderChord(data);

        if (data.status !== 'Playing') {
          sentimentDiv.textContent = 'PAUSED';
          return;
        }

        const mood = (data.mood || 'neutral').toLowerCase();
        const feeling = (data.mood_feeling || mood).toLowerCase();
        const artUrl = data.art_url || '';
        const title = data.title || '';
        const artist = data.artist || '';

        // A borderline stretch is shown as both moods rather than a guess.
        // The feeling ("FRUSTRATED", "LONGING") when the timeline has one;
        // loadFeelingImage uses the specific feeling (which has its own pixel
        // art in static/images/moods/<feeling>/), while loadCoverImage uses
        // the feeling/mood target in moodart.MOOD_TARGETS.
        const word = (data.mood_feeling || mood).toUpperCase();
        const moodText = (data.mood_feeling || !data.mood_alt ? word : `${word} ↔ ${data.mood_alt.toUpperCase()}`)
          + (data.mood_contrast ? ` · ${data.mood_contrast}` : '');
        sentimentDiv.textContent = moodText + (data.genre ? ' • ' + data.genre : '');
        // Say where the song came from when it is not a player: radio and
        // record mode dead-reckon the playhead, so their timing is an estimate.
        const via = {radio: ' · 📻 radio', 'tui-radio': ' · 📻 radio', recording: ' · ⏺ rec'}[data.sync_source] || '';
        trackInfoDiv.textContent = ((title && artist) ? `${title} — ${artist}` : (title || 'Karaoke Playing')) + via;
        valBpm.textContent = data.bpm ? Math.round(data.bpm) : '120';
        valEnergy.textContent = data.energy !== null ? Math.round(data.energy * 100) + '%' : '50%';

        // Beat tracking for periodic 9th beat randomizer update
        const bpm = data.bpm || 120.0;
        const currentBeat = data.position_s / (60.0 / Math.max(bpm, 1.0));

        let needReload = false;
        if (mood !== currentMood) {
          currentMood = mood;
          needReload = true;
        }
        if (feeling !== currentFeeling) {
          currentFeeling = feeling;
          needReload = true;
        }
        if (artUrl !== currentArtUrl) {
          currentArtUrl = artUrl;
          needReload = true;
        }
        if (!window.lastBeat || Math.abs(currentBeat - window.lastBeat) >= 9.0) {
          window.lastBeat = currentBeat;
          currentLoadSeed = Date.now();
          reseedPixelRandomizer(Math.floor(currentBeat * 997 + Date.now()));
          needReload = true;
        }

        if (needReload || !feelingLoaded || !coverLoaded) {
          loadFeelingImage(currentFeeling);
          loadCoverImage(currentFeeling, currentArtUrl);
        }
      } catch (e) {
        console.error('SSE parse error:', e);
      }
    };

    function updateMosaicBuffer(beatPulse, soundPower) {
      if (!feelingLoaded && !coverLoaded) return false;

      const feelingImgData = feelingLoaded
        ? feelingCtx.getImageData(0, 0, GRID_SIZE, GRID_SIZE)
        : feelingCtx.createImageData(GRID_SIZE, GRID_SIZE);

      const coverImgData = coverLoaded
        ? coverCtx.getImageData(0, 0, GRID_SIZE, GRID_SIZE)
        : coverCtx.createImageData(GRID_SIZE, GRID_SIZE);

      const outImgData = mosaicCtx.createImageData(GRID_SIZE, GRID_SIZE);
      const outData = outImgData.data;
      const fData = feelingImgData.data;
      const cData = coverImgData.data;

      const cx = GRID_SIZE / 2;
      const cy = GRID_SIZE / 2;

      for (let y = 0; y < GRID_SIZE; y++) {
        for (let x = 0; x < GRID_SIZE; x++) {
          const pixelIndex = y * GRID_SIZE + x;
          const byteIndex = pixelIndex * 4;

          const fR = fData[byteIndex];
          const fG = fData[byteIndex + 1];
          const fB = fData[byteIndex + 2];
          const fA = fData[byteIndex + 3];

          const permIndex = pixelPermutation[pixelIndex];
          const jitterX = pixelJitter[pixelIndex * 2] * (0.3 + soundPower * 0.7);
          const jitterY = pixelJitter[pixelIndex * 2 + 1] * (0.3 + soundPower * 0.7);

          const dx = x - cx;
          const dy = y - cy;
          const dist = Math.sqrt(dx * dx + dy * dy);
          const radialPush = micPeakImpulse * 5.0 * (dist / (GRID_SIZE * 0.5));
          const angle = Math.atan2(dy, dx);

          let sampleX = Math.floor((permIndex % GRID_SIZE) + jitterX + Math.cos(angle) * radialPush);
          let sampleY = Math.floor(Math.floor(permIndex / GRID_SIZE) + jitterY + Math.sin(angle) * radialPush);

          sampleX = (sampleX % GRID_SIZE + GRID_SIZE) % GRID_SIZE;
          sampleY = (sampleY % GRID_SIZE + GRID_SIZE) % GRID_SIZE;

          const coverSampleIndex = (sampleY * GRID_SIZE + sampleX) * 4;
          const cR = cData[coverSampleIndex];
          const cG = cData[coverSampleIndex + 1];
          const cB = cData[coverSampleIndex + 2];

          const threshold = pixelThresholds[pixelIndex];
          const showCover = soundPower > (threshold * 0.85);
          const flash = beatPulse * 0.35 + micPeakImpulse * 0.4;

          if (showCover) {
            const coverAlpha = Math.min(1.0, 0.45 + soundPower * 0.55);
            const r = cR * coverAlpha + fR * (1 - coverAlpha);
            const g = cG * coverAlpha + fG * (1 - coverAlpha);
            const b = cB * coverAlpha + fB * (1 - coverAlpha);

            outData[byteIndex] = Math.min(255, r + (255 - r) * flash);
            outData[byteIndex + 1] = Math.min(255, g + (255 - g) * flash);
            outData[byteIndex + 2] = Math.min(255, b + (255 - b) * flash);
            outData[byteIndex + 3] = 255;
          } else {
            outData[byteIndex] = Math.min(255, fR + (255 - fR) * (flash * 0.7));
            outData[byteIndex + 1] = Math.min(255, fG + (255 - fG) * (flash * 0.7));
            outData[byteIndex + 2] = Math.min(255, fB + (255 - fB) * (flash * 0.7));
            outData[byteIndex + 3] = fA || 255;
          }
        }
      }

      mosaicCtx.putImageData(outImgData, 0, 0);
      return true;
    }

    // Listen to live captions stream for real-time speech sentiment & voice intonation
    const captionSource = new EventSource('/api/captions/stream');
    captionSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        if (!data || !data.text) return;
        const text = data.text.toLowerCase();
        let speechMood = 'neutral';
        if (text.includes('!') || text.includes('haha') || text.includes('lol') || text.includes('grapje') || text.includes('lachen')) {
          speechMood = 'happy';
        } else if (text.includes('niet') || text.includes('fout') || text.includes('onzin') || text.includes('stop') || text.includes('ruzie')) {
          speechMood = 'angry';
        } else if (text.includes('valpartij') || text.includes('pech') || text.includes('schade') || text.includes('pijn')) {
          speechMood = 'sad';
        } else if (text.includes('liefde') || text.includes('mooi') || text.includes('fijn') || text.includes('dank')) {
          speechMood = 'tender';
        } else if (data.rms && data.rms > 0.035) {
          speechMood = 'angry';
        } else if (data.rms && data.rms > 0.018) {
          speechMood = 'happy';
        }

        if (data.bpm && valBpm) valBpm.textContent = Math.round(data.bpm);
        if (data.wpm && valWpm) valWpm.textContent = Math.round(data.wpm);

        if (speechMood !== 'neutral' && speechMood !== currentMood) {
          currentMood = speechMood;
          sentimentDiv.textContent = currentMood.toUpperCase() + ' • LIVE SPEECH';
          loadFeelingImage(currentMood);
          loadCoverImage(currentMood, currentArtUrl);
        }
      } catch (e) {
        console.error('Caption SSE error:', e);
      }
    };

    // Render loop running at 60 FPS
    function renderLoop(now) {
      requestAnimationFrame(renderLoop);
      updateMicMetrics();

      valVibe.textContent = Math.round(roomVibeLevel * 100) + '%';

      const st = currentState || { status: 'Playing', position_s: 0, bpm: 120, energy: 0.5, mood: currentMood };
      const isPlaying = !currentState || currentState.status === 'Playing' || Boolean(currentState.is_playing);

      let currentPos = Number(st.position_s || 0);
      if (isPlaying) {
        const elapsed = (performance.now() - lastEventTime) / 1000;
        currentPos += elapsed;
      }

      const bpm = Number(st.bpm) || 120.0;
      const beatPeriod = 60.0 / Math.max(bpm, 1.0);
      const beatProgress = (currentPos / beatPeriod) % 1.0;

      // Music beat pulse: sharp spike on downbeat that decays smoothly
      const beatPulse = isPlaying ? Math.max(0.0, 1.0 - beatProgress * 1.6) : 0.0;

      // Sound influence: Combined metric from Room Mic + Music Beat + Song Energy
      const songEnergy = (st.energy !== null && st.energy !== undefined) ? Number(st.energy) : 0.5;
      const soundPower = Math.min(1.0,
        (micActive ? (roomVibeLevel * 1.6 + micPeakImpulse * 0.6) : (songEnergy * 0.4))
        + beatPulse * 0.45
      );

      // Update ambient glow behind canvas
      const glowScale = 1.0 + soundPower * 0.25;
      ambientGlow.style.transform = `scale(${glowScale})`;

      if (visualMode === 'mosaic' || visualMode === 'mix') {
        updateMosaicBuffer(beatPulse, soundPower);
      }

      if (visualMode === 'rings' || visualMode === 'mix') {
        renderGenerativeRingsFrame(moodCanvasCtx, moodCanvas.width, moodCanvas.height, currentPos, beatProgress, beatPulse, soundPower);
        return;
      }

      if (feelingLoaded || coverLoaded) {
        moodCanvasCtx.clearRect(0, 0, moodCanvas.width, moodCanvas.height);
        moodCanvasCtx.drawImage(mosaicBuffer, 0, 0);
      }
    }

    // Apply initial visual mode (sets canvas dimensions & active pill)
    applyVisualModeUI();

    // Start render loop
    requestAnimationFrame(renderLoop);
    // Initial load: fetch current stage snapshot immediately so first frame has track data
    fetch('/api/stage/state')
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        if (data && !currentState) {
          currentState = data;
          lastEventTime = performance.now();
          renderChord(data);
          currentMood = (data.mood || 'neutral').toLowerCase();
          currentFeeling = (data.mood_feeling || currentMood).toLowerCase();
          currentArtUrl = data.art_url || '';
          loadFeelingImage(currentFeeling);
          loadCoverImage(currentFeeling, currentArtUrl);
        }
      })
      .catch(() => {});
    loadFeelingImage(currentFeeling);
    loadCoverImage(currentFeeling, '');
  </script>
</body>
</html>"""

