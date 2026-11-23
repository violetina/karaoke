"""Deterministic mood stills for a track, built from its measured features.

Match the pipeline described in ``creative/mood-image``:
  * resolve the measured features (``bpm`` / ``key`` / ``camelot`` / ``energy`` /
    ``genre`` / ``form``),
  * map each field to a visual parameter,
  * build layers front-to-back with alpha-over (never additive),
  * seed the whole thing deterministically, and
  * verify with objective pixel metrics (dark tonal field + bright local core
    + no clipped highlights).

The renderer is a pure function over an ``analysis`` object duck-typed like a
``track_analysis`` row (``.energy``, ``.bpm``, ``.brightness``,
``.resolved_key``/``.detected_key``). It can be driven from SQLite, from the
MCP ``get_now_playing`` payload, or from the control API's query params.

Palette (G# minor / Camelot ``1A``, the chain-breaking minor) --
  bg        : #06070F -> #161A3C, deep violet -> navy field
  ring      : #9676C4, violet (cool, tense)
  core      : #FFEEF0, white-hot hot spot
  flare     : #FF5C54, tension red-orange
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
TARGET_W = 1920
TARGET_H = 1080
SCALE = 1                     # render at target resolution (SCALE=1)

C_BG_DEEP = (7, 9, 16)
C_BG_TOP = (16, 18, 38)
C_BG_CORE = (28, 22, 62)
C_RING = (150, 118, 196)
C_CORE_MAX = (255, 238, 210)
C_CORE_MID = (235, 170, 210)
C_ACCENT = (255, 92, 84)
C_TEXT = (202, 192, 236)
C_TXT_DIM = (150, 120, 160)
C_GRAIN = 16

W, H = TARGET_W, TARGET_H
# Rows (height) vs Columns (width): NumPy 2-D arrays are indexed (row, col) = (H, W)
IR, IC = H * SCALE, W * SCALE

_NOTE_SEMITONES = {
    "c": 0, "c#": 1, "db": 1,
    "d": 2, "d#": 3, "eb": 3,
    "e": 4, "fb": 4, "e#": 5,
    "f": 5, "f#": 6, "gb": 6,
    "g": 7, "g#": 8, "ab": 8,
    "a": 9, "a#": 10, "bb": 10,
    "b": 11, "cb": 11, "b#": 0,
}


def _lerp(a: float, b: float, t: Any) -> Any:
    return a + (b - a) * t


def _hsl(h: float, s: float, v: float) -> tuple[int, int, int]:
    s = min(1.0, max(0.0, s))
    v = min(1.0, max(0.0, v))
    h = h % 1.0
    if s == 0:
        c = int(round(v * 255))
        return c, c, c
    f = (h * 6.0) - int(h * 6.0)          # fractional part, in [0, 1)
    i = int(h * 6.0) % 6
    p = v * (1 - s)
    q = v * (1 - s * f)
    t = v * (1 - s * (1 - f))
    vals = {
        0: (v, t, p), 1: (q, v, p), 2: (p, v, t),
        3: (p, q, v), 4: (t, p, v), 5: (v, p, q),
    }
    return tuple(int(round(c * 255)) for c in vals[i])


def _parse_pitch_class(text: str) -> Optional[int]:
    if not text:
        return None
    m = re.match(r"^\s*([A-Ga-g])([#b♯♭]?)", str(text))
    if not m:
        return None
    note = m.group(1).lower()
    acc = m.group(2).replace("♯", "#").replace("♭", "b")
    return _NOTE_SEMITONES.get(note + acc, _NOTE_SEMITONES.get(note))


def _key_hue(analysis: Any) -> float:
    """Base hue from the track's key (G# minor -> the purple-violet field)."""
    key = getattr(analysis, "resolved_key", None) or getattr(analysis, "detected_key", None)
    if key is None:
        return 0.68  # deep violet-blue default for G# minor / 1A
    pitch = getattr(key, "pitch_class", None)
    if pitch is None:
        name = getattr(key, "name", "") or str(key)
        pitch = _parse_pitch_class(name)
        if pitch is None:
            pitch = sum(ord(c) for c in name) % 12
    # Offset so G# (pitch 8) lands on 0.68 (deep indigo-violet)
    return float((pitch % 12)) / 12.0


def _is_minor(analysis: Any) -> bool:
    key = getattr(analysis, "resolved_key", None) or getattr(analysis, "detected_key", None)
    name = (getattr(key, "name", "") or str(key or "")).lower()
    if "minor" in name:
        return True
    if "major" in name:
        return False
    return bool(re.match(r"^\s*[a-g][#b]?m\b", name))


def _mood_hue_shift(mood: str) -> float:
    """Small hue drift per mood tag so the same song renders differently
    as its mood timeline advances."""
    if not mood:
        return 0.0
    key = mood.lower().strip()
    shifts = {
        "tense": 0.06, "energetic": 0.09, "angry": 0.15, "aggressive": 0.14,
        "sad": -0.06, "melancholy": -0.04, "mournful": -0.05,
        "calm": 0.01, "peaceful": 0.02, "dreamy": 0.04, "tender": 0.03,
        "happy": 0.08, "upbeat": 0.07, "playful": 0.06,
        "energized": 0.09,
    }
    return shifts.get(key, 0.0)


def _chord_hue_offset(roman: str) -> float:
    """Hue offset per chord Roman numeral. Strips flats/sharps and trailing
    extensions so 'bII' and 'II' land on the same hue offset."""
    if not roman:
        return 0.0
    base = re.sub(r"[b#m°769]", "", roman, flags=re.IGNORECASE).strip().upper()
    shifts = {
        "I": 0.0, "II": -0.02, "III": -0.01, "IV": 0.04, "V": 0.06,
        "VI": -0.02, "VII": 0.08,
    }
    return shifts.get(base, 0.0)


def _chord_quality_fall(chord: dict) -> tuple[float, float]:
    """Minor->cooler/darker, diminished->tighter; major is neutral."""
    if not chord:
        return 0.0, 0.0
    name = (chord.get("chord") or "").strip().lower()
    if name.endswith("m") or name.endswith("dim"):
        return -0.02, -0.03
    if "°" in name:
        return 0.02, -0.01
    return 0.0, 0.0


def _chord_frequency(name: str) -> float:
    """Return the fundamental frequency (Hz) of a chord name.

    Maps the root pitch class to its A4=440 Hz fundamental so the waveform
    drawn by render() carries the chord's actual pitch. Parses the root note
    and optional accidental ('#' or 'b') at the start of the chord name so
    'B', 'Bb', and 'Bm' are distinguished cleanly.
    """
    if not name:
        return 261.63  # C4 default
    pc = _parse_pitch_class(name)
    if pc is None:
        # Fallback for Roman numerals if passed instead of chord name
        roman_semitones = {
            "I": 0, "II": 2, "III": 4, "IV": 5, "V": 7, "VI": 9, "VII": 11,
        }
        s = name.strip()
        acc = -1 if s.startswith(("b", "♭")) else (1 if s.startswith(("#", "♯")) else 0)
        base = re.sub(r"[^IViv]", "", s).upper()
        pc = (roman_semitones.get(base, 0) + acc) % 12
    # A4 = 440 Hz at pitch_class 9 (A). Formula: f = 440 * 2^((pc-9)/12)
    return 440.0 * (2.0 ** ((pc - 9) / 12.0))


def _progression_shape(name: str) -> dict:
    """Map a chord's written name to a visual-geometry descriptor. The chord
    name carries the quality (major/minor/diminished/extended), which we map to
    a distinct ring / ray / waveform shape so progressions read differently.
    """
    if not name:
        return {"mode": "stable", "rays": 0.0, "ring_mod": 0.0, "wave": "flat"}
    n = name.strip().lower()
    if "°" in n or "dim" in n:
        return {"mode": "jagged",  "rays": 0.14, "ring_mod": 0.14, "wave": "sawtooth"}
    if "7" in n or "9" in n or "11" in n or "13" in n:
        if "m" in n and not n.startswith("maj") and "maj" not in n:
            return {"mode": "sway", "rays": 0.0, "ring_mod": 0.05, "wave": "sine"}
        return {"mode": "rich",    "rays": 0.08, "ring_mod": 0.06, "wave": "sawtooth"}
    if "m" in n and "maj" not in n:
        return {"mode": "sway",    "rays": 0.0,  "ring_mod": 0.05, "wave": "sine"}
    return {"mode": "stable",  "rays": 0.0,  "ring_mod": 0.0,  "wave": "flat"}


def _wave_color_for(fundamental: float, chord_roman: str) -> tuple[int, int, int]:
    """Pick a bright, saturated colour for the waveform from the chord's hue."""
    hue = ((fundamental / 440.0) + _chord_hue_offset(chord_roman)) % 1.0
    return _hsl(hue, 0.88, 0.94)


def _draw_waveform_on(image: np.ndarray, elapsed: float, freq: float,
                      color: tuple[int, int, int], wave: str = "sine") -> np.ndarray:
    """Draw a smooth 2-D harmonic waveform ribbon across the frame, accented
    around the playhead position.

    The waveform's spatial frequency is set by the chord's fundamental pitch
    (``freq``), its harmonic contour follows ``wave`` (sine/sawtooth/triangle/flat),
    and its phase and playhead advance with ``elapsed``.

    Accepts a float image in [0, 1] range and returns a float image in [0, 1]
    composited with alpha-over blending.
    """
    img = np.clip(image, 0.0, 1.0).astype(np.float32)
    rows, cols = img.shape[0], img.shape[1]

    # Map elapsed seconds to a horizontal playhead across the frame
    playhead_x = ((float(elapsed) % 60.0) / 60.0) * cols
    xx = np.arange(cols, dtype=np.float32)
    yy = np.arange(rows, dtype=np.float32)[:, None]

    # Spatial cycles across width proportional to chord fundamental frequency
    cycles = 3.5 + (freq / 110.0) * 1.8
    phase = (xx / float(cols)) * cycles * 2.0 * math.pi + float(elapsed) * 1.8

    if wave == "sawtooth":
        osc = 0.65 * np.sin(phase) - 0.30 * np.sin(2.0 * phase) + 0.16 * np.sin(3.0 * phase)
    elif wave == "triangle":
        t = (phase % (2.0 * math.pi)) / (2.0 * math.pi)
        osc = (1.0 - 4.0 * np.abs(t - 0.5))
    elif wave == "flat":
        osc = 0.35 * np.sin(phase)
    else:
        osc = np.sin(phase) + 0.25 * np.sin(phase * 2.0 + 0.6)

    # Envelope: strongest near the playhead and across the center field
    playhead_dist = np.abs(xx - playhead_x) / (cols * 0.28)
    envelope = 0.35 + 0.65 * np.exp(-(playhead_dist ** 2))

    amp = float(rows) * 0.055
    mid_y = float(rows) * 0.44
    y_centers = mid_y + amp * osc * envelope  # shape: (cols,)

    # Soft glowing ribbon around y_centers[x]
    dy = np.abs(yy - y_centers[None, :])      # shape: (rows, cols)
    core_ribbon = np.exp(-((dy / 2.2) ** 2)) * 0.65
    halo_ribbon = np.exp(-((dy / 9.0) ** 2)) * 0.22
    overlay_alpha = np.clip((core_ribbon + halo_ribbon) * envelope[None, :], 0.0, 0.85)

    color_rgb = np.array(color, dtype=np.float32) / 255.0
    return _over(img, color_rgb, overlay_alpha)


@dataclass
class DummyAnalysis:
    """Lightweight stand-in for a measured ``track_analysis`` row."""

    energy: float = 0.5
    bpm: float = 120.0
    brightness: float = 0.5
    resolved_key: Any = None
    detected_key: Any = None
    genre: str = "trip hop"
    form: str = "static"
    title: str = ""
    artist: str = ""
    # v2-timeline derived fields (filled by the control API route)
    highlighted_word: str = ""    # current word being sung (lyrics v2 timeline)
    current_mood: str = ""        # current mood from the v2 mood timeline
    phase: float = 0.37           # active-line progress (0..1) at the tracked time
    chord_roman: str = ""
    chord: Optional[dict] = None
    has_measured_metadata: bool = True

    @classmethod
    def from_track(cls, tid: int, conn) -> "DummyAnalysis":
        cur = conn.cursor()
        cur.execute(
            "SELECT a.energy, a.bpm, a.brightness, a.detected_key, g.genre, t.title, t.artist "
            "FROM tracks t LEFT JOIN track_analysis a ON a.track_id=t.track_id "
            "LEFT JOIN track_genre g ON g.track_id=t.track_id WHERE t.track_id=%s",
            (tid,),
        )
        r = cur.fetchone()
        if not r:
            return cls(has_measured_metadata=False)
        has_measured = any(r[k] is not None for k in ("energy", "bpm", "detected_key", "genre"))
        e_val = r["energy"] if r["energy"] is not None else 0.5
        bpm_val = r["bpm"] if r["bpm"] is not None else 120.0
        b_val = r["brightness"] if r["brightness"] is not None else e_val
        return cls(
            energy=float(e_val),
            bpm=float(bpm_val),
            brightness=float(b_val),
            resolved_key=r["detected_key"],
            detected_key=r["detected_key"],
            genre=r["genre"] or "",
            form="static",
            title=r["title"] or "",
            artist=r["artist"] or "" if "artist" in r else "",
            has_measured_metadata=has_measured,
        )


def _over(base: np.ndarray, color: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    a = np.clip(alpha, 0.0, 1.0)[..., None]
    return base * (1.0 - a) + color * a


def _pixels(img: Image.Image) -> list[list[tuple[int, int, int]]]:
    out = []
    for y in range(img.height):
        row = []
        for x in range(img.width):
            row.append(tuple(img.getpixel((x, y))))
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# Title band
# ---------------------------------------------------------------------------
def _font_paths() -> list[str]:
    """Candidate locations for DejaVu fonts (used for the title band)."""
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        "/opt/libreoffice25.8/share/fonts/truetype/DejaVuSans-Bold.ttf",
        "/opt/libreoffice25.8/share/fonts/truetype/DejaVuSans.ttf",
    ]
    return [p for p in candidates if os.path.exists(p)]


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for p in _font_paths():
        if bold and "Bold" in p:
            return ImageFont.truetype(p, size)
    for p in _font_paths():
        if not bold and "Bold" not in p:
            return ImageFont.truetype(p, size)
    for p in _font_paths():
        return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def _fit_title_font(draw: ImageDraw.ImageDraw, text: str, max_w: int,
                    start_size: int = 92, min_size: int = 44) -> ImageFont.FreeTypeFont:
    size = start_size
    while size > min_size:
        font = load_font(size, bold=True)
        bbox = draw.textbbox((0, 0), text, font=font)
        if (bbox[2] - bbox[0]) <= max_w:
            return font
        size -= 6
    return load_font(min_size, bold=True)


def draw_title_band(img: Image.Image, drawing: ImageDraw.Draw,
                    title: str, artist: str, energy: Optional[float],
                    bpm: Optional[float], key: str, genre: str, W: int, H: int,
                    highlighted_word: str = "",
                    chord_label: str = "") -> None:
    """Translucent lower title card centered over the lower ring field."""
    title_str = (title or "UNTITLED").strip().upper()
    artist_str = (artist or "").strip()

    meta_parts = []
    if artist_str:
        meta_parts.append(artist_str)
    if bpm:
        meta_parts.append(f"{bpm:.0f} BPM")
    if key and key != "—":
        meta_parts.append(str(key))
    if genre:
        meta_parts.append(str(genre).lower())
    if energy is not None:
        meta_parts.append(f"energy {energy:.0%}")
    if chord_label:
        meta_parts.append(chord_label)
    meta_str = "  ·  ".join(meta_parts)

    word_str = (highlighted_word or "").strip().upper()
    max_text_w = int(W * 0.82)

    tfont = _fit_title_font(drawing, title_str, max_text_w, start_size=92, min_size=44)
    wfont = load_font(42, bold=False)
    mfont = load_font(28, bold=False)

    tb = drawing.textbbox((0, 0), title_str, font=tfont)
    th = tb[3] - tb[1]
    tw = tb[2] - tb[0]

    mh, mw = 0, 0
    if meta_str:
        mb = drawing.textbbox((0, 0), meta_str, font=mfont)
        mh = mb[3] - mb[1]
        mw = mb[2] - mb[0]

    wh, ww = 0, 0
    if word_str:
        wb = drawing.textbbox((0, 0), word_str, font=wfont)
        wh = wb[3] - wb[1]
        ww = wb[2] - wb[0]

    pad_x = 64
    pad_top = 28
    pad_bot = 32
    gap_word = 16 if word_str else 0
    gap_meta = 20 if meta_str else 0

    content_h = th + (wh + gap_word if word_str else 0) + (mh + gap_meta if meta_str else 0)
    panel_h = content_h + pad_top + pad_bot
    panel_w = min(W - 80, max(int(W * 0.44), max(tw, mw, ww) + pad_x * 2))

    panel_x0 = int((W - panel_w) // 2)
    panel_y1 = int(H - 36)
    panel_y0 = int(panel_y1 - panel_h)

    # Translucent dark overlay panel (alpha=160) so the lower rings show through
    panel = Image.new("RGBA", (panel_w, panel_h), (6, 7, 15, 0))
    mask = Image.new("L", (panel_w, panel_h), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.rounded_rectangle([0, 0, panel_w - 1, panel_h - 1], radius=22, fill=160)
    panel.putalpha(mask)
    img.paste(panel, (panel_x0, panel_y0), panel)

    dr = ImageDraw.Draw(img)
    cx = W // 2

    def _centered_line(center_y: int, text: str, fill: tuple[int, int, int], font) -> None:
        dr.text((cx + 3, center_y + 4), text, fill=(0, 0, 0), font=font, anchor="mm")
        dr.text((cx, center_y), text, fill=fill, font=font, anchor="mm")

    cur_y = panel_y0 + pad_top + th // 2
    _centered_line(cur_y, title_str, C_CORE_MAX, tfont)
    cur_y += th // 2

    if word_str:
        cur_y += gap_word + wh // 2
        _centered_line(cur_y, word_str, (218, 192, 242), wfont)
        cur_y += wh // 2

    if meta_str:
        cur_y += gap_meta + mh // 2
        _centered_line(cur_y, meta_str, C_TEXT, mfont)


def _ractive_animation_teeth(yy: np.ndarray, xx: np.ndarray,
                             dist: np.ndarray, phase: float, surge: float,
                             wave: str = "sine", ring_mod: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
    """Build concentric pulse rings and beat-flare rings around the core.

    The ``wave`` selector picks the harmonic shape (sine / triangle / sawtooth /
    flat) and ``ring_mod`` nudges spacing so chord quality and progressions
    alter the ring geometry.
    """
    ring_buf = np.zeros((IR, IC), dtype=np.float32)
    flare_buf = np.zeros((IR, IC), dtype=np.float32)
    RING_CNT = 9
    step_scale = 54.0 * SCALE * (1.0 + 0.25 * ring_mod)

    for i in range(RING_CNT):
        base_r = (i + 1) * step_scale
        arg = phase * math.pi * 2.2 + i * (1.1 + ring_mod)
        if wave == "sawtooth":
            t = (arg % (2 * math.pi)) / (2 * math.pi)
            osc = 0.72 + 0.28 * t
        elif wave == "triangle":
            t = (arg % (2 * math.pi)) / (2 * math.pi)
            tri = 1.0 - 4.0 * abs(t - 0.5)
            osc = 0.72 + 0.28 * (tri * 0.5 + 0.5)
        elif wave == "flat":
            osc = 0.78 + 0.08 * math.sin(i * 0.9)
        else:  # sine
            osc = 0.72 + 0.28 * math.sin(arg)

        r = base_r * osc
        bw = (9.0 + (i % 4) * 3.0) * SCALE
        d_ring = np.abs(dist - r)

        # Smooth ring band + soft outer halo so rings carry visible colour saturation
        band = np.clip(1.0 - d_ring / (bw * 1.35), 0.0, 1.0)
        weight = ((RING_CNT + 1 - i) / float(RING_CNT)) * (0.55 + 0.35 * surge)
        ring_buf += (band * weight).astype(np.float32)

        # Wider beat-flare halo around each ring
        band_f = np.clip(1.0 - d_ring / (bw * 3.2), 0.0, 1.0) ** 1.5
        flare_w = 0.48 * (0.6 + 0.4 * surge) * (0.5 + 0.5 * math.cos(i * 2.3 + phase * 5.0))
        flare_buf += (band_f * flare_w).astype(np.float32)

    return np.clip(ring_buf * 0.55, 0.0, 1.0), np.clip(flare_buf * 0.65, 0.0, 1.0)


_ray_cache: dict[tuple[int, int, int], dict[str, Any]] = {}


def _compute_ray_grid(ray_angles: np.ndarray, yy: np.ndarray, xx: np.ndarray,
                      cx: float, cy: float) -> np.ndarray:
    """Aggregate 36 ray contributions into one (IR, IC) grid.

    Ray geometry depends only on the seed and canvas dimensions, so this is
    computed once per seed and cached in-process.
    """
    scale = float(SCALE)
    vx = ((xx - cx) / scale).astype(np.float32)
    vy = ((yy - cy) / scale).astype(np.float32)
    n = max(1, len(ray_angles))
    ray_len_base = 0.36 * max(IR, IC) / scale
    width = 9.0
    ray_buf = np.zeros((IR, IC), dtype=np.float32)
    for a in ray_angles:
        u = np.float32(math.cos(a))
        v = np.float32(math.sin(a))
        t = vx * u + vy * v
        d = np.abs(vx * v - vy * u)
        in_seg = (t >= 0.0) & (t <= ray_len_base) & (d < width)
        falloff = np.clip(1.0 - d / width, 0.0, 1.0) * np.clip(1.0 - t / ray_len_base, 0.0, 1.0)
        ray_buf += np.where(in_seg, falloff, 0.0).astype(np.float32)
    return np.clip(ray_buf / (n * 0.18), 0.0, 1.0).astype(np.float32)


def _rays(ray_angles: np.ndarray, yy: np.ndarray, xx: np.ndarray,
          cx: float, cy: float) -> np.ndarray:
    return _compute_ray_grid(ray_angles, yy, xx, cx, cy)


def render(analysis: Any, mood: str = "neutral", seed: int = 0,
           cols: int = TARGET_W, rows: int = TARGET_H,
           elapsed: Optional[float] = None) -> Image.Image:
    """Render the mood still for a measured track.

    Deterministic for a given (analysis, mood, seed, elapsed). Returns an RGB
    ``PIL.Image.Image`` at ``cols`` x ``rows`` (default 1920x1080).
    """
    rng = np.random.default_rng(seed)

    # --- feature mapping (see creative/mood-image/references/feature-mapping.md) ---
    base_hue = _key_hue(analysis)
    minor = _is_minor(analysis)
    raw_energy = getattr(analysis, "energy", 0.5)
    energy = min(1.0, max(0.0, float(0.5 if raw_energy is None else raw_energy)))
    bpm = float(getattr(analysis, "bpm", 120.0) or 120.0)
    raw_bright = getattr(analysis, "brightness", None)
    brightness = min(1.0, max(0.0, float(energy if raw_bright is None else raw_bright)))

    # --- v2-timeline params: mood + phase + current word + chord ------------------
    active_mood = getattr(analysis, "current_mood", "") or mood or ""
    mood_shift = _mood_hue_shift(active_mood)
    raw_phase = getattr(analysis, "phase", None)
    phase = 0.37 if raw_phase is None else float(raw_phase)
    if elapsed is not None and (raw_phase is None or raw_phase == 0.37):
        # Advance phase with elapsed beats even when no lyrics timeline is present
        phase = (0.37 + (float(elapsed) * (bpm / 60.0) * 0.25)) % 1.0

    highlight_word = str(getattr(analysis, "highlighted_word", "") or "").strip()
    chord_roman = str(getattr(analysis, "chord_roman", "") or "").strip()
    chord = getattr(analysis, "chord", None) or {}
    chord_name = str(chord.get("chord") or "").strip()
    chord_shift = _chord_hue_offset(chord_roman or "")
    chord_sat, chord_val = _chord_quality_fall(chord)

    # --- progression-shape descriptor (different geometry per chord name) ----
    progression_shape = _progression_shape(chord_name)
    ring_mod = progression_shape["ring_mod"]
    ray_density = progression_shape["rays"]
    wave_type = progression_shape["wave"]

    # key + mood + chord Roman numeral combine to place the palette hue
    base_hue = (base_hue + mood_shift + chord_shift) % 1.0

    # map each measured field onto a visual parameter
    field_sat = min(0.95, max(0.20, 0.42 + 0.36 * energy + chord_sat))
    field_val = min(0.65, max(0.12, 0.18 + 0.20 * brightness + chord_val))
    if minor:                       # minor keys sit in a cooler, deeper tonal field
        field_sat *= 0.88
        field_val *= 0.84

    ring_freq = (0.6 + (bpm / 120.0)) + ring_mod * 0.8
    surge = 0.55 + 0.45 * max(energy, abs(math.sin(phase * math.pi)))

    # Genre texture tie-in (modulates film grain and ring pulse depth)
    genre_name = str(getattr(analysis, "genre", "") or "").lower()
    genre_grain_boost = 1.25 if any(g in genre_name for g in ("trip hop", "rock", "punk", "industrial", "metal")) else 1.0

    def color(h: float, s: float, v: float) -> np.ndarray:
        return np.array(_hsl(h, s, v), dtype=np.float32) / 255.0

    # ---- key-driven palette: each key shifts the field & rings -------------------
    C_FIELD_CORE = color(base_hue, min(0.90, field_sat * 1.15), min(0.42, field_val * 1.25))
    C_FIELD_MID = color((base_hue + 0.03) % 1.0, field_sat, field_val * 0.78)
    C_FIELD_DEEP = color((base_hue - 0.04) % 1.0, field_sat * 0.85, max(0.06, field_val * 0.36))

    C_RING_COL = color((base_hue + 0.07) % 1.0, min(0.92, field_sat * 1.15),
                       min(0.82, 0.48 + 0.32 * energy))
    C_RING_PULSE = color((base_hue - 0.08) % 1.0, min(0.88, 0.55 + 0.30 * energy),
                         min(0.92, 0.62 + 0.28 * energy))
    C_FLARE = (np.array(C_ACCENT, np.float32) / 255.0) * (0.68 + 0.32 * energy)
    C_CORE = (np.array(C_CORE_MAX, np.float32) / 255.0) * (0.78 + 0.22 * energy)
    C_GLOW = (np.array(C_CORE_MID, np.float32) / 255.0) * (0.65 + 0.35 * energy)
    C_RAY = color((base_hue + 0.04) % 1.0, min(0.85, field_sat * 0.95),
                  min(0.75, 0.38 + 0.34 * energy))

    # Build isotropic 2-D coordinate grid: yy in [0..IR) (rows/height), xx in [0..IC) (cols/width)
    yy, xx = np.mgrid[0:IR, 0:IC]
    cx, cy = IC / 2.0, IR * 0.44   # slightly above vertical midpoint so lower title card fits cleanly
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2)
    maxr = max(IR, IC) * 0.65
    dnorm = np.clip(dist / maxr, 0.0, 1.0)

    # ---- 1) background: key-tinted radial gradient -------------------------------
    bg_base_core = 0.55 * (np.array(C_BG_CORE, dtype=np.float32) / 255.0) + 0.45 * C_FIELD_CORE
    bg_base_mid = 0.55 * (np.array(C_BG_TOP, dtype=np.float32) / 255.0) + 0.45 * C_FIELD_MID
    bg_base_deep = 0.65 * (np.array(C_BG_DEEP, dtype=np.float32) / 255.0) + 0.35 * C_FIELD_DEEP

    bg = np.zeros((IR, IC, 3), dtype=np.float32)
    for k in range(3):
        inner = _lerp(bg_base_core[k], bg_base_mid[k], np.clip(dnorm * 1.4, 0.0, 1.0))
        bg[..., k] = _lerp(inner, bg_base_deep[k], dnorm ** 1.25)

    # ---- 2) rings + beat flare (pulse rhythm set by BPM & chord progression) -----
    phase_r = phase * ring_freq / 1.6
    ring_buf, flare_buf = _ractive_animation_teeth(
        yy, xx, dist, phase_r, surge, wave=wave_type, ring_mod=ring_mod)

    # ---- 3) core + off-center tension flare --------------------------------------
    core_r = (145.0 * SCALE) * (0.52 + 0.48 * surge)
    g = np.clip(1.0 - dist / (core_r * 3.4), 0.0, 1.0)
    glow_alpha = (g ** 1.8) * (0.34 + 0.16 * energy)
    core_alpha = np.where(
        dist <= core_r,
        (0.62 + 0.22 * energy) * np.clip(1.0 - (dist - 0.02 * core_r) / (0.65 * core_r), 0.0, 1.0),
        0.0,
    )

    # Off-center tension flare (gives warm crimson/terracotta depth around the core)
    flare_x = cx + (115.0 * SCALE) * math.cos(phase_r * 1.3)
    flare_y = cy + (95.0 * SCALE) * math.sin(phase_r * 1.5)
    d_f = np.sqrt((xx - flare_x) ** 2 + (yy - flare_y) ** 2)
    flare_ellipse = np.exp(-((d_f / (210.0 * SCALE)) ** 2))
    flare_spot_alpha = flare_ellipse * (0.42 + 0.25 * energy)

    # ---- 4) radiating rays (intensity from energy + chord ray_density) -----------
    ray_angles = rng.uniform(0, 2 * math.pi, 36)
    cache_id = (seed, IR, IC)
    cfg = _ray_cache.get(cache_id)
    if cfg is None:
        ray_grid = _compute_ray_grid(ray_angles, yy, xx, cx, cy)
        cfg = {"angles": ray_angles, "grid": ray_grid}
        _ray_cache[cache_id] = cfg
    ray_buf = cfg["grid"]

    # ---- composite with alpha-over (stays in [0, 1], never blows out) ------------
    out = bg
    out = _over(out, C_RING_COL, ring_buf * (0.68 + 0.30 * energy))
    out = _over(out, C_FLARE, flare_buf * (0.58 + 0.36 * energy))
    out = _over(out, C_RING_PULSE, (ring_buf * flare_buf) * (0.45 + 0.35 * energy))
    out = _over(out, C_RAY, ray_buf * (0.30 + 0.32 * energy + ray_density * 1.4))
    out = _over(out, C_FLARE, flare_spot_alpha)
    out = _over(out, C_GLOW, glow_alpha)
    out = _over(out, C_CORE, core_alpha)

    # ---- 5) waveform overlay (when elapsed and a real chord are active) ----------
    if elapsed is not None and (chord_name or chord_roman):
        freq = _chord_frequency(chord_name or chord_roman)
        wave_color = _wave_color_for(freq, chord_roman)
        out = _draw_waveform_on(out, float(elapsed), freq, wave_color, wave=wave_type)

    # Single film-grain pass (alpha-blended so highlights cannot clip)
    grain_amp = (C_GRAIN * genre_grain_boost) / 255.0
    grain = rng.random((IR, IC, 1)).astype(np.float32) * grain_amp
    out = out * 0.92 + grain * 0.08

    # Soft bottom vignette so the lower title card sits cleanly over the field
    vy = np.linspace(0.0, 1.0, IR, dtype=np.float32)[:, None]
    vig = np.clip((vy - 0.58) / 0.42, 0.0, 1.0)[..., None]
    vig_col = np.array(C_BG_DEEP, dtype=np.float32) / 255.0
    out = out * (1.0 - vig * 0.48) + vig_col * (vig * 0.48)

    out = np.clip(out, 0.0, 1.0)
    out_img = Image.fromarray((out * 255.0).astype(np.uint8), "RGB")
    if (cols, rows) != (IC, IR):
        out_img = out_img.resize((cols, rows), Image.Resampling.LANCZOS)

    draw = ImageDraw.Draw(out_img)
    chord_label = ""
    if chord_name and chord_roman and chord_name != chord_roman:
        chord_label = f"{chord_name} ({chord_roman})"
    elif chord_name or chord_roman:
        chord_label = chord_name or chord_roman

    has_meta = getattr(analysis, "has_measured_metadata", True)
    disp_energy = energy if has_meta else None
    disp_bpm = bpm if has_meta else None
    disp_key = str(getattr(analysis, "detected_key", getattr(analysis, "resolved_key", "")) or "—") if has_meta else ""
    disp_genre = str(getattr(analysis, "genre", "") or "") if has_meta else ""

    draw_title_band(
        out_img, draw,
        title=str(getattr(analysis, "title", "") or ""),
        artist=str(getattr(analysis, "artist", "") or
                   str(getattr(analysis, "name", "") or "")),
        energy=disp_energy,
        bpm=disp_bpm,
        key=disp_key,
        genre=disp_genre,
        highlighted_word=highlight_word,
        chord_label=chord_label,
        W=cols, H=rows,
    )
    return out_img
