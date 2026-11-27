"""Benchmark the AI DJ chat across local Ollama models.

Asks the live dashboard DJ booth (``POST /api/dj/chat`` on :8900, the exact
path the dashboard uses, RAG retrieval included) a fixed set of questions
about one fully analysed library track, once per model, and reports for each
answer:

- **speed**: time to the first visible token (TTFT) and total time. Thinking
  models spend their reasoning before the first visible token, so a long TTFT
  there is "thinking", not a hang.
- **facts**: scored against the database, not by eye: key + BPM from
  ``track_analysis``, the feeling from ``track_moods``, chords from
  ``track_chords``, band members from the ``karaoke-artist-cache`` index, and
  whether a ``[QUEUE: Artist - Title]`` suggestion is a real library track
  other than the one playing.
- **system load** while the model answers: CPU %, 1-min load average, RAM
  used, GPU busy % and GPU memory (VRAM + GTT, which an APU like the Radeon
  890M uses for most of a model), sampled every 0.5 s.

Every model is unloaded before the next one starts, so its cold-load time is
measured separately and models never share the GPU.

    PYTHONPATH=src .venv/bin/python scripts/bench_dj_models.py
    PYTHONPATH=src .venv/bin/python scripts/bench_dj_models.py \\
        --model qwen3:1.7b --model qwen3:latest --track-id 74

Writes a Markdown report and the raw JSON next to it (default
``~/.local/share/karaoke/bench/``).
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

DASHBOARD_URL = "http://127.0.0.1:8900"
OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_TRACK_ID = 74          # The Slits - Typical Girls: key, BPM, mood, chords, artist cache
SAMPLE_S = 0.5
DRM = Path("/sys/class/drm")

# --------------------------------------------------------------------------
# Ground truth
# --------------------------------------------------------------------------


@dataclass
class Truth:
    track_id: int
    artist: str
    title: str
    key: str = ""                # "F minor"
    bpm: Optional[float] = None
    feeling: str = ""            # track_moods.summary.dominant_feeling
    mood: str = ""               # track_moods.summary.dominant (quadrant)
    chords: list[str] = field(default_factory=list)   # most-used first
    members: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)


def load_truth(track_id: int) -> Truth:
    from karaoke import localcache

    with localcache.connect() as conn:
        r = conn.execute(
            """
            SELECT t.artist, t.title, a.detected_key, a.bpm, m.summary, ch.chords
            FROM tracks t
            LEFT JOIN track_analysis a ON a.track_id = t.track_id
            LEFT JOIN track_moods m ON m.track_id = t.track_id
            LEFT JOIN track_chords ch ON ch.track_id = t.track_id
            WHERE t.track_id = %s
            """, (track_id,)).fetchone()
    if r is None:
        raise SystemExit(f"track {track_id} not found")
    summary = r["summary"] or {}
    truth = Truth(track_id, r["artist"], r["title"], key=r["detected_key"] or "",
                  bpm=r["bpm"], feeling=summary.get("dominant_feeling") or "",
                  mood=summary.get("dominant") or "", chords=chord_ranking(r["chords"] or []))
    try:
        from karaoke.osclient import client
        hits = client().search(index="karaoke-artist-cache", body={
            "query": {"match_phrase": {"name": truth.artist}}, "size": 1})["hits"]["hits"]
        if hits:
            src = hits[0]["_source"]
            truth.members = list(src.get("member_names") or [])
            truth.genres = list(src.get("genres") or [])
    except Exception:
        pass
    return truth


def chord_ranking(chords: list[dict[str, Any]]) -> list[str]:
    """Chord names ordered by total time held, most-used first."""
    held: dict[str, float] = {}
    for c in chords:
        name = str(c.get("name") or "").strip()
        if not name or name.upper() == "N":
            continue
        held[name] = held.get(name, 0.0) + max(0.0, float(c.get("end", 0) or 0) - float(c.get("at", 0) or 0))
    return [n for n, _ in sorted(held.items(), key=lambda kv: kv[1], reverse=True)]


# --------------------------------------------------------------------------
# Scoring (pure, unit-tested)
# --------------------------------------------------------------------------

_NOTE = r"[A-G](?:#|b|♯|♭)?"


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").replace("♯", "#").replace("♭", "b")).strip().casefold()


def key_mentioned(answer: str, key: str) -> bool:
    """True when the answer names the same key: 'F minor', 'F min', 'Fm', 'E# minor', or Camelot '4A'."""
    from karaoke import musictheory

    want = musictheory.parse_key(key) if key else None
    if want is None:
        return False
    a = answer.replace("♯", "#").replace("♭", "b")
    for m in re.finditer(rf"(?<![A-Za-z#])({_NOTE})\s*-?\s*((?i:major|maj|minor|min)|m(?![a-z]))", a):
        tonic = m.group(1)
        mode = "minor" if m.group(2).lower().startswith("m") and not m.group(2).lower().startswith("maj") else "major"
        got = musictheory.parse_key(f"{tonic} {mode}")
        if got is not None and got.tonic == want.tonic and got.mode == want.mode:
            return True
    return bool(re.search(rf"(?<![0-9]){re.escape(want.camelot)}(?![0-9A-Za-z])", a))


def bpm_mentioned(answer: str, bpm: Optional[float], tol: float = 3.0) -> bool:
    if bpm is None:
        return False
    for m in re.finditer(r"(\d{2,3}(?:\.\d+)?)\s*(?:bpm|beats)", answer, re.IGNORECASE):
        if abs(float(m.group(1)) - bpm) <= tol:
            return True
    return False


def any_mentioned(answer: str, words: list[str]) -> list[str]:
    a = _norm(answer)
    return [w for w in words if w and re.search(rf"(?<![a-z]){re.escape(_norm(w))}(?![a-z])", a)]


def chords_mentioned(answer: str, chords: list[str]) -> list[str]:
    a = answer.replace("♯", "#").replace("♭", "b")
    return [c for c in chords if re.search(rf"(?<![A-Za-z#]){re.escape(c)}(?![A-Za-z#0-9])", a)]


def parse_queue(answer: str) -> Optional[str]:
    m = re.search(r"\[QUEUE:\s*([^\]]+)\]", answer)
    return m.group(1).replace('"', "").strip() if m else None


def strip_tags(answer: str) -> str:
    return re.sub(r"\[(?:FOCUS|QUEUE):[^\]]*\]", "", answer)


QUESTIONS = [
    ("key_bpm", "What key and BPM is this song in?"),
    ("mood", "What's the mood or feeling of this song?"),
    ("chords", "Which chords does this song use?"),
    ("artist", "Tell me about {artist}. Who are the band members?"),
    ("suggest", "Suggest the next song to play after this one."),
]


def score(qid: str, answer: str, truth: Truth, conn=None) -> tuple[float, str]:
    """Return (score 0..1, short note) for one answer."""
    body = strip_tags(answer)
    if qid == "key_bpm":
        k, b = key_mentioned(body, truth.key), bpm_mentioned(body, truth.bpm)
        return (0.5 * k + 0.5 * b,
                f"key {'✓' if k else '✗'} ({truth.key}), bpm {'✓' if b else '✗'} ({round(truth.bpm or 0)})")
    if qid == "mood":
        hit = any_mentioned(body, [truth.feeling, truth.mood])
        return (1.0 if hit else 0.0, f"{'✓ ' + hit[0] if hit else '✗'} (expected {truth.feeling}/{truth.mood})")
    if qid == "chords":
        top = truth.chords[:6]
        hit = chords_mentioned(body, top)
        s = min(1.0, len(hit) / 2) if top else 0.0
        return (s, f"{len(hit)}/{len(top)} of top chords {top} ({', '.join(hit) or 'none'})")
    if qid == "artist":
        exp = truth.members[:5]
        hit = any_mentioned(body, exp)
        s = min(1.0, len(hit) / 2) if exp else 0.0
        return (s, f"members {len(hit)}/{len(exp)} ({', '.join(hit) or 'none'})")
    if qid == "suggest":
        q = parse_queue(answer)
        if not q:
            return 0.0, "no [QUEUE: …] tag"
        tid = resolve_queue(q, conn)
        if tid is None:
            return 0.25, f"'{q}' not in library (invented?)"
        if tid == truth.track_id:
            return 0.0, f"'{q}' is the song already playing"
        return 1.0, f"'{q}' → track {tid}"
    return 0.0, "unknown question"


def resolve_queue(q: str, conn) -> Optional[int]:
    from karaoke import localcache
    parts = [p.strip() for p in re.split(r"\s+[-–—]\s+", q) if p.strip()]
    if len(parts) < 2:
        return None
    artist, title = parts[-2], parts[-1]
    return localcache.find_track_id(artist, title, conn) or localcache.find_track_id_relaxed(artist, title, conn)


# --------------------------------------------------------------------------
# System load sampling
# --------------------------------------------------------------------------

def _gpu_dir() -> Optional[Path]:
    for card in sorted(DRM.glob("card[0-9]")):
        if (card / "device" / "gpu_busy_percent").exists():
            return card / "device"
    return None


def _read_int(p: Path) -> Optional[int]:
    try:
        return int(p.read_text().strip())
    except Exception:
        return None


def _cpu_times() -> tuple[int, int]:
    f = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
    vals = [int(x) for x in f]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return sum(vals), idle


def _mem_used_mb() -> float:
    info = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, v = line.split(":", 1)
        info[k] = int(v.split()[0])
    return (info["MemTotal"] - info["MemAvailable"]) / 1024


class LoadSampler:
    """Samples CPU / RAM / GPU every SAMPLE_S seconds in a thread."""

    def __init__(self) -> None:
        self.gpu = _gpu_dir()
        self.samples: list[dict[str, float]] = []
        self._stop = threading.Event()
        self._t: Optional[threading.Thread] = None

    def __enter__(self) -> "LoadSampler":
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._t:
            self._t.join()

    def _run(self) -> None:
        prev = _cpu_times()
        while not self._stop.wait(SAMPLE_S):
            cur = _cpu_times()
            dt, di = cur[0] - prev[0], cur[1] - prev[1]
            prev = cur
            s = {"cpu": 100.0 * (1 - di / dt) if dt else 0.0,
                 "load1": float(Path("/proc/loadavg").read_text().split()[0]),
                 "ram_mb": _mem_used_mb()}
            if self.gpu:
                s["gpu"] = float(_read_int(self.gpu / "gpu_busy_percent") or 0)
                vram = _read_int(self.gpu / "mem_info_vram_used") or 0
                gtt = _read_int(self.gpu / "mem_info_gtt_used") or 0
                s["gpu_mem_mb"] = (vram + gtt) / 2**20
            self.samples.append(s)

    def summary(self) -> dict[str, float]:
        if not self.samples:
            return {}
        out: dict[str, float] = {}
        for k in self.samples[0]:
            vals = [s[k] for s in self.samples]
            out[f"{k}_avg"] = round(statistics.fmean(vals), 1)
            out[f"{k}_max"] = round(max(vals), 1)
        return out


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------

@dataclass
class Result:
    model: str
    qid: str
    question: str
    answer: str = ""
    ttft_s: Optional[float] = None
    total_s: float = 0.0
    chars: int = 0
    score: float = 0.0
    note: str = ""
    error: str = ""
    think_leak: bool = False
    load: dict[str, float] = field(default_factory=dict)


def unload_all(client: httpx.Client) -> None:
    try:
        for m in client.get(f"{OLLAMA_URL}/api/ps").json().get("models", []):
            client.post(f"{OLLAMA_URL}/api/generate", json={"model": m["name"], "keep_alive": 0})
    except Exception:
        pass
    for _ in range(40):
        try:
            if not client.get(f"{OLLAMA_URL}/api/ps").json().get("models"):
                return
        except Exception:
            return
        time.sleep(0.5)


def cold_load(client: httpx.Client, model: str) -> tuple[float, str]:
    """Load the model with a 1-token request; return (load seconds, where it runs)."""
    t0 = time.monotonic()
    r = client.post(f"{OLLAMA_URL}/api/generate",
                    json={"model": model, "prompt": "hi", "stream": False,
                          "options": {"num_predict": 1}, "think": False}, timeout=600)
    wall = time.monotonic() - t0
    load_s = (r.json().get("load_duration") or 0) / 1e9 if r.status_code == 200 else wall
    where = ""
    try:
        for m in client.get(f"{OLLAMA_URL}/api/ps").json().get("models", []):
            if m.get("name") == model:
                total, vram = m.get("size") or 0, m.get("size_vram") or 0
                where = f"{round(100 * vram / total)}% GPU" if total else ""
                where += f", {total / 2**30:.1f} GiB"
    except Exception:
        pass
    return round(load_s or wall, 2), where


def ask(client: httpx.Client, model: str, question: str, truth: Truth, timeout: float) -> Result:
    res = Result(model=model, qid="", question=question)
    payload = {"message": question, "current_artist": truth.artist,
               "current_title": truth.title, "model": model, "provider": "ollama"}
    t0 = time.monotonic()
    event = ""
    with LoadSampler() as sampler:
        try:
            with client.stream("POST", f"{DASHBOARD_URL}/api/dj/chat", json=payload,
                               timeout=httpx.Timeout(timeout, read=timeout)) as resp:
                for line in resp.iter_lines():
                    if time.monotonic() - t0 > timeout:
                        res.error = f"timeout after {timeout:.0f}s"
                        break
                    if line.startswith("event:"):
                        event = line[6:].strip()
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = json.loads(line[5:].strip() or "{}")
                    if event == "token":
                        tok = data.get("token", "")
                        if tok and res.ttft_s is None and tok.strip():
                            res.ttft_s = round(time.monotonic() - t0, 2)
                        res.answer += tok
                    elif event == "error":
                        res.error = str(data.get("error"))
                    elif event == "done":
                        break
        except Exception as exc:
            res.error = f"{type(exc).__name__}: {exc}"
    res.total_s = round(time.monotonic() - t0, 2)
    res.chars = len(res.answer)
    res.think_leak = "<think>" in res.answer or "</think>" in res.answer
    res.load = sampler.summary()
    return res


def run(models: list[str], track_ids: list[int], timeout: float, out_dir: Path, label: str) -> Path:
    from karaoke import localcache

    truths = [load_truth(t) for t in track_ids]
    results: list[Result] = []
    loads: dict[str, dict[str, Any]] = {}
    with httpx.Client() as client, localcache.connect() as conn:
        for model in models:
            unload_all(client)
            print(f"\n=== {model}: loading…", flush=True)
            try:
                load_s, where = cold_load(client, model)
            except Exception as exc:
                print(f"  could not load: {exc}")
                continue
            loads[model] = {"load_s": load_s, "where": where}
            print(f"  loaded in {load_s}s ({where})", flush=True)
            for truth in truths:
                for qid, q in QUESTIONS:
                    question = q.format(artist=truth.artist)
                    r = ask(client, model, question, truth, timeout)
                    r.qid = qid
                    if not r.error:
                        r.score, r.note = score(qid, r.answer, truth, conn)
                    else:
                        r.note = r.error
                    results.append(r)
                    print(f"  {qid:8} ttft={r.ttft_s}s total={r.total_s}s score={r.score:.2f}  {r.note}"
                          f"  gpu~{r.load.get('gpu_avg', '-')}% cpu~{r.load.get('cpu_avg', '-')}%", flush=True)
        unload_all(client)

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = out_dir / f"dj-models-{stamp}{'-' + label if label else ''}"
    base.with_suffix(".json").write_text(json.dumps(
        {"truths": [asdict(t) for t in truths], "loads": loads,
         "results": [asdict(r) for r in results]}, indent=2, ensure_ascii=False))
    base.with_suffix(".md").write_text(report(models, truths, loads, results, label))
    print(f"\nReport: {base.with_suffix('.md')}\nRaw:    {base.with_suffix('.json')}")
    return base.with_suffix(".md")


def report(models: list[str], truths: list[Truth], loads: dict[str, Any],
           results: list[Result], label: str = "") -> str:
    lines = [f"# DJ model benchmark {time.strftime('%Y-%m-%d %H:%M')}{' (' + label + ')' if label else ''}", ""]
    for t in truths:
        lines.append(f"Track {t.track_id}: **{t.artist} – {t.title}** · key {t.key} · "
                     f"{round(t.bpm or 0)} BPM · feeling {t.feeling} · chords {', '.join(t.chords[:6])} · "
                     f"members {', '.join(t.members[:5])}")
    lines += ["", "## Summary", "",
              "| model | load | facts | median TTFT | median total | GPU busy avg | CPU avg | GPU mem max | RAM max | errors |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for m in models:
        rs = [r for r in results if r.model == m]
        if not rs:
            continue
        ok = [r for r in rs if not r.error]
        med = lambda xs: f"{statistics.median(xs):.1f}s" if xs else "–"   # noqa: E731
        avg = lambda k: f"{statistics.fmean([r.load[k] for r in ok if k in r.load]):.0f}" if any(k in r.load for r in ok) else "–"   # noqa: E731
        mx = lambda k: f"{max([r.load[k] for r in ok if k in r.load]) / 1024:.1f} GiB" if any(k in r.load for r in ok) else "–"   # noqa: E731
        lines.append(
            f"| {m} | {loads.get(m, {}).get('load_s', '–')}s ({loads.get(m, {}).get('where', '')}) "
            f"| {sum(r.score for r in rs):.1f}/{len(rs)} "
            f"| {med([r.ttft_s for r in ok if r.ttft_s is not None])} | {med([r.total_s for r in ok])} "
            f"| {avg('gpu_avg')}% | {avg('cpu_avg')}% | {mx('gpu_mem_mb_max')} | {mx('ram_mb_max')} "
            f"| {len(rs) - len(ok)}{' · think-leak' if any(r.think_leak for r in rs) else ''} |")
    lines += ["", "## Per question", "", "| model | question | score | TTFT | total | note |", "|---|---|---|---|---|---|"]
    for r in results:
        lines.append(f"| {r.model} | {r.qid} | {r.score:.2f} | {r.ttft_s if r.ttft_s is not None else '–'} "
                     f"| {r.total_s} | {r.note.replace('|', '/')} |")
    lines += ["", "## Answers", ""]
    for r in results:
        a = (r.answer or r.error).strip().replace("\n", " ")
        lines.append(f"- **{r.model} · {r.qid}**: {a[:400]}{'…' if len(a) > 400 else ''}")
    lines.append("")
    return "\n".join(lines)


def installed_models() -> list[str]:
    r = httpx.get(f"{OLLAMA_URL}/api/tags", timeout=5).json()
    return sorted(m["name"] for m in r.get("models", []))


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", action="append", help="Ollama model (repeatable; default: all installed)")
    ap.add_argument("--skip", action="append", default=[], help="model to leave out (repeatable)")
    ap.add_argument("--track-id", action="append", type=int, help=f"seed track (repeatable; default {DEFAULT_TRACK_ID})")
    ap.add_argument("--timeout", type=float, default=180.0, help="per-answer timeout in seconds")
    ap.add_argument("--out", type=Path, default=Path.home() / ".local/share/karaoke/bench")
    ap.add_argument("--label", default="", help="suffix for the report name, e.g. 'before-fix'")
    args = ap.parse_args(argv)
    models = [m for m in (args.model or installed_models()) if m not in set(args.skip)]
    run(models, args.track_id or [DEFAULT_TRACK_ID], args.timeout, args.out, args.label)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
