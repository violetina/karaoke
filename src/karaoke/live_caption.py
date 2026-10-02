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
    """Render a /tv style centered live auto-caption display page."""
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
      --bg: #07090e;
      --card-bg: rgba(18, 22, 34, 0.85);
      --border: rgba(255, 255, 255, 0.08);
      --accent: #ffd700;
      --accent-glow: rgba(255, 215, 0, 0.4);
      --text: #ffffff;
      --text-muted: rgba(255, 255, 255, 0.55);
      --text-dim: rgba(255, 255, 255, 0.25);
      --font-size: 3.4rem;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg);
      color: var(--text);
      font-family: 'Outfit', -apple-system, BlinkMacSystemFont, sans-serif;
      height: 100vh;
      display: flex;
      flex-direction: column;
      overflow: hidden;
      user-select: none;
    }

    header {
      padding: 16px 28px;
      background: rgba(13, 17, 27, 0.9);
      border-bottom: 1px solid var(--border);
      display: flex;
      align-items: center;
      justify-content: space-between;
      backdrop-filter: blur(14px);
      z-index: 10;
    }
    .badge-live {
      display: flex;
      align-items: center;
      gap: 10px;
      font-size: 0.95rem;
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
      box-shadow: 0 0 14px #ff0055;
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
      background: rgba(255, 255, 255, 0.06);
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
      background: rgba(255, 255, 255, 0.16);
      border-color: rgba(255, 255, 255, 0.25);
    }

    /* /tv style centered stage */
    main {
      flex: 1;
      display: flex;
      flex-direction: column;
      justify-content: center;
      align-items: center;
      padding: 2rem 4rem;
      position: relative;
      overflow: hidden;
      text-align: center;
    }

    #captions-container {
      width: 100%;
      max-width: 1400px;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 1.6rem;
      text-align: center;
    }

    .caption-line {
      line-height: 1.3;
      max-width: 90%;
      transition: all 0.35s cubic-bezier(0.2, 0, 0, 1);
      word-break: break-word;
    }

    .caption-line.prev-2 {
      font-size: calc(var(--font-size) * 0.52);
      color: var(--text-dim);
      opacity: 0.35;
      transform: translateY(8px);
    }

    .caption-line.prev-1 {
      font-size: calc(var(--font-size) * 0.72);
      color: var(--text-muted);
      opacity: 0.7;
      transform: translateY(4px);
      font-weight: 600;
    }

    .caption-line.active {
      font-size: var(--font-size);
      font-weight: 900;
      color: var(--accent);
      text-shadow: 0 0 35px var(--accent-glow), 0 0 15px var(--accent);
      opacity: 1;
      transform: scale(1.02);
      letter-spacing: 0.4px;
    }

    .active-anim {
      animation: popIn 0.28s cubic-bezier(0.18, 0.89, 0.32, 1.28);
    }
    @keyframes popIn {
      from { opacity: 0; transform: scale(0.96) translateY(12px); }
      to { opacity: 1; transform: scale(1.02) translateY(0); }
    }

    #time-pill {
      font-family: 'JetBrains Mono', monospace;
      font-size: 0.95rem;
      color: var(--accent);
      background: rgba(255, 215, 0, 0.08);
      border: 1px solid rgba(255, 215, 0, 0.2);
      padding: 0.4rem 1.2rem;
      border-radius: 999px;
      margin-top: 0.5rem;
      letter-spacing: 1.5px;
      text-transform: uppercase;
      transition: all 0.2s ease;
    }

    footer {
      padding: 14px 28px;
      background: rgba(13, 17, 27, 0.85);
      border-top: 1px solid var(--border);
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 0.85rem;
      color: var(--text-muted);
      font-family: 'JetBrains Mono', monospace;
    }
  </style>
</head>
<body>
  <header>
    <div class="badge-live">
      <div class="pulse-dot"></div>
      <span>Live Dutch Auto-Captions · Koers TV</span>
    </div>
    <div class="controls">
      <button class="btn" onclick="toggleColor()">Yellow / White / Cyan</button>
      <button class="btn" onclick="adjustSize(-0.25)">A-</button>
      <button class="btn" onclick="adjustSize(0.25)">A+</button>
      <button class="btn" onclick="toggleFullscreen()">[F] Fullscreen</button>
    </div>
  </header>

  <main>
    <div id="captions-container">
      <div class="caption-line prev-2" id="line-prev-2"></div>
      <div class="caption-line prev-1" id="line-prev-1"></div>
      <div class="caption-line active active-anim" id="line-active">Luisteren naar het commentaar…</div>
      <div id="time-pill">LIVE KOERS</div>
    </div>
  </main>

  <footer>
    <span>PipeWire Monitor: HiFi Speaker</span>
    <span>Druk op [F] voor Fullscreen</span>
  </footer>

  <script>
    const linePrev2 = document.getElementById('line-prev-2');
    const linePrev1 = document.getElementById('line-prev-1');
    const lineActive = document.getElementById('line-active');
    const timePill = document.getElementById('time-pill');

    let historyTexts = [];
    let currentFontSize = 3.4;
    let colorMode = 0; // 0: Yellow, 1: White, 2: Cyan

    function adjustSize(delta) {
      currentFontSize = Math.max(1.8, Math.min(5.0, currentFontSize + delta));
      document.documentElement.style.setProperty('--font-size', currentFontSize + 'rem');
    }

    function toggleColor() {
      colorMode = (colorMode + 1) % 3;
      if (colorMode === 0) {
        // Teletext Yellow
        document.documentElement.style.setProperty('--accent', '#ffd700');
        document.documentElement.style.setProperty('--accent-glow', 'rgba(255, 215, 0, 0.4)');
      } else if (colorMode === 1) {
        // Crisp White
        document.documentElement.style.setProperty('--accent', '#ffffff');
        document.documentElement.style.setProperty('--accent-glow', 'rgba(255, 255, 255, 0.3)');
      } else {
        // Electric Cyan
        document.documentElement.style.setProperty('--accent', '#00f2fe');
        document.documentElement.style.setProperty('--accent-glow', 'rgba(0, 242, 254, 0.4)');
      }
    }

    function toggleFullscreen() {
      if (!document.fullscreenElement) {
        document.documentElement.requestFullscreen().catch(() => {});
      } else {
        document.exitFullscreen().catch(() => {});
      }
    }

    window.addEventListener('keydown', (e) => {
      if (e.key === 'f' || e.key === 'F') {
        toggleFullscreen();
      }
    });

    function setCaption(item) {
      if (!item || !item.text) return;

      const newText = item.text.trim();
      if (!newText) return;

      // Don't duplicate if identical to current active
      if (historyTexts.length > 0 && historyTexts[historyTexts.length - 1] === newText) {
        return;
      }

      historyTexts.push(newText);
      if (historyTexts.length > 30) {
        historyTexts.shift();
      }

      const len = historyTexts.length;
      linePrev2.textContent = len >= 3 ? historyTexts[len - 3] : '';
      linePrev1.textContent = len >= 2 ? historyTexts[len - 2] : '';
      
      lineActive.textContent = newText;
      lineActive.classList.remove('active-anim');
      void lineActive.offsetWidth; // retrigger animation
      lineActive.classList.add('active-anim');

      if (item.time) {
        timePill.textContent = `${item.time} · WIELRENNEN`;
      }
    }

    // Connect SSE stream
    function connectSSE() {
      const evt = new EventSource('/api/captions/stream');
      evt.onmessage = (e) => {
        try {
          const data = JSON.parse(e.data);
          if (data && data.text) {
            setCaption(data);
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
