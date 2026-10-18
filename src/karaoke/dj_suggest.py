"""What to play next: songs that fit, filtered by what makes a smooth mix.

The DJ booth's ``/suggest`` used to pick by key and tempo alone: a Camelot
neighbour within 12% BPM, nearest BPM first. Measured against what radio DJs
actually played next -- 295 consecutive pairs from the radio sessions, the
true next song ranked among 200 random library songs -- that ranked the
right song no better than chance (median 121 of 201). So did key, tempo or
chord motion on their own. What predicted the next song:

=========================  ======  ============  =====
signal                     top-10  median rank   MRR
=========================  ======  ============  =====
old /suggest (harmonic)     6.8%       121       0.050
timbre (CLAP)              46.9%        15       0.271
mood flow (end -> start)   19.7%        32       0.085
timbre + 0.1 mood flow     46.3%        14       0.305
 + one song per act        51.7%         8       0.347
=========================  ======  ============  =====

(weights tuned on half the pairs, reported on the other half; radio DJs
repeated an act in only 4 of 147 transitions.)

So every strategy ranks by **fit** -- the sound, plus how the candidate's
opening mood follows the current song's closing mood -- and the strategies
differ in what they *require*: compatible keys and tempo for a harmonic mix,
rising or falling intensity for energy, chord motion for structure. Key and
tempo did not predict what DJs play, but they decide whether the transition
is smooth, which is what the strategy asks for.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

from .logger import log

#: Fit = timbre + MOOD_WEIGHT * mood flow (see the table above).
MOOD_WEIGHT = 0.1
#: Candidates fetched by sound before the strategy filters them.
POOL = 150
#: Tempo still mixes within this ratio, or at half/double time.
HARMONIC_TEMPO = 0.08
WIDE_TEMPO = 0.12

STRATEGIES = ("flow", "harmonic", "energy_up", "cool_down", "mood", "structural")


@dataclass
class Track:
    track_id: int
    artist: str = ""
    title: str = ""
    key: str = ""
    camelot: Optional[str] = None
    bpm: Optional[float] = None
    energy: Optional[float] = None
    has_synced: bool = False
    clap: Any = None
    motion: Any = None
    harmony_style: Optional[str] = None
    #: (valence, arousal, feeling) of the first and last mood segment.
    start: Optional[tuple] = None
    end: Optional[tuple] = None
    extra: dict = field(default_factory=dict)


# -- the pure part -------------------------------------------------------------

def camelot_score(a: Optional[str], b: Optional[str]) -> float:
    """1 same key, 0.8 relative or one step, 0.4 two steps, else 0."""
    if not a or not b or a == "?" or b == "?":
        return 0.0
    na, la, nb, lb = int(a[:-1]), a[-1], int(b[:-1]), b[-1]
    d = min((na - nb) % 12, (nb - na) % 12)
    if d == 0:
        return 1.0 if la == lb else 0.8
    if la != lb:
        return 0.0
    return {1: 0.8, 2: 0.4}.get(d, 0.0)


def tempo_ratio(a: Optional[float], b: Optional[float]) -> Optional[float]:
    """Relative tempo distance, allowing half and double time (0 = same)."""
    if not a or not b:
        return None
    return min(abs(math.log(b / a)), abs(math.log(b / (2 * a))), abs(math.log(b / (a / 2))))


def cosine(u, v) -> float:
    if u is None or v is None:
        return 0.0
    import numpy as np

    u, v = np.asarray(u, dtype=float), np.asarray(v, dtype=float)
    denom = float(np.linalg.norm(u) * np.linalg.norm(v))
    return float(u @ v / denom) if denom else 0.0


def mood_flow(seed: Track, cand: Track) -> float:
    """How naturally the candidate's opening follows the seed's closing, 0..1."""
    if not seed.end or not cand.start:
        return 0.0
    (va, aa, _), (vb, ab, _) = seed.end, cand.start
    return math.exp(-math.hypot(va - vb, math.tanh(aa) - math.tanh(ab)) / 0.6)


def arousal_step(seed: Track, cand: Track) -> Optional[float]:
    """Candidate's opening intensity minus the seed's closing intensity."""
    if not seed.end or not cand.start:
        return None
    return math.tanh(cand.start[1]) - math.tanh(seed.end[1])


def score(seed: Track, cand: Track, strategy: str) -> Optional[float]:
    """The candidate's score under a strategy, or None if it does not qualify."""
    timbre = cosine(seed.clap, cand.clap)
    flow = mood_flow(seed, cand)
    fit = timbre + MOOD_WEIGHT * flow
    ratio = tempo_ratio(seed.bpm, cand.bpm)
    key = camelot_score(seed.camelot, cand.camelot)

    if strategy == "harmonic":
        if key < 0.8 or ratio is None or ratio > cand.extra.get("tempo_limit", HARMONIC_TEMPO):
            return None
        return fit + 0.05 * key
    if strategy in ("energy_up", "cool_down"):
        up = strategy == "energy_up"
        step = arousal_step(seed, cand)
        bpm_up = (cand.bpm - seed.bpm) if (seed.bpm and cand.bpm) else None
        if step is None and bpm_up is None:
            return None
        direction = 1 if up else -1
        # Must move the right way on intensity, or (without mood data) tempo.
        if step is not None and direction * step <= 0.05:
            return None
        if step is None and direction * bpm_up < 3:
            return None
        if bpm_up is not None and direction * bpm_up < -10:
            return None          # a big tempo drop is not an energy lift
        return fit + 0.5 * direction * (step or 0.0)
    if strategy == "mood":
        return 0.5 * timbre + flow
    if strategy == "structural":
        if cand.motion is None or seed.motion is None:
            return None
        style = 0.1 if (seed.harmony_style and seed.harmony_style == cand.harmony_style) else 0.0
        return cosine(seed.motion, cand.motion) + 0.3 * timbre + style
    return fit                    # flow


def reason(seed: Track, cand: Track) -> str:
    """Why this one: the sound, the mood handover, key and tempo."""
    parts = []
    t = cosine(seed.clap, cand.clap)
    if t:
        parts.append("same sound" if t >= 0.85 else "similar sound" if t >= 0.7 else f"sound {t:.2f}")
    if seed.end and cand.start:
        a, b = seed.end[2] or "", cand.start[2] or ""
        if a and b:
            parts.append(f"{a} → {b}" if a != b else f"stays {a}")
    k = camelot_score(seed.camelot, cand.camelot)
    if seed.camelot and cand.camelot:
        tag = "same key" if seed.camelot == cand.camelot else ("compatible key" if k >= 0.8 else "key")
        parts.append(f"{tag} {seed.camelot}→{cand.camelot}" if seed.camelot != cand.camelot else tag)
    if seed.bpm and cand.bpm:
        diff = cand.bpm - seed.bpm
        parts.append(f"{round(cand.bpm)} BPM ({'+' if diff >= 0 else ''}{round(diff)})")
    return " • ".join(parts)


def artist_key(artist: str) -> str:
    """One name per act: "The Jimi Hendrix Experience" and "Jimi Hendrix" match."""
    import re

    a = (artist or "").casefold().strip()
    a = re.split(r"\s+(?:feat\.?|ft\.?|featuring|&|and|with|x)\s+|,|/| - ", a)[0].strip()
    a = re.sub(r"^the\s+", "", a)
    a = re.sub(r"\s+(experience|band|trio|quartet|orchestra|group)$", "", a)
    return a


def rank(seed: Track, candidates: list[Track], strategy: str, limit: int,
         excluded: set[int] = frozenset()) -> list[tuple[float, Track]]:
    """Best candidates under a strategy; harmonic widens its tempo if short.

    "Same sound" finds the same act first -- The Pretender drew three Foo
    Fighters songs, Catfish Blues three Hendrix -- and back-to-back songs by
    one act is poor DJing. So the seed's act is skipped while others remain,
    and each act appears once in a list.
    """
    seed_act = artist_key(seed.artist)

    def run(tempo_limit: float, allow_seed_act: bool):
        scored = []
        for c in candidates:
            if c.track_id == seed.track_id or c.track_id in excluded:
                continue
            if not allow_seed_act and seed_act and artist_key(c.artist) == seed_act:
                continue
            c.extra["tempo_limit"] = tempo_limit
            s = score(seed, c, strategy)
            if s is not None:
                scored.append((s, c))
        scored.sort(key=lambda x: x[0], reverse=True)
        out, acts = [], set()
        for s, c in scored:
            act = artist_key(c.artist)
            if act and act in acts:
                continue
            acts.add(act)
            out.append((s, c))
            if len(out) >= limit:
                break
        return out

    ranked: list[tuple[float, Track]] = []
    for tempo_limit in ((HARMONIC_TEMPO, WIDE_TEMPO) if strategy == "harmonic" else (HARMONIC_TEMPO,)):
        ranked = run(tempo_limit, allow_seed_act=False)
        if len(ranked) >= limit:
            return ranked
    # Short of other acts: the seed's own act fills the *remaining* slots,
    # after everything else, never ahead of it.
    have = {c.track_id for _, c in ranked}
    extra = [(s, c) for s, c in run(tempo_limit, allow_seed_act=True) if c.track_id not in have]
    return (ranked + extra)[:limit]


# -- loading -------------------------------------------------------------------

def _load(track_ids: list[int], conn, os_client) -> dict[int, Track]:
    """Everything the scorer reads, for many tracks in a few queries."""
    import numpy as np

    from . import clap_vector, musictheory
    from .key_progression import PROGRESSION_INDEX, doc_id

    if not track_ids:
        return {}
    tracks: dict[int, Track] = {}
    for r in conn.execute(
            """SELECT t.track_id, t.artist, t.title, a.detected_key, a.bpm, a.energy,
                      EXISTS (SELECT 1 FROM lyrics l WHERE l.track_id = t.track_id
                              AND l.synced_lyrics != '') AS has_synced
               FROM tracks t LEFT JOIN track_analysis a ON a.track_id = t.track_id
               WHERE t.track_id = ANY(%s)""", (list(track_ids),)).fetchall():
        k = musictheory.parse_key(r["detected_key"]) if r["detected_key"] else None
        tracks[int(r["track_id"])] = Track(
            track_id=int(r["track_id"]), artist=r["artist"] or "", title=r["title"] or "",
            key=r["detected_key"] or "", camelot=(k.camelot if k and k.camelot != "?" else None),
            bpm=r["bpm"], energy=r["energy"], has_synced=bool(r["has_synced"]))
    try:
        for r in conn.execute("SELECT track_id, segments FROM track_moods WHERE track_id = ANY(%s)",
                              (list(track_ids),)).fetchall():
            segs = r["segments"] or []
            t = tracks.get(int(r["track_id"]))
            if t and segs:
                first, last = segs[0], segs[-1]
                t.start = (first["valence"], first["arousal"], first.get("feeling") or first["mood"])
                t.end = (last["valence"], last["arousal"], last.get("feeling") or last["mood"])
    except Exception:
        conn.rollback()
    if os_client is not None:
        try:
            docs = os_client.mget(index=clap_vector.CLAP_INDEX, _source=["clap_vector"],
                                  body={"ids": [clap_vector.doc_id(t) for t in tracks]})["docs"]
            for d in docs:
                if d.get("found") and d["_source"].get("clap_vector"):
                    tid = int(d["_id"].split(":")[-1])
                    tracks[tid].clap = np.asarray(d["_source"]["clap_vector"], dtype=np.float32)
        except Exception:
            log.debug("dj_suggest: CLAP lookup failed", exc_info=True)
        try:
            docs = os_client.mget(index=PROGRESSION_INDEX, _source=["chord_motion", "harmony_style"],
                                  body={"ids": [doc_id(t) for t in tracks]})["docs"]
            for d in docs:
                src = d.get("_source") or {}
                if d.get("found"):
                    tid = int(d["_id"].split(":")[-1])
                    if src.get("chord_motion"):
                        tracks[tid].motion = np.asarray(src["chord_motion"], dtype=np.float32)
                    tracks[tid].harmony_style = src.get("harmony_style")
        except Exception:
            log.debug("dj_suggest: progression lookup failed", exc_info=True)
    return tracks


def _candidate_ids(seed: Track, strategy: str, conn, os_client) -> list[int]:
    """By sound when the seed has a CLAP vector; otherwise by key and tempo."""
    from . import search

    ids: list[int] = []
    if seed.clap is not None and os_client is not None:
        ids = [h.track_id for h in search.sounds_like_track(seed.track_id, k=POOL,
                                                            os_client=os_client)]
    if strategy == "structural" and os_client is not None:
        ids += [s["track_id"] for s in search.similar_chords_for_track(seed.track_id, k=POOL,
                                                                       os_client=os_client)]
    if len(ids) < 20 and seed.bpm:
        # No sound to go by: a tempo band, as the old logic did.
        ids += [int(r["track_id"]) for r in conn.execute(
            """SELECT track_id FROM track_analysis WHERE bpm BETWEEN %s AND %s
               AND detected_key IS NOT NULL AND track_id != %s LIMIT %s""",
            (seed.bpm * 0.75, seed.bpm * 1.3, seed.track_id, POOL)).fetchall()]
    return list(dict.fromkeys(ids))


def suggest(seed_id: int, strategy: str = "flow", limit: int = 5,
            excluded: set[int] = frozenset(), conn=None, os_client=None) -> list[dict[str, Any]]:
    """Ranked next songs for the booth, each with the reason it was chosen."""
    from . import localcache
    from .osclient import client

    if strategy not in STRATEGIES:
        strategy = "flow"
    own = conn is None
    c = conn or localcache.connect()
    try:
        osc = os_client if os_client is not None else client()
        seed = _load([seed_id], c, osc).get(seed_id)
        if seed is None:
            return []
        pool = _load(_candidate_ids(seed, strategy, c, osc), c, osc)
        ranked = rank(seed, list(pool.values()), strategy, limit, set(excluded))
    finally:
        if own:
            c.close()
    return [{
        "track_id": t.track_id, "artist": t.artist, "title": t.title,
        "key": t.key, "camelot": t.camelot or "?", "bpm": round(t.bpm, 1) if t.bpm else None,
        "energy": round(t.energy, 2) if t.energy is not None else None,
        "has_synced_lyrics": t.has_synced,
        "feeling": t.start[2] if t.start else None,
        "score": round(s, 3), "reason": reason(seed, t),
    } for s, t in ranked]
