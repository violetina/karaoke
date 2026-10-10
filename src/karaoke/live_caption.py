"""Real-time desktop audio transcription for live Dutch auto-captions."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import subprocess
import threading
import time
import urllib.request
import wave
from typing import AsyncGenerator, Optional

import numpy as np

_CONFIG = {
    "model": os.environ.get("KARAOKE_CAPTION_MODEL", "faster-whisper-small"),
    "language": os.environ.get("KARAOKE_CAPTION_LANG", "nl"),
    "api_key": os.environ.get("GEMINI_API_KEY") or os.environ.get("VIOLETINA_API_KEY") or "",
    "source": os.environ.get("KARAOKE_CAPTION_SOURCE", "mic"),
}

_WHISPER_MODELS = {}
_MODEL_LOCK = threading.Lock()
_SUBSCRIBERS = []
_HISTORY: list[dict] = []
_MAX_HISTORY = 30
_RUNNING = False
_WORKER_THREAD: Optional[threading.Thread] = None


def get_caption_config() -> dict[str, str]:
    return dict(_CONFIG)


def set_caption_config(model: Optional[str] = None, language: Optional[str] = None, api_key: Optional[str] = None, source: Optional[str] = None) -> dict[str, str]:
    if model:
        _CONFIG["model"] = model.strip()
    if language:
        _CONFIG["language"] = language.strip()
    if api_key is not None:
        _CONFIG["api_key"] = api_key.strip()
    if source:
        _CONFIG["source"] = source.strip()
    return dict(_CONFIG)


def get_whisper_model(model_name: str = "faster-whisper-small"):
    size_name = model_name.replace("faster-whisper-", "").strip() or "small"
    with _MODEL_LOCK:
        if size_name not in _WHISPER_MODELS:
            from faster_whisper import WhisperModel
            # Cached in ~/.cache/huggingface/hub
            _WHISPER_MODELS[size_name] = WhisperModel(size_name, device="cpu", compute_type="int8")
        return _WHISPER_MODELS[size_name]


def get_model():
    return get_whisper_model("small")


def pcm_to_wav(pcm_bytes: bytes, sample_rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return buf.getvalue()


def transcribe_gemini(pcm_bytes: bytes, api_key: str, language: str = "nl", model_name: str = "gemini-2.5-flash") -> str:
    wav_bytes = pcm_to_wav(pcm_bytes)
    b64_audio = base64.b64encode(wav_bytes).decode("ascii")

    lang_map = {"nl": "Dutch", "en": "English", "fr": "French", "de": "German", "es": "Spanish"}
    lang_name = lang_map.get(language, "auto")
    if lang_name != "auto":
        prompt = f"Transcribe the spoken audio in {lang_name} verbatim. Return ONLY the exact transcribed text, nothing else. If silent or background noise only, return an empty string."
    else:
        prompt = "Transcribe the spoken audio verbatim. Return ONLY the transcribed text, nothing else. If silent, return an empty string."

    clean_model = model_name if "gemini" in model_name else "gemini-2.5-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{clean_model}:generateContent?key={api_key}"
    payload = {
        "contents": [{
            "parts": [
                {"inline_data": {"mime_type": "audio/wav", "data": b64_audio}},
                {"text": prompt}
            ]
        }],
        "generationConfig": {
            "temperature": 0.0
        }
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        res_data = json.loads(resp.read().decode("utf-8"))
        candidates = res_data.get("candidates", [])
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            return " ".join([p.get("text", "").strip() for p in parts if p.get("text")]).strip()
    return ""


def transcribe_gemma(pcm_bytes: bytes, api_key: str, language: str = "nl", model_name: str = "gemma-2-9b-it") -> str:
    """Use Gemma model via API for speech transcription / Dutch text refinement."""
    raw_text = ""
    if api_key:
        try:
            raw_text = transcribe_gemini(pcm_bytes, api_key=api_key, language=language, model_name="gemini-2.5-flash")
        except Exception:
            pass

    if not raw_text:
        try:
            audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            whisper_model = get_whisper_model("faster-whisper-small")
            trans_lang = None if language == "auto" else language
            segments, _ = whisper_model.transcribe(audio, language=trans_lang, vad_filter=True, beam_size=3, temperature=0.0)
            raw_text = " ".join([s.text.strip() for s in segments if s.text and s.text.strip()])
        except Exception:
            pass

    if not raw_text or not api_key:
        return raw_text

    clean_model = model_name if "gemma" in model_name else "gemma-2-9b-it"
    lang_map = {"nl": "Dutch", "en": "English", "fr": "French", "de": "German", "es": "Spanish"}
    lang_name = lang_map.get(language, "Dutch")
    prompt = f"You are a fluent {lang_name} speech assistant. Clean up, correct spelling, and fix grammar for this transcribed {lang_name} speech: '{raw_text}'. Return ONLY the clean corrected {lang_name} text, nothing else."

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{clean_model}:generateContent?key={api_key}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.0}
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            res_data = json.loads(resp.read().decode("utf-8"))
            candidates = res_data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                refined = " ".join([p.get("text", "").strip() for p in parts if p.get("text")]).strip()
                if refined:
                    return refined
    except Exception:
        pass
    return raw_text


def _broadcast(item: dict):
    _HISTORY.append(item)
    if len(_HISTORY) > _MAX_HISTORY:
        _HISTORY.pop(0)
    for q in list(_SUBSCRIBERS):
        try:
            q.put_nowait(item)
        except Exception:
            pass


_WPM_HISTORY: list[tuple[float, int]] = []


def detect_live_bpm(audio_pcm: np.ndarray, sample_rate: int = 16000) -> float:
    """Estimate live audio beat / tempo (BPM) from audio frame energy onset autocorrelation."""
    hop = 512
    frames = [float(np.mean(audio_pcm[i:i + hop] ** 2)) for i in range(0, len(audio_pcm) - hop, hop)]
    if not frames:
        return 120.0
    env = np.array(frames)
    diff = np.maximum(0, np.diff(env))
    if len(diff) < 20 or float(np.max(diff)) == 0.0:
        return 120.0
    fps = sample_rate / hop
    min_lag = int(fps * 60 / 180)  # 180 BPM
    max_lag = int(fps * 60 / 60)   # 60 BPM
    autocorr = np.correlate(diff, diff, mode="full")
    autocorr = autocorr[len(diff) - 1:]
    if max_lag < len(autocorr):
        lags = autocorr[min_lag:max_lag]
        if len(lags) > 0 and float(np.max(lags)) > 0:
            best_lag = min_lag + int(np.argmax(lags))
            bpm = (fps * 60.0) / best_lag
            return round(float(bpm), 1)
    return 120.0


def compute_rolling_wpm(new_words: int, window_seconds: float = 20.0) -> float:
    """Calculate speech cadence in Words Per Minute (WPM) over a rolling time window."""
    now = time.monotonic()
    _WPM_HISTORY.append((now, new_words))
    cutoff = now - window_seconds
    while _WPM_HISTORY and _WPM_HISTORY[0][0] < cutoff:
        _WPM_HISTORY.pop(0)
    if len(_WPM_HISTORY) <= 1:
        return round(float(new_words * (60.0 / 3.0)), 1)
    span = _WPM_HISTORY[-1][0] - _WPM_HISTORY[0][0]
    total_words = sum(w for _, w in _WPM_HISTORY)
    if span <= 0:
        return 0.0
    return round(float((total_words / span) * 60.0), 1)


def _find_audio_source() -> str:
    source_cfg = _CONFIG.get("source", os.environ.get("KARAOKE_CAPTION_SOURCE", "mic")).strip().lower()

    if source_cfg in ("mic", "microphone", "input", "default_source", "source"):
        try:
            res = subprocess.run(["pactl", "get-default-source"], stdout=subprocess.PIPE, text=True, check=True)
            src = res.stdout.strip()
            if src:
                return src
        except Exception:
            pass
        return "alsa_input.pci-0000_c1_00.6.HiFi__Mic2__source"

    if source_cfg in ("speaker", "monitor", "sink", "output", "desktop"):
        try:
            res = subprocess.run(["pactl", "get-default-sink"], stdout=subprocess.PIPE, text=True, check=True)
            sink = res.stdout.strip()
            if sink:
                return f"{sink}.monitor"
        except Exception:
            pass
        return "alsa_output.pci-0000_c1_00.6.HiFi__Speaker__sink.monitor"

    raw_val = _CONFIG.get("source", "").strip()
    if raw_val:
        return raw_val

    try:
        res = subprocess.run(["pactl", "get-default-source"], stdout=subprocess.PIPE, text=True, check=True)
        src = res.stdout.strip()
        if src:
            return src
    except Exception:
        pass
    return "alsa_input.pci-0000_c1_00.6.HiFi__Mic2__source"


def _find_monitor_sink() -> str:
    return _find_audio_source()


def _worker_loop():
    global _RUNNING
    audio_source = _find_audio_source()
    sample_rate = 16000
    chunk_seconds = 3.0
    bytes_per_chunk = int(sample_rate * chunk_seconds * 2) # 16-bit mono

    cmd = [
        "parec",
        "-d", audio_source,
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
            current_src = _find_audio_source()
            if current_src != audio_source:
                audio_source = current_src
                try:
                    proc.terminate()
                    proc.wait(timeout=1.0)
                except Exception:
                    pass
                cmd = [
                    "parec",
                    "-d", audio_source,
                    f"--rate={sample_rate}",
                    "--channels=1",
                    "--format=s16le",
                ]
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                continue

            raw = proc.stdout.read(bytes_per_chunk)
            if not raw or len(raw) < bytes_per_chunk:
                time.sleep(0.1)
                continue

            audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

            # Quick energy gate: ignore near-silence
            rms = float(np.sqrt(np.mean(audio**2)))
            if rms < 0.005:
                continue

            model_name = _CONFIG.get("model", "faster-whisper-small")
            lang = _CONFIG.get("language", "nl")
            api_key = _CONFIG.get("api_key", "").strip()

            full_text = ""

            if model_name.startswith("gemma"):
                if not api_key:
                    _broadcast({"text": "[Gemma API key required - enter key or set GEMINI_API_KEY]", "time": time.strftime("%H:%M:%S")})
                    time.sleep(2.0)
                    continue
                try:
                    full_text = transcribe_gemma(raw, api_key=api_key, language=lang, model_name=model_name)
                except Exception as exc:
                    _broadcast({"text": f"[Gemma error: {exc}]", "time": time.strftime("%H:%M:%S")})
                    time.sleep(1.0)
                    continue
            elif model_name.startswith("gemini"):
                if not api_key:
                    _broadcast({"text": "[Gemini API key required - enter key or set GEMINI_API_KEY]", "time": time.strftime("%H:%M:%S")})
                    time.sleep(2.0)
                    continue
                try:
                    full_text = transcribe_gemini(raw, api_key=api_key, language=lang, model_name=model_name)
                except Exception as exc:
                    _broadcast({"text": f"[Gemini API error: {exc}]", "time": time.strftime("%H:%M:%S")})
                    time.sleep(1.0)
                    continue
            else:
                try:
                    whisper_model = get_whisper_model(model_name)
                    trans_lang = None if lang == "auto" else lang
                    segments, _ = whisper_model.transcribe(
                        audio,
                        language=trans_lang,
                        vad_filter=True,
                        beam_size=3,
                        temperature=0.0,
                    )
                    texts = [s.text.strip() for s in segments if s.text and s.text.strip()]
                    if texts:
                        full_text = " ".join(texts)
                except Exception as exc:
                    _broadcast({"text": f"[Whisper error: {exc}]", "time": time.strftime("%H:%M:%S")})
                    time.sleep(1.0)
            live_bpm = detect_live_bpm(audio)

            if full_text and full_text.lower() != last_text.lower():
                last_text = full_text
                w_count = len(full_text.split())
                live_wpm = compute_rolling_wpm(w_count)
                _broadcast({
                    "text": full_text,
                    "time": time.strftime("%H:%M:%S"),
                    "rms": round(rms, 4),
                    "bpm": live_bpm,
                    "wpm": live_wpm,
                    "model": model_name,
                    "lang": lang,
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
    .btn, .select-btn, .input-key {
      background: rgba(255, 255, 255, 0.08);
      border: 1px solid var(--border);
      color: #fff;
      padding: 6px 12px;
      border-radius: 8px;
      font-size: 0.85rem;
      font-weight: 700;
      font-family: inherit;
      outline: none;
      transition: all 0.15s ease;
      cursor: pointer;
    }
    .btn:hover, .select-btn:hover, .input-key:focus {
      background: rgba(255, 255, 255, 0.18);
      border-color: rgba(255, 255, 255, 0.3);
    }
    .select-btn option {
      background: #0d111b;
      color: #fff;
    }
    .input-key {
      width: 160px;
      cursor: text;
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
      <span>Live Auto-Captions</span>
    </div>
    <div class="controls">
      <button class="btn" id="source-toggle-btn" onclick="toggleSource()">🎙️ Mic ON</button>
      <select class="select-btn" id="lang-select" onchange="updateConfig()">
        <option value="nl">🇳🇱 Dutch (nl)</option>
        <option value="en">🇬🇧 English (en)</option>
        <option value="fr">🇫🇷 French (fr)</option>
        <option value="de">🇩🇪 German (de)</option>
        <option value="es">🇪🇸 Spanish (es)</option>
        <option value="auto">🌐 Auto Detect</option>
      </select>
      <select class="select-btn" id="model-select" onchange="onModelChange()">
        <option value="faster-whisper-small">⚡ Whisper Small (Local)</option>
        <option value="faster-whisper-tiny">🚀 Whisper Tiny (Fast Local)</option>
        <option value="faster-whisper-base">🎯 Whisper Base (Local)</option>
        <option value="gemini-2.5-flash">✨ Gemini 2.5 Flash (API)</option>
        <option value="gemini-1.5-flash">✨ Gemini 1.5 Flash (API)</option>
        <option value="gemma-2-9b-it">💎 Gemma 2 9B (Dutch Refiner API)</option>
        <option value="gemma-3-27b-it">💎 Gemma 3 27B (Dutch Refiner API)</option>
      </select>
      <input type="password" id="api-key-input" placeholder="API Key (violetina...)" class="input-key" style="display:none;" onchange="updateConfig()" />
      <button class="btn" onclick="toggleColor()">Color</button>
      <button class="btn" onclick="adjustSize(-0.25)">A-</button>
      <button class="btn" onclick="adjustSize(0.25)">A+</button>
      <button class="btn" onclick="toggleFullscreen()">[F] Fullscreen</button>
    </div>
  </header>

  <main>
    <div id="captions-container">
      <div class="caption-line prev-2" id="line-prev-2"></div>
      <div class="caption-line prev-1" id="line-prev-1"></div>
      <div class="caption-line active active-anim" id="line-active">Luisteren naar audio…</div>
      <div id="time-pill">LIVE CAPTIONS</div>
    </div>
  </main>

  <footer>
    <span id="footer-source-text">PipeWire Input: Microphone / Default Source</span>
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
    let currentSource = 'mic';

    function updateSourceUI() {
      const btn = document.getElementById('source-toggle-btn');
      const footerSpan = document.getElementById('footer-source-text');
      if (currentSource === 'speaker' || currentSource === 'monitor' || currentSource === 'desktop') {
        btn.textContent = '🔊 Speaker (What\'s Playing)';
        if (footerSpan) footerSpan.textContent = 'PipeWire Output: Speaker Monitor (What\'s Playing)';
      } else {
        btn.textContent = '🎙️ Mic ON';
        if (footerSpan) footerSpan.textContent = 'PipeWire Input: Microphone / Default Source';
      }
    }

    function toggleSource() {
      currentSource = (currentSource === 'mic' || currentSource === 'microphone') ? 'speaker' : 'mic';
      updateSourceUI();
      updateConfig();
    }

    async function loadConfig() {
      try {
        const res = await fetch('/api/captions/config');
        if (res.ok) {
          const cfg = await res.json();
          if (cfg.model) document.getElementById('model-select').value = cfg.model;
          if (cfg.language) document.getElementById('lang-select').value = cfg.language;
          if (cfg.api_key) document.getElementById('api-key-input').value = cfg.api_key;
          if (cfg.source) {
            currentSource = cfg.source;
            updateSourceUI();
          }
          onModelChange(false);
        }
      } catch (err) {
        console.warn('Failed to load captions config:', err);
      }
    }

    function onModelChange(triggerSave = true) {
      const model = document.getElementById('model-select').value;
      const keyInput = document.getElementById('api-key-input');
      if (model.startsWith('gemini') || model.startsWith('gemma')) {
        keyInput.style.display = 'inline-block';
      } else {
        keyInput.style.display = 'none';
      }
      if (triggerSave) {
        updateConfig();
      }
    }

    async function updateConfig() {
      const model = document.getElementById('model-select').value;
      const language = document.getElementById('lang-select').value;
      const apiKey = document.getElementById('api-key-input').value;
      try {
        await fetch('/api/captions/config', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ model, language, api_key: apiKey, source: currentSource }),
        });
      } catch (err) {
        console.warn('Failed to save config:', err);
      }
    }

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
        const info = item.lang ? `${item.time} · ${item.lang.toUpperCase()}` : `${item.time}`;
        timePill.textContent = info;
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

    loadConfig();
    connectSSE();
  </script>
</body>
</html>"""
