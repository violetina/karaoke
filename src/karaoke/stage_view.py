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

    # 2. Look up lyrics and audio analysis from local database
    lines: list[dict[str, Any]] = []
    bpm: Optional[float] = None
    key: Optional[str] = None
    energy: Optional[float] = None
    chord_cpm: Optional[float] = None
    fifth_ratio: Optional[float] = None
    track_id: Optional[int] = None

    if artist and title:
        try:
            with localcache.connect() as conn:
                track_id = localcache.find_track_id(artist, title, conn)
                if track_id is None and url:
                    found = localcache.find_track_by_url(url, conn)
                    if found:
                        track_id = found[0]

                if track_id is not None:
                    cached_ly = localcache.get_lyrics_by_track_id(track_id, conn)
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
            # Pick next 3 upcoming tracks
            for r in rows[cur_idx + 1: cur_idx + 4]:
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
    
    mood = "neutral"
    genre = ""
    if active_line_idx >= 0 and active_line_idx < len(lines):
        try:
            from karaoke.visuals import mood_of
            mood = mood_of(lines[active_line_idx]["text"])
        except Exception:
            pass

    if mood == "neutral":
        try:
            from . import live_caption
            hist = live_caption.get_caption_history()
            if hist:
                latest = hist[-1]
                txt = latest.get("text", "")
                rms_val = latest.get("rms")
                from .sentiment import mood_of as speech_mood_of
                speech_m = speech_mood_of(txt, rms=rms_val)
                if speech_m != "neutral":
                    mood = speech_m
        except Exception:
            pass

    if track_id is not None:
        try:
            with localcache.connect() as conn:
                g = localcache.genre_for(track_id, conn)
                if g:
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
        "genre": genre,
        "chord_cpm": chord_cpm,
        "fifth_ratio": fifth_ratio,
        "active_line_index": active_line_idx,
        "next_line_in": next_line_in,
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
      width: 76vmin;
      height: 76vmin;
      max-width: 640px;
      max-height: 640px;
      image-rendering: pixelated;
      image-rendering: crisp-edges;
      border-radius: 12px;
      background: #000;
      display: block;
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
  </div>

  <div id="stage-container">
    <div id="canvas-wrap">
      <canvas id="mood-canvas" width="64" height="64"></canvas>
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
      img.crossOrigin = 'Anonymous';
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
      img.crossOrigin = 'Anonymous';
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

    // Server-Sent Events stream from Karaoke stage
    const evtSource = new EventSource('/api/stage/stream');
    evtSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        currentState = data;
        lastEventTime = performance.now();

        if (data.status !== 'Playing') {
          sentimentDiv.textContent = 'PAUSED';
          return;
        }

        const mood = (data.mood || 'neutral').toLowerCase();
        const artUrl = data.art_url || '';
        const title = data.title || '';
        const artist = data.artist || '';

        sentimentDiv.textContent = mood.toUpperCase() + (data.genre ? ' • ' + data.genre : '');
        trackInfoDiv.textContent = (title && artist) ? `${title} — ${artist}` : (title || 'Karaoke Playing');
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
          loadFeelingImage(currentMood);
          loadCoverImage(currentMood, currentArtUrl);
        }
      } catch (e) {
        console.error('SSE parse error:', e);
      }
    };

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

      if (!currentState || currentState.status !== 'Playing') return;

      let currentPos = currentState.position_s || 0;
      if (currentState.is_playing) {
        const elapsed = (performance.now() - lastEventTime) / 1000;
        currentPos += elapsed;
      }

      const bpm = currentState.bpm || 120.0;
      const beatPeriod = 60.0 / Math.max(bpm, 1.0);
      const beatProgress = (currentPos / beatPeriod) % 1.0;
      
      // Music beat pulse: sharp spike on downbeat that decays smoothly
      const beatPulse = Math.max(0.0, 1.0 - beatProgress * 1.6);

      // Sound influence: Combined metric from Room Mic + Music Beat + Song Energy
      const songEnergy = currentState.energy !== null ? currentState.energy : 0.5;
      const soundPower = Math.min(1.0, 
        (micActive ? (roomVibeLevel * 1.6 + micPeakImpulse * 0.6) : (songEnergy * 0.4)) 
        + beatPulse * 0.45
      );

      // Update ambient glow behind canvas
      const glowScale = 1.0 + soundPower * 0.25;
      ambientGlow.style.transform = `scale(${glowScale})`;

      // Get pixel data from feeling (background) and cover (foreground)
      if (!feelingLoaded && !coverLoaded) return;

      const feelingImgData = feelingLoaded 
        ? feelingCtx.getImageData(0, 0, GRID_SIZE, GRID_SIZE) 
        : feelingCtx.createImageData(GRID_SIZE, GRID_SIZE);

      const coverImgData = coverLoaded 
        ? coverCtx.getImageData(0, 0, GRID_SIZE, GRID_SIZE) 
        : coverCtx.createImageData(GRID_SIZE, GRID_SIZE);

      const outImgData = moodCanvasCtx.createImageData(GRID_SIZE, GRID_SIZE);
      const outData = outImgData.data;
      const fData = feelingImgData.data;
      const cData = coverImgData.data;

      // Center coordinates for radial shockwaves on sound bursts
      const cx = GRID_SIZE / 2;
      const cy = GRID_SIZE / 2;

      // Draw composite: Feeling in back, randomized cover pixels on top governed by sound
      for (let y = 0; y < GRID_SIZE; y++) {
        for (let x = 0; x < GRID_SIZE; x++) {
          const pixelIndex = y * GRID_SIZE + x;
          const byteIndex = pixelIndex * 4;

          // Background Feeling pixel
          const fR = fData[byteIndex];
          const fG = fData[byteIndex + 1];
          const fB = fData[byteIndex + 2];
          const fA = fData[byteIndex + 3];

          // Randomized/scrambled Cover pixel position
          // Using shuffled permutation index + sound-dependent spatial jitter
          const permIndex = pixelPermutation[pixelIndex];
          const jitterX = pixelJitter[pixelIndex * 2] * (0.3 + soundPower * 0.7);
          const jitterY = pixelJitter[pixelIndex * 2 + 1] * (0.3 + soundPower * 0.7);

          // Radial displacement on loud claps / mic transients
          const dx = x - cx;
          const dy = y - cy;
          const dist = Math.sqrt(dx * dx + dy * dy);
          const radialPush = micPeakImpulse * 5.0 * (dist / (GRID_SIZE * 0.5));
          const angle = Math.atan2(dy, dx);

          let sampleX = Math.floor((permIndex % GRID_SIZE) + jitterX + Math.cos(angle) * radialPush);
          let sampleY = Math.floor(Math.floor(permIndex / GRID_SIZE) + jitterY + Math.sin(angle) * radialPush);

          // Clamp sample coordinates within bounds
          sampleX = (sampleX % GRID_SIZE + GRID_SIZE) % GRID_SIZE;
          sampleY = (sampleY % GRID_SIZE + GRID_SIZE) % GRID_SIZE;

          const coverSampleIndex = (sampleY * GRID_SIZE + sampleX) * 4;
          const cR = cData[coverSampleIndex];
          const cG = cData[coverSampleIndex + 1];
          const cB = cData[coverSampleIndex + 2];

          // Sound-dependent coverage threshold
          // Each pixel has a unique threshold. As sound increases, more cover pixels appear.
          const threshold = pixelThresholds[pixelIndex];
          
          // Low sound: feeling shines through; High sound: randomized cover mosaic takes over
          const showCover = soundPower > (threshold * 0.85);

          // Beat brightness & color flash
          const flash = beatPulse * 0.35 + micPeakImpulse * 0.4;

          if (showCover) {
            // Cover pixel is active over the feeling
            // Blend opacity between cover and feeling based on sound power
            const coverAlpha = Math.min(1.0, 0.45 + soundPower * 0.55);
            
            const r = cR * coverAlpha + fR * (1 - coverAlpha);
            const g = cG * coverAlpha + fG * (1 - coverAlpha);
            const b = cB * coverAlpha + fB * (1 - coverAlpha);

            outData[byteIndex] = Math.min(255, r + (255 - r) * flash);
            outData[byteIndex + 1] = Math.min(255, g + (255 - g) * flash);
            outData[byteIndex + 2] = Math.min(255, b + (255 - b) * flash);
            outData[byteIndex + 3] = 255;
          } else {
            // Feeling image in the background
            outData[byteIndex] = Math.min(255, fR + (255 - fR) * (flash * 0.7));
            outData[byteIndex + 1] = Math.min(255, fG + (255 - fG) * (flash * 0.7));
            outData[byteIndex + 2] = Math.min(255, fB + (255 - fB) * (flash * 0.7));
            outData[byteIndex + 3] = fA || 255;
          }
        }
      }

      moodCanvasCtx.putImageData(outImgData, 0, 0);
    }

    // Start render loop
    requestAnimationFrame(renderLoop);
    // Initial load
    loadFeelingImage('neutral');
    loadCoverImage('neutral', '');
  </script>
</body>
</html>"""

