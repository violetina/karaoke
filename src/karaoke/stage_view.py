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
  <div id="mood-hud" style="position: absolute; top: 1.5rem; right: 1.5rem; display: flex; flex-direction: column; align-items: flex-end; gap: 0.5rem; z-index: 50;">
    <canvas id="track-mood-art" width="64" height="64" style="width: 100px; height: 100px; border-radius: 8px; box-shadow: 0 4px 15px rgba(0,0,0,0.5); image-rendering: pixelated;"></canvas>
    <div id="mood-sentiment" style="font-size: 0.85rem; font-weight: bold; text-transform: uppercase; letter-spacing: 2px; color: #00f2fe; text-shadow: 0 2px 4px rgba(0,0,0,0.8);"></div>
  </div>
  
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
    <canvas id="hud-mood-art" width="64" height="64" style="image-rendering: pixelated; width: 120px; height: 120px; border-radius: 8px; box-shadow: 0 4px 15px rgba(0,0,0,0.5);"></canvas>
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
      const hudMood = document.getElementById('hud-mood-art');
      
      if (data.art_url) {
         let newSrc = '/api/art?url=' + encodeURIComponent(data.art_url);
         if (!hudArt.src.endsWith(newSrc)) {
             hudArt.src = newSrc;
         }
         hudArt.style.display = 'block';
      } else {
         hudArt.style.display = 'none';
      }
      
      if (data.title) {
         hudMood.style.display = 'block';
         let seed = data.title || "unknown";
         let newSrc = `/api/mood-art?mood=${mood}&seed=${encodeURIComponent(seed)}&energy=${data.energy !== null ? data.energy : 0.5}&bpm=${data.bpm || 120.0}`;
         if (window.currentMoodUrl !== newSrc) {
             window.currentMoodUrl = newSrc;
             const img = new Image();
             img.crossOrigin = "Anonymous";
             img.onload = () => {
                 const offCtx = document.createElement('canvas').getContext('2d');
                 offCtx.canvas.width = 64;
                 offCtx.canvas.height = 64;
                 offCtx.drawImage(img, 0, 0, 64, 64);
                 window.moodBaseImageData = offCtx.getImageData(0, 0, 64, 64);
                 hudMood.width = 64;
                 hudMood.height = 64;
                 window.moodCanvasCtx = hudMood.getContext('2d');
             };
             img.src = newSrc;
         }
      } else {
         hudMood.style.display = 'none';
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
