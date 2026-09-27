"""Real-time desktop audio transcription for live Dutch auto-captions."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import time
from typing import AsyncGenerator, Optional

import numpy as np

_MODEL = None
_MODEL_LOCK = threading.Lock()
_SUBSCRIBERS = []
_HISTORY: list[dict] = []
_MAX_HISTORY = 30
_RUNNING = False
_WORKER_THREAD: Optional[threading.Thread] = None


def get_model():
    global _MODEL
    with _MODEL_LOCK:
        if _MODEL is None:
            from faster_whisper import WhisperModel
            # Cached in ~/.cache/huggingface/hub/models--Systran--faster-whisper-small
            _MODEL = WhisperModel("small", device="cpu", compute_type="int8")
        return _MODEL


def _broadcast(item: dict):
    _HISTORY.append(item)
    if len(_HISTORY) > _MAX_HISTORY:
        _HISTORY.pop(0)
    for q in list(_SUBSCRIBERS):
        try:
            q.put_nowait(item)
        except Exception:
            pass


def _find_monitor_sink() -> str:
    try:
        res = subprocess.run(["pactl", "get-default-sink"], stdout=subprocess.PIPE, text=True, check=True)
        sink = res.stdout.strip()
        if sink:
            return f"{sink}.monitor"
    except Exception:
        pass
    return "alsa_output.pci-0000_c1_00.6.HiFi__Speaker__sink.monitor"


def _worker_loop():
    global _RUNNING
    model = get_model()
    monitor_source = _find_monitor_sink()
    sample_rate = 16000
    chunk_seconds = 3.0
    bytes_per_chunk = int(sample_rate * chunk_seconds * 2) # 16-bit mono

    cmd = [
        "parec",
        "-d", monitor_source,
        f"--rate={sample_rate}",
        "--channels=1",
        "--format=s16le",
    ]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except Exception as exc:
        _broadcast({"text": f"[Audio capture error: {exc}]", "time": time.strftime("%H:%M:%S")})
        _RUNNING = False
        return

    last_text = ""

    while _RUNNING:
        try:
            raw = proc.stdout.read(bytes_per_chunk)
            if not raw or len(raw) < bytes_per_chunk:
                time.sleep(0.1)
                continue

            audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

            # Quick energy gate: ignore near-silence
            rms = float(np.sqrt(np.mean(audio**2)))
            if rms < 0.005:
                continue

            segments, _ = model.transcribe(
                audio,
                language="nl",
                vad_filter=True,
                beam_size=3,
                temperature=0.0,
            )

            texts = [s.text.strip() for s in segments if s.text and s.text.strip()]
            if texts:
                full_text = " ".join(texts)
                # Ignore exact duplicates from consecutive chunks
                if full_text.lower() != last_text.lower():
                    last_text = full_text
                    _broadcast({
                        "text": full_text,
                        "time": time.strftime("%H:%M:%S"),
                        "rms": round(rms, 4),
                    })
        except Exception as exc:
            time.sleep(0.5)

    try:
        proc.terminate()
        proc.wait(timeout=1.0)
    except Exception:
        pass


def ensure_caption_worker():
    global _RUNNING, _WORKER_THREAD
    if not _RUNNING:
        _RUNNING = True
        _WORKER_THREAD = threading.Thread(target=_worker_loop, daemon=True, name="dutch-caption-worker")
        _WORKER_THREAD.start()


def get_caption_history() -> list[dict]:
    return list(_HISTORY)


async def caption_event_stream() -> AsyncGenerator[str, None]:
    ensure_caption_worker()
    q = asyncio.Queue()
    _SUBSCRIBERS.append(q)

    # First send initial greeting/history
    for item in list(_HISTORY)[-5:]:
        yield f"data: {json.dumps(item)}\n\n"

    try:
        while True:
            item = await q.get()
            yield f"data: {json.dumps(item)}\n\n"
    finally:
        if q in _SUBSCRIBERS:
            _SUBSCRIBERS.remove(q)


def render_captions_html() -> str:
    """Render a broadcast-style live auto-caption display page."""
    return r"""<!DOCTYPE html>
<html lang="nl">
<head>
  <meta charset="UTF-8">
  <title>Live Dutch Auto-Captions · Wielrennen</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@500;700;800;900&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #090c10;
      --card-bg: rgba(18, 22, 31, 0.85);
      --border: rgba(255, 255, 255, 0.1);
      --accent: #ffd700;
      --accent-glow: rgba(255, 215, 0, 0.35);
      --text: #ffffff;
      --font-size: 2.2rem;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg);
      color: var(--text);
      font-family: 'Outfit', -apple-system, BlinkMacSystemFont, sans-serif;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      user-select: text;
    }

    header {
      padding: 14px 24px;
      background: rgba(13, 17, 23, 0.95);
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
      backdrop-filter: blur(10px);
      z-index: 10;
    }
    .badge-live {
      display: flex;
      align-items: center;
      gap: 10px;
      font-size: 0.9rem;
      font-weight: 800;
      letter-spacing: 1.5px;
      text-transform: uppercase;
      color: #fff;
    }
    .pulse-dot {
      width: 10px;
      height: 10px;
      background: #ff0055;
      border-radius: 50%;
      box-shadow: 0 0 12px #ff0055;
      animation: pulse 1.4s infinite;
    }
    @keyframes pulse {
      0%, 100% { transform: scale(1); opacity: 1; }
      50% { transform: scale(1.3); opacity: 0.5; }
    }

    .controls {
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .btn {
      background: rgba(255, 255, 255, 0.08);
      border: 1px solid var(--border);
      color: #fff;
      padding: 6px 14px;
      border-radius: 8px;
      font-size: 0.85rem;
      font-weight: 700;
      cursor: pointer;
      transition: all 0.15s ease;
      font-family: inherit;
    }
    .btn:hover {
      background: rgba(255, 255, 255, 0.18);
      border-color: rgba(255, 255, 255, 0.3);
    }

    #captions-scroll {
      flex: 1;
      overflow-y: auto;
      padding: 30px 40px 100px 40px;
      display: flex;
      flex-direction: column;
      gap: 18px;
      scroll-behavior: smooth;
    }

    .caption-card {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-left: 6px solid var(--accent);
      padding: 18px 24px;
      border-radius: 12px;
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.6);
      animation: slideIn 0.25s ease-out;
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    @keyframes slideIn {
      from { opacity: 0; transform: translateY(12px); }
      to { opacity: 1; transform: translateY(0); }
    }

    .caption-meta {
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.8rem;
      color: rgba(255, 255, 255, 0.4);
      letter-spacing: 1px;
    }

    .caption-text {
      font-size: var(--font-size);
      font-weight: 800;
      line-height: 1.35;
      color: var(--accent);
      text-shadow: 0 2px 8px rgba(0, 0, 0, 0.8);
      letter-spacing: 0.3px;
    }

    .latest-card {
      border-color: rgba(255, 215, 0, 0.4);
      box-shadow: 0 12px 40px rgba(0, 0, 0, 0.8), 0 0 25px var(--accent-glow);
    }
    .latest-card .caption-text {
      color: var(--accent);
    }

    #empty-state {
      margin: auto;
      text-align: center;
      color: rgba(255, 255, 255, 0.35);
      font-size: 1.2rem;
      font-weight: 600;
    }
  </style>
</head>
<body>
  <header>
    <div class="badge-live">
      <div class="pulse-dot"></div>
      <span>Live Dutch Auto-Captions · Wielrennen</span>
    </div>
    <div class="controls">
      <button class="btn" onclick="toggleColor()">Teletext Yellow / White</button>
      <button class="btn" onclick="adjustSize(-0.2)">A-</button>
      <button class="btn" onclick="adjustSize(0.2)">A+</button>
      <button class="btn" onclick="clearCaptions()">Clear</button>
    </div>
  </header>

  <div id="captions-scroll">
    <div id="empty-state">Luisteren naar het commentaar... (Listening to live commentary)</div>
  </div>

  <script>
    const scrollContainer = document.getElementById('captions-scroll');
    const emptyState = document.getElementById('empty-state');
    let autoScroll = true;
    let isYellow = true;
    let currentFontSize = 2.2;

    function adjustSize(delta) {
      currentFontSize = Math.max(1.2, Math.min(3.8, currentFontSize + delta));
      document.documentElement.style.setProperty('--font-size', currentFontSize + 'rem');
    }

    function toggleColor() {
      isYellow = !isYellow;
      const color = isYellow ? '#ffd700' : '#ffffff';
      const glow = isYellow ? 'rgba(255, 215, 0, 0.35)' : 'rgba(255, 255, 255, 0.25)';
      document.documentElement.style.setProperty('--accent', color);
      document.documentElement.style.setProperty('--accent-glow', glow);
    }

    function clearCaptions() {
      scrollContainer.innerHTML = '';
      scrollContainer.appendChild(emptyState);
    }

    function addCaption(item) {
      if (emptyState.parentNode) {
        emptyState.remove();
      }

      // Remove highlight from previous
      const prevLatest = document.querySelector('.latest-card');
      if (prevLatest) prevLatest.classList.remove('latest-card');

      const card = document.createElement('div');
      card.className = 'caption-card latest-card';

      const meta = document.createElement('div');
      meta.className = 'caption-meta';
      meta.textContent = item.time || new Date().toLocaleTimeString();

      const text = document.createElement('div');
      text.className = 'caption-text';
      text.textContent = item.text;

      card.appendChild(meta);
      card.appendChild(text);
      scrollContainer.appendChild(card);

      // Keep max 40 items in DOM for performance
      while (scrollContainer.children.length > 40) {
        scrollContainer.removeChild(scrollContainer.firstChild);
      }

      if (autoScroll) {
        scrollContainer.scrollTop = scrollContainer.scrollHeight;
      }
    }

    // Connect SSE stream
    function connectSSE() {
      const evt = new EventSource('/api/captions/stream');
      evt.onmessage = (e) => {
        try {
          const data = JSON.parse(e.data);
          if (data && data.text) {
            addCaption(data);
          }
        } catch (err) {
          console.error('SSE parse error:', err);
        }
      };
      evt.onerror = () => {
        setTimeout(connectSSE, 2000);
      };
    }

    connectSSE();
  </script>
</body>
</html>"""
