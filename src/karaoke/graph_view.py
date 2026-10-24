"""Interactive graph visualization for Karaoke similarity constellations.

Provides:
- Graph construction across structural chord motion, CLAP timbre, and Camelot wheel harmonies.
- Cytoscape.js HTML interactive web interface served at /graph.
- JSON API endpoints for Obot, Antigravity, and web clients.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from . import localcache, musictheory
from .logger import log

CAMELOT_COLORS = {
    "1A": "#00b4d8", "1B": "#48cae4",
    "2A": "#0077b6", "2B": "#0096c7",
    "3A": "#023e8a", "3B": "#03045e",
    "4A": "#5a189a", "4B": "#7b2cbf",
    "5A": "#7209b7", "5B": "#9d4edd",
    "6A": "#b5179e", "6B": "#c77dff",
    "7A": "#f72585", "7B": "#e0aaff",
    "8A": "#ff4d6d", "8B": "#ff758f",
    "9A": "#ff7b00", "9B": "#ff9e00",
    "10A": "#ffb703", "10B": "#ffd166",
    "11A": "#80b918", "11B": "#aacc00",
    "12A": "#2d6a4f", "12B": "#52b788",
}

MOOD_COLORS = {
    "happy": "#f59e0b",    # Amber / warm yellow
    "tender": "#10b981",   # Emerald / teal green
    "sad": "#3b82f6",      # Blue
    "angry": "#ef4444",    # Crimson red
    "neutral": "#94a3b8",  # Slate gray
}


def _add_mood_timelines(nodes: dict[int, dict[str, Any]], strategy: str) -> None:
    """Attach each node's mood timeline: the arc, swings and borderline share.

    Where a track has one, its dominant mood -- sound and lyric meaning
    together -- replaces the lyric-only one, so the mood colour and filter
    agree with what /mood shows.
    """
    if not nodes:
        return
    try:
        from . import mood_timeline

        with localcache.connect() as conn:
            mood_timeline.ensure_schema(conn)
            # Any version: older timelines lack feelings but still show their
            # moods, so a version bump does not blank the graph until the
            # backfill has recomputed everything.
            rows = conn.execute(
                "SELECT track_id, duration_s, segments, summary FROM track_moods"
                " WHERE track_id = ANY(%s)", (list(nodes),)).fetchall()
    except Exception as exc:
        log.debug("graph: mood timeline lookup failed: %s", exc)
        return
    for row in rows:
        node = nodes.get(int(row["track_id"]))
        summary = row["summary"] or {}
        if node is None or not summary.get("dominant"):
            continue
        node["dominant_mood"] = summary["dominant"]
        node["mood_source"] = "timeline"
        node["mood_arc"] = summary.get("feeling_arc") or summary.get("arc", "")
        node["mood_feeling"] = summary.get("dominant_feeling")
        node["mood_contrast"] = summary.get("contrast")
        node["mood_swings"] = summary.get("swing_count", 0)
        node["mood_borderline"] = summary.get("borderline_share", 0.0)
        node["mood_duration"] = row["duration_s"]
        node["mood_segments"] = [[round(seg["start_s"], 1), round(seg["end_s"], 1),
                                  seg["mood"], seg.get("alt"), seg.get("feeling"),
                                  seg.get("contrast")]
                                 for seg in (row["segments"] or [])]
        if strategy in ("mood", "feeling", "sentiment"):
            node["color"] = MOOD_COLORS.get(summary["dominant"], "#94a3b8")


# Which fact each strategy's neighbour search stands for. "Links from" on the
# page can pick several; each link then still gets every fact scored.
STRATEGY_FACT = {
    "structural": "chords", "chords": "chords", "chord_progression": "chords",
    "acoustic": "sound",
    "context": "lyrics", "lyrics": "lyrics", "meaning": "lyrics",
    "mood": "mood", "feeling": "mood", "sentiment": "mood",
    "harmonic": "key",
}

FACTS = ("chords", "sound", "lyrics", "mood", "key", "tempo", "genre")

# A vector fact holds when two songs are closer than 90% of random library
# pairs. Measured 2026-10-24 on 1,500 random tracks (4,000 pairs per space);
# each space's own top-8 neighbours sit well above these (p10: chords 0.967,
# sound 0.900, lyrics 0.471).
FACT_THRESHOLDS = {"chords": 0.915, "sound": 0.845, "lyrics": 0.356}

# Two tempos agree within 6%, counting half and double time as the same pulse.
TEMPO_TOLERANCE = 0.06


def parse_strategies(strategy: str) -> list[str]:
    """Split 'structural,acoustic' into known strategies, keeping order."""
    out: list[str] = []
    for part in (strategy or "").split(","):
        part = part.strip().lower()
        if part in STRATEGY_FACT and part not in out:
            out.append(part)
    return out or ["structural"]


def parse_facts(raw: Any) -> list[str]:
    """Facts named in a comma list (or list), unknown names dropped."""
    parts = raw.split(",") if isinstance(raw, str) else list(raw or [])
    return [p.strip().lower() for p in parts if p and p.strip().lower() in FACTS]


def _neighbours(curr_id: int, strategy: str, k: int) -> list[dict[str, Any]]:
    """Nearest neighbours of one track in one strategy's space."""
    if strategy in ("structural", "chords", "chord_progression"):
        from . import search
        return search.similar_chords_for_track(curr_id, k=k)
    if strategy in ("context", "lyrics", "meaning"):
        from . import search
        return search.similar_lyrics_for_track(curr_id, k=k)
    if strategy in ("mood", "feeling", "sentiment"):
        from . import search
        return search.similar_mood_for_track(curr_id, k=k)
    if strategy == "acoustic":
        from . import queue_suggest
        raw_hits, space = queue_suggest._seed_neighbours(curr_id, k=k)
        return [
            {"track_id": h.track_id, "score": round(h.similarity, 3), "space": space}
            for h in raw_hits
        ]
    # harmonic: Camelot adjacent tracks
    neighbors: list[dict[str, Any]] = []
    try:
        with localcache.connect() as conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT a.detected_key, a.bpm FROM track_analysis a
                WHERE a.track_id = %s
            """, (curr_id,))
            arow = cur.fetchone()
            if arow and arow["detected_key"]:
                k_obj = musictheory.parse_key(arow["detected_key"])
                if k_obj and k_obj.camelot and k_obj.camelot != "?":
                    compat = _camelot_compatible(k_obj.camelot)
                    cur.execute("""
                        SELECT t.track_id, a.detected_key, a.bpm
                        FROM tracks t
                        JOIN track_analysis a ON a.track_id = t.track_id
                        WHERE t.track_id != %s AND a.detected_key IS NOT NULL
                        ORDER BY t.play_count DESC, t.track_id ASC
                        LIMIT 60
                    """, (curr_id,))
                    for r in cur.fetchall():
                        cand_key = r.get("detected_key")
                        if not cand_key:
                            continue
                        rk = musictheory.parse_key(cand_key)
                        if rk and rk.camelot in compat:
                            neighbors.append({
                                "track_id": r["track_id"],
                                "score": 0.9,
                                "space": "camelot",
                            })
                            if len(neighbors) >= k:
                                break
    except Exception:
        pass
    return neighbors


def _camelot_compatible(cam: str) -> dict[str, str]:
    """Camelot codes that mix with `cam`, mapped to how they relate."""
    num, let = int(cam[:-1]), cam[-1]
    other = "B" if let == "A" else "A"
    return {
        cam: "same key",
        f"{num}{other}": "relative key",
        f"{(num % 12) + 1}{let}": "adjacent key",
        f"{((num - 2) % 12) + 1}{let}": "adjacent key",
    }


def _load_nodes(ids: list[int], seed_ids: list[int], strategy: str) -> dict[int, dict[str, Any]]:
    """Node records for `ids`: key, tempo, genres, mood and chord form."""
    nodes: dict[int, dict[str, Any]] = {}
    if not ids:
        return nodes
    with localcache.connect() as conn:
        has_artist_genres = localcache.table_exists(conn, "artist_genres")
        # The artist's genres, as the TUI's H filter and search_songs use them.
        artist_genres_sql = """
            , (SELECT array_agg(DISTINCT lower(trim(x))) FROM artist_genres ag,
                      LATERAL (VALUES (ag.genre), (ag.broad_genre)) v(x)
               WHERE ag.artist_normalized = lower(trim(t.artist))
                 AND COALESCE(trim(x), '') <> '') AS artist_genres
        """ if has_artist_genres else ""
        cur = conn.cursor()
        cur.execute(f"""
            SELECT t.track_id, t.artist, t.title, a.detected_key, a.bpm, a.energy, g.genre,
                   t.play_count {artist_genres_sql}
            FROM tracks t
            LEFT JOIN track_analysis a ON a.track_id = t.track_id
            LEFT JOIN track_genre g ON g.track_id = t.track_id
            WHERE t.track_id = ANY(%s)
        """, (ids,))
        rows = cur.fetchall()

    for r in rows:
        tid = r["track_id"]
        k_str = r["detected_key"] or ""
        k_obj = musictheory.parse_key(k_str) if k_str else None
        cam = k_obj.camelot if k_obj else "?"
        color = CAMELOT_COLORS.get(cam.upper(), "#6c757d")
        nodes[tid] = {
            "id": str(tid),
            "track_id": tid,
            "label": f"{r['artist']} — {r['title']}",
            "artist": r["artist"],
            "title": r["title"],
            "key": k_str,
            "camelot": cam,
            "color": color,
            "key_color": color,
            "bpm": round(r["bpm"], 1) if r["bpm"] else None,
            "energy": round(r["energy"], 2) if r["energy"] is not None else None,
            "genre": r["genre"] or "",
            "artist_genres": sorted(r.get("artist_genres") or []),
            "play_count": r["play_count"] or 0,
            "is_seed": tid in seed_ids,
        }

    # Batch enrich with OpenSearch progression, chord, and mood data
    try:
        from .key_progression import PROGRESSION_INDEX, doc_id
        from .osclient import client as get_os_client
        osc = get_os_client()
        if osc:
            if osc.indices.exists(index=PROGRESSION_INDEX):
                prog_ids = [doc_id(tid) for tid in ids if tid in nodes]
                if prog_ids:
                    try:
                        m_res = osc.mget(index=PROGRESSION_INDEX, body={"ids": prog_ids})
                        for doc in m_res.get("docs", []):
                            if doc.get("found"):
                                src = doc["_source"]
                                tid = src.get("track_id")
                                if tid and tid in nodes:
                                    nodes[tid]["form"] = src.get("form", "unknown")
                                    nodes[tid]["fifth_ratio"] = round(src["fifth_ratio"], 3) if src.get("fifth_ratio") is not None else None
                                    nodes[tid]["distinct_chords"] = src.get("distinct_chords")
                    except Exception as exc:
                        log.debug("Batch mget progression error: %s", exc)

            if osc.indices.exists(index="tracks"):
                track_doc_ids = [f"sqlite:{tid}" for tid in ids]
                if track_doc_ids:
                    try:
                        m_res = osc.mget(index="tracks", body={"ids": track_doc_ids})
                        for doc in m_res.get("docs", []):
                            if doc.get("found"):
                                tsrc = doc["_source"]
                                tid = tsrc.get("track_id")
                                if tid and tid in nodes:
                                    dm = tsrc.get("dominant_mood") or "neutral"
                                    nodes[tid]["dominant_mood"] = dm
                                    nodes[tid]["sentiment_hits"] = tsrc.get("sentiment_hits", 0)
                                    if strategy in ("mood", "feeling", "sentiment"):
                                        nodes[tid]["color"] = MOOD_COLORS.get(dm.lower(), "#94a3b8")
                    except Exception as exc:
                        log.debug("Batch mget tracks error: %s", exc)
    except Exception as exc:
        log.warning("OpenSearch node enrichment error: %s", exc)

    _add_mood_timelines(nodes, strategy)
    return nodes


class _Vectors:
    """Per-request cache of the chord, CLAP and lyric vectors of graph nodes."""

    def __init__(self) -> None:
        self._have: dict[str, dict[int, Any]] = {f: {} for f in FACT_THRESHOLDS}
        self._asked: set[int] = set()
        self._osc: Any = None  # opensearch client, or False once found unavailable
        self._spaces: Optional[dict[str, tuple[str, str, Any]]] = None

    def _client(self) -> Any:
        if self._osc is None:
            try:
                from . import clap_vector, key_progression
                from .osclient import client as get_os_client
                self._osc = get_os_client() or False
                self._spaces = {
                    "chords": (key_progression.PROGRESSION_INDEX, "chord_motion",
                               key_progression.doc_id),
                    "sound": (clap_vector.CLAP_INDEX, "clap_vector", clap_vector.doc_id),
                    "lyrics": ("tracks", "lyrics_vector", lambda tid: f"sqlite:{tid}"),
                }
            except Exception as exc:
                log.debug("graph: no vector client: %s", exc)
                self._osc = False
        return self._osc

    def load(self, ids: list[int]) -> None:
        todo = [tid for tid in ids if tid not in self._asked]
        if not todo or not self._client():
            return
        self._asked.update(todo)
        import numpy as np
        for fact, (index, field, make_id) in (self._spaces or {}).items():
            try:
                res = self._osc.mget(index=index, body={"ids": [make_id(t) for t in todo]},
                                     _source=["track_id", field])
            except Exception as exc:
                log.debug("graph: %s vectors unavailable: %s", fact, exc)
                continue
            for doc in res.get("docs", []):
                src = doc.get("_source") or {}
                vec = src.get(field)
                if doc.get("found") and vec and src.get("track_id") is not None:
                    arr = np.asarray(vec, dtype=float)
                    norm = float(np.linalg.norm(arr))
                    if norm > 0:
                        self._have[fact][int(src["track_id"])] = arr / norm

    def cosine(self, fact: str, a: int, b: int) -> Optional[float]:
        va, vb = self._have[fact].get(a), self._have[fact].get(b)
        if va is None or vb is None:
            return None
        return round(float(va @ vb), 3)


def _genre_set(node: dict[str, Any]) -> set[str]:
    """The artist's genres when classified, the track's CLAP genre otherwise."""
    if node.get("artist_genres"):
        return set(node["artist_genres"])
    g = (node.get("genre") or "").strip().lower()
    return {g} if g else set()


def edge_facts(a: dict[str, Any], b: dict[str, Any], vectors: Optional[_Vectors]) -> dict[str, Any]:
    """Every fact two songs share or not; None where either side lacks the data.

    Vector facts carry their cosine, the rest a bool or a short description.
    """
    facts: dict[str, Any] = {}
    for fact in FACT_THRESHOLDS:
        facts[fact] = vectors.cosine(fact, a["track_id"], b["track_id"]) if vectors else None

    ma, mb = a.get("dominant_mood"), b.get("dominant_mood")
    facts["mood"] = (ma == mb and ma != "neutral") if ma and mb else None

    ca, cb = a.get("camelot") or "?", b.get("camelot") or "?"
    if ca == "?" or cb == "?":
        facts["key"] = None
    else:
        facts["key"] = _camelot_compatible(ca).get(cb, False)

    ba, bb = a.get("bpm"), b.get("bpm")
    if ba and bb:
        facts["tempo"] = any(abs(bb * f / ba - 1.0) <= TEMPO_TOLERANCE for f in (1.0, 0.5, 2.0))
    else:
        facts["tempo"] = None

    ga, gb = _genre_set(a), _genre_set(b)
    shared = sorted(ga & gb)
    facts["genre"] = (shared[0] if shared else False) if ga and gb else None
    return facts


def agreeing(facts: dict[str, Any]) -> list[str]:
    """The facts that hold, in FACTS order."""
    out = []
    for fact in FACTS:
        val = facts.get(fact)
        if fact in FACT_THRESHOLDS:
            if val is not None and val >= FACT_THRESHOLDS[fact]:
                out.append(fact)
        elif val:
            out.append(fact)
    return out


def _node_passes(node: dict[str, Any], bpm_min: Optional[float], bpm_max: Optional[float],
                 genre_filter: Optional[str], camelot_filter: Optional[str],
                 mood_filter: Optional[str]) -> bool:
    bpm = node.get("bpm")
    if bpm_min is not None and bpm is not None and bpm < bpm_min:
        return False
    if bpm_max is not None and bpm is not None and bpm > bpm_max:
        return False
    if genre_filter and genre_filter.strip().lower() not in _genre_set(node):
        return False
    if camelot_filter and camelot_filter.upper() != (node.get("camelot") or "").upper():
        return False
    if mood_filter and mood_filter.lower() != (node.get("dominant_mood") or "").lower():
        return False
    return True


def build_similarity_graph(
    track_id: Optional[int] = None,
    playlist_id: Optional[str] = None,
    strategy: str = "structural",
    hops: int = 2,
    limit_per_node: int = 4,
    min_score: Optional[float] = None,
    bpm_min: Optional[float] = None,
    bpm_max: Optional[float] = None,
    genre_filter: Optional[str] = None,
    camelot_filter: Optional[str] = None,
    mood_filter: Optional[str] = None,
    require: Any = None,
    min_facts: int = 0,
) -> dict[str, Any]:
    """Build a node-link similarity graph centered on a track or playlist.

    `strategy` may name several spaces ('structural,acoustic'); links come
    from each. Every link is scored on all FACTS, and `require` (facts that
    must hold) and `min_facts` (how many must) decide which links the walk
    follows. Node filters apply during the walk too, so a filtered graph
    keeps growing through songs that pass instead of ending up empty.
    """
    strategies = parse_strategies(strategy)
    strategy_label = ",".join(strategies)
    primary = strategies[0]
    required = parse_facts(require)
    min_facts = max(0, min(int(min_facts or 0), len(FACTS)))
    hops = max(1, min(hops, 3))
    limit_per_node = max(1, min(limit_per_node, 8))
    seed_ids: list[int] = []

    with localcache.connect() as conn:
        if playlist_id:
            try:
                pl_tracks = localcache.get_saved_playlist_tracks(playlist_id, conn=conn)
                if not pl_tracks and playlist_id in ("dj-list", "Karaoke: DJ List"):
                    pl_info = localcache.find_saved_playlist_by_id("dj-list", conn=conn)
                    if pl_info and pl_info.get("playlist_id") and pl_info["playlist_id"] != "dj-list":
                        pl_tracks = localcache.get_saved_playlist_tracks(pl_info["playlist_id"], conn=conn)
                seed_ids = [t["track_id"] for t in pl_tracks if t.get("track_id")]
            except Exception as exc:
                log.warning("Could not read playlist %s: %s", playlist_id, exc)

        if not seed_ids and track_id is not None:
            seed_ids = [track_id]

        if not seed_ids:
            # Check if a track is actively playing on desktop player (Chromium, Spotify, MPRIS)
            try:
                from . import playerctl
                import re
                active_player = playerctl.playing_player()
                meta = playerctl.current_metadata(active_player) if active_player else playerctl.current_metadata()
                if meta and (meta.artist or meta.title):
                    found_tid = None
                    if meta.title and any(sep in meta.title for sep in (" - ", " – ", " — ")):
                        parts = re.split(r"\s*[-–—]\s*", meta.title, maxsplit=1)
                        if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                            found_tid = localcache.find_track_id_relaxed(parts[0].strip(), parts[1].strip(), conn)
                    if not found_tid:
                        found_tid = localcache.find_track_id_relaxed(meta.artist, meta.title, conn)
                    if not found_tid and meta.url:
                        u_res = localcache.find_track_by_url(meta.url, conn)
                        if u_res:
                            found_tid = u_res[0]
                    if found_tid:
                        seed_ids = [found_tid]
            except Exception as exc:
                log.debug("No active player seed found: %s", exc)

        if not seed_ids:
            cur = conn.cursor()
            params: list[Any] = []
            where_clauses = ["a.bpm IS NOT NULL"]
            if genre_filter:
                where_clauses.append("lower(trim(COALESCE(g.genre, ''))) = lower(trim(%s))")
                params.append(genre_filter)
            where_sql = " AND ".join(where_clauses)
            cur.execute(f"""
                SELECT t.track_id FROM tracks t
                JOIN track_analysis a ON a.track_id = t.track_id
                LEFT JOIN track_genre g ON g.track_id = t.track_id
                WHERE {where_sql}
                ORDER BY t.play_count DESC, t.track_id ASC
                LIMIT 1
            """, tuple(params))
            row = cur.fetchone()
            if row:
                seed_ids = [row["track_id"]]
            else:
                seed_ids = [14082]

    # For large playlists (e.g. 600+ sync-list), clamp seeds and hops to prevent UI freeze
    MAX_SEEDS = 35
    if len(seed_ids) > MAX_SEEDS:
        seed_ids = seed_ids[:MAX_SEEDS]

    if len(seed_ids) > 10 and hops > 1:
        hops = 1

    # Scoring every fact needs the vectors; without filters on them a plain
    # one-space graph still gets them, for the "why linked" panel.
    vectors = _Vectors()
    nodes: dict[int, dict[str, Any]] = _load_nodes(list(seed_ids), seed_ids, primary)
    vectors.load(list(seed_ids))
    filtering = bool(required or min_facts or bpm_min is not None or bpm_max is not None
                     or genre_filter or camelot_filter or mood_filter)
    # Over-fetch when links may be refused, so a filtered song still gets
    # `limit_per_node` links per space where they exist.
    fetch_k = 40 if filtering else limit_per_node

    MAX_TOTAL_GRAPH_NODES = 80
    visited: set[int] = set()
    queue: list[tuple[int, int]] = [(sid, 0) for sid in seed_ids]
    edges: dict[tuple[int, int], dict[str, Any]] = {}
    # Songs on the graph so far: several spaces each add `limit_per_node`
    # links per song, so the cap must hold on what is drawn, not only on
    # what is expanded.
    linked: set[int] = set(seed_ids)

    while queue and len(visited) < MAX_TOTAL_GRAPH_NODES:
        curr_id, depth = queue.pop(0)
        if curr_id in visited or depth >= hops or curr_id not in nodes:
            continue
        visited.add(curr_id)

        found: list[tuple[str, dict[str, Any]]] = []
        for strat in strategies:
            try:
                found += [(strat, n) for n in _neighbours(curr_id, strat, fetch_k)]
            except Exception as exc:
                log.debug("graph: %s neighbours of %s failed: %s", strat, curr_id, exc)
        new_ids = list({int(n["track_id"]) for _, n in found
                        if n.get("track_id") and int(n["track_id"]) not in nodes})
        nodes.update(_load_nodes(new_ids, seed_ids, primary))
        vectors.load([curr_id] + new_ids)

        taken: dict[str, int] = {}
        for strat, nbr in found:
            tid = int(nbr.get("track_id") or 0)
            if not tid or tid == curr_id or tid not in nodes or taken.get(strat, 0) >= limit_per_node:
                continue
            if tid not in linked and len(linked) >= MAX_TOTAL_GRAPH_NODES:
                continue
            score = float(nbr.get("score", 0.8))
            if min_score is not None and score < min_score:
                continue
            target = nodes[tid]
            if tid not in seed_ids and not _node_passes(
                    target, bpm_min, bpm_max, genre_filter, camelot_filter, mood_filter):
                continue
            facts = edge_facts(nodes[curr_id], target, vectors)
            agree = agreeing(facts)
            if any(f not in agree for f in required) or len(agree) < min_facts:
                continue
            taken[strat] = taken.get(strat, 0) + 1
            linked.add(tid)
            edge_key = (min(curr_id, tid), max(curr_id, tid))
            edge = edges.get(edge_key)
            if edge is None:
                edges[edge_key] = {
                    "source": curr_id,
                    "target": tid,
                    "weight": score,
                    "relation": nbr.get("space", strat),
                    "found_by": [STRATEGY_FACT[strat]],
                    "facts": facts,
                    "agree": agree,
                }
            elif STRATEGY_FACT[strat] not in edge["found_by"]:
                edge["found_by"].append(STRATEGY_FACT[strat])
                edge["weight"] = max(edge["weight"], score)
            if tid not in visited and depth + 1 < hops and len(visited) + len(queue) < MAX_TOTAL_GRAPH_NODES:
                queue.append((tid, depth + 1))

    graph_nodes = {tid: n for tid, n in nodes.items() if tid in linked}
    for tid, node in graph_nodes.items():
        node["degree"] = sum(1 for e in edges.values() if tid in (e["source"], e["target"]))

    formatted_edges = [
        {
            "id": f"e_{e['source']}_{e['target']}",
            "source": str(e["source"]),
            "target": str(e["target"]),
            "weight": e["weight"],
            "relation": e["relation"],
            "found_by": e["found_by"],
            "facts": e["facts"],
            "agree": e["agree"],
            "strength": len(e["agree"]),
        }
        for e in edges.values()
    ]

    return {
        "nodes": list(graph_nodes.values()),
        "edges": formatted_edges,
        "stats": {
            "node_count": len(graph_nodes),
            "edge_count": len(formatted_edges),
            "strategy": strategy_label,
            "seed_ids": seed_ids,
            "hops": hops,
            "min_score": min_score,
            "bpm_min": bpm_min,
            "bpm_max": bpm_max,
            "genre_filter": genre_filter,
            "camelot_filter": camelot_filter,
            "mood_filter": mood_filter,
            "require": required,
            "min_facts": min_facts,
            "fact_thresholds": FACT_THRESHOLDS,
        },
        "graph_url": f"http://localhost:8888/graph?{'playlist_id=' + playlist_id if playlist_id else 'track_id=' + str(seed_ids[0])}&strategy={strategy_label}",
    }


GRAPH_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Karaoke Similarity Constellation</title>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/cytoscape/3.30.2/cytoscape.min.js"></script>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: #0f111a;
      color: #e2e8f0;
      display: flex;
      flex-direction: column;
      height: 100vh;
      overflow: hidden;
    }
    header {
      background: #181b29;
      border-bottom: 1px solid #2d3748;
      padding: 10px 18px;
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .header-top {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      flex-wrap: wrap;
    }
    .brand {
      font-size: 1.15rem;
      font-weight: 700;
      color: #38bdf8;
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .controls {
      display: flex;
      align-items: center;
      gap: 8px;
      flex-wrap: wrap;
    }
    .filter-bar {
      display: flex;
      align-items: center;
      gap: 12px;
      flex-wrap: wrap;
      background: #131622;
      padding: 8px 12px;
      border-radius: 6px;
      border: 1px solid #282f44;
      font-size: 0.8rem;
    }
    .filter-item {
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .filter-label {
      color: #94a3b8;
      font-weight: 500;
      white-space: nowrap;
    }
    .filter-val {
      color: #38bdf8;
      font-family: monospace;
      font-weight: 600;
      min-width: 30px;
    }
    input, select, button {
      background: #23283b;
      color: #f1f5f9;
      border: 1px solid #3b4261;
      padding: 5px 10px;
      border-radius: 5px;
      font-size: 0.82rem;
      outline: none;
    }
    input[type="range"] {
      padding: 0;
      cursor: pointer;
      accent-color: #38bdf8;
    }
    input:focus, select:focus {
      border-color: #38bdf8;
    }
    button {
      background: #2563eb;
      border-color: #1d4ed8;
      cursor: pointer;
      font-weight: 600;
      transition: background 0.15s;
    }
    button:hover { background: #1d4ed8; }
    .btn-secondary {
      background: #334155;
      border-color: #475569;
    }
    .btn-secondary:hover { background: #475569; }
    .btn-secondary.active { background: #0e7490; box-shadow: 0 0 0 1px #38bdf8 inset; }
    .btn-apply {
      background: #0284c7;
      border-color: #0369a1;
      color: #fff;
    }
    .btn-apply:hover { background: #0369a1; }
    .stats-badge {
      margin-left: auto;
      background: #1e293b;
      border: 1px solid #334155;
      color: #38bdf8;
      padding: 3px 8px;
      border-radius: 12px;
      font-size: 0.75rem;
      font-weight: 600;
      white-space: nowrap;
    }
    .main {
      flex: 1;
      display: flex;
      position: relative;
    }
    #cy {
      flex: 1;
      height: 100%;
      background: radial-gradient(circle at center, #141724 0%, #0a0c13 100%);
    }
    #sidebar {
      width: 320px;
      background: #161926;
      border-left: 1px solid #2d3748;
      padding: 20px;
      display: flex;
      flex-direction: column;
      gap: 16px;
      overflow-y: auto;
      transition: transform 0.2s ease;
    }
    .card {
      background: #1e2235;
      border: 1px solid #2e3650;
      border-radius: 8px;
      padding: 14px;
    }
    .card h3 {
      font-size: 1.05rem;
      color: #f8fafc;
      margin-bottom: 4px;
    }
    .card .artist {
      color: #94a3b8;
      font-size: 0.9rem;
      margin-bottom: 12px;
    }
    .collector-drop {
      min-height: 46px;
      margin: 8px 0;
      padding: 6px;
      border: 2px dashed #334155;
      border-radius: 6px;
      transition: border-color 0.15s, background 0.15s;
    }
    .collector-drop.over { border-color: #38bdf8; background: rgba(56, 189, 248, 0.08); }
    /* Shown over the graph only while a node is dragged: the side-pane box can
       be below the fold on a short screen, and a drop target must be visible. */
    #collector-float {
      display: none;
      position: fixed;
      left: 24px;
      bottom: 64px;
      z-index: 50;
      padding: 14px 22px;
      border: 2px dashed #38bdf8;
      border-radius: 10px;
      background: rgba(10, 12, 19, 0.88);
      color: #e2e8f0;
      font-size: 0.9rem;
      pointer-events: none;
    }
    #collector-float.over { background: rgba(56, 189, 248, 0.25); }
    .collector-hint { color: #64748b; font-size: 0.75rem; text-align: center; padding: 8px 0; }
    .collector-item {
      display: flex; align-items: center; gap: 6px;
      padding: 4px 6px; margin: 2px 0;
      background: #141724; border: 1px solid #262c42; border-radius: 4px;
      font-size: 0.78rem; cursor: grab;
    }
    .collector-item.dragging { opacity: 0.4; }
    .collector-item .num { color: #64748b; width: 1.4em; text-align: right; }
    .collector-item .what { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .collector-item .rm { background: none; border: none; color: #94a3b8; cursor: pointer; padding: 0 2px; }
    .collector-actions { display: flex; flex-wrap: wrap; gap: 6px; }
    .collector-actions input {
      flex: 1 1 100%; padding: 5px 8px; font-size: 0.8rem;
      background: #141724; color: #f8fafc; border: 1px solid #334155; border-radius: 4px;
    }
    .collector-status { font-size: 0.75rem; color: #94a3b8; margin-top: 6px; min-height: 1em; }
    .mood-arc { margin-top: 10px; }
    .chips { display: flex; gap: 4px; flex-wrap: wrap; }
    .chip {
      background: #23283b; color: #94a3b8; border: 1px solid #3b4261;
      padding: 3px 8px; border-radius: 999px; font-size: 0.75rem; font-weight: 500;
    }
    .chip:hover { background: #2d3450; }
    .chip[aria-pressed="true"] { background: #0e7490; color: #f8fafc; border-color: #38bdf8; }
    .why-list { list-style: none; margin-top: 8px; font-size: 0.8rem; }
    .why-list li { display: flex; justify-content: space-between; gap: 8px; padding: 3px 0;
                   border-bottom: 1px solid #1e293b; color: #64748b; }
    .why-list li.yes { color: #e2e8f0; }
    .why-list li.yes .why-name::before { content: "✓ "; color: #34d399; }
    .why-list li.no .why-name::before { content: "· "; }
    .why-val { font-family: monospace; }
    .mood-strip {
      display: flex;
      height: 14px;
      margin: 5px 0 4px;
      border-radius: 4px;
      overflow: hidden;
      border: 1px solid #262c42;
    }
    .mood-strip > div { height: 100%; }
    .mood-arc-text { font-size: 0.75rem; color: #94a3b8; line-height: 1.4; }
    .meta-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      font-size: 0.8rem;
    }
    .meta-item {
      background: #141724;
      padding: 6px 8px;
      border-radius: 4px;
      border: 1px solid #262c42;
    }
    .meta-label {
      color: #64748b;
      font-size: 0.7rem;
      text-transform: uppercase;
      margin-bottom: 2px;
    }
    .meta-val {
      font-weight: 600;
      color: #e2e8f0;
    }
    .badge {
      display: inline-block;
      padding: 3px 8px;
      border-radius: 12px;
      font-size: 0.75rem;
      font-weight: 600;
    }
    .actions {
      display: flex;
      flex-direction: column;
      gap: 8px;
      margin-top: 10px;
    }
    .legend {
      font-size: 0.75rem;
      color: #64748b;
      line-height: 1.4;
    }
    #status-toast {
      position: absolute;
      bottom: 16px;
      left: 16px;
      background: rgba(15, 23, 42, 0.9);
      border: 1px solid #334155;
      padding: 8px 14px;
      border-radius: 6px;
      font-size: 0.8rem;
      pointer-events: none;
      transition: opacity 0.3s;
    }
  </style>
</head>
<body>
  <header>
    <div class="header-top">
      <div class="brand">
        <span>✨ Karaoke Similarity Constellation</span>
      </div>
      <div class="controls">
        <input type="text" id="seed-input" placeholder="Track ID or Artist / Title..." style="width: 200px;">
        <span class="filter-label" title="Which similarity searches draw the links. Pick several to combine them.">Links from:</span>
        <div class="chips" id="strategy-chips">
          <button type="button" class="chip" data-strategy="structural" aria-pressed="true" title="Chord motion">Chords</button>
          <button type="button" class="chip" data-strategy="acoustic" aria-pressed="false" title="CLAP timbre">Sound</button>
          <button type="button" class="chip" data-strategy="harmonic" aria-pressed="false" title="Camelot key">Key</button>
          <button type="button" class="chip" data-strategy="context" aria-pressed="false" title="Lyrics and meaning">Lyrics</button>
          <button type="button" class="chip" data-strategy="mood" aria-pressed="false" title="Emotion and sentiment">Mood</button>
        </div>
        <select id="playlist-select" onchange="onPlaylistSelected()">
          <option value="">Load Playlist...</option>
        </select>
        <button onclick="loadGraph()">Explore</button>
        <button class="btn-secondary" onclick="loadNowPlaying()">Now Playing</button>
        <button class="btn-secondary" id="songrec-btn" onclick="findWithSongrec()"
                title="Identify what is audible (radio mode, the output, or the mic) and locate it">🎙 Find with songrec</button>
        <button class="btn-secondary" id="follow-btn" onclick="toggleFollow()" aria-pressed="false"
                title="Re-center on each new song from radio mode or the desktop player">📡 Follow</button>
      </div>
    </div>
    <div class="filter-bar">
      <div class="filter-item">
        <span class="filter-label">Min Sim:</span>
        <input type="range" id="min-score" min="0.0" max="1.0" step="0.05" value="0.50" oninput="document.getElementById('min-score-val').textContent = parseFloat(this.value).toFixed(2)">
        <span id="min-score-val" class="filter-val">0.50</span>
      </div>
      <div class="filter-item">
        <span class="filter-label">BPM:</span>
        <input type="number" id="bpm-min" placeholder="Min" style="width: 60px;">
        <span style="color:#64748b">-</span>
        <input type="number" id="bpm-max" placeholder="Max" style="width: 60px;">
      </div>
      <div class="filter-item">
        <span class="filter-label">Camelot:</span>
        <select id="camelot-select">
          <option value="">All Keys</option>
          <option value="1A">1A (Abm)</option><option value="1B">1B (B)</option>
          <option value="2A">2A (Ebm)</option><option value="2B">2B (F#)</option>
          <option value="3A">3A (Bbm)</option><option value="3B">3B (Db)</option>
          <option value="4A">4A (Fm)</option><option value="4B">4B (Ab)</option>
          <option value="5A">5A (Cm)</option><option value="5B">5B (Eb)</option>
          <option value="6A">6A (Gm)</option><option value="6B">6B (Bb)</option>
          <option value="7A">7A (Dm)</option><option value="7B">7B (F)</option>
          <option value="8A">8A (Am)</option><option value="8B">8B (C)</option>
          <option value="9A">9A (Em)</option><option value="9B">9B (G)</option>
          <option value="10A">10A (Bm)</option><option value="10B">10B (D)</option>
          <option value="11A">11A (F#m)</option><option value="11B">11B (A)</option>
          <option value="12A">12A (C#m)</option><option value="12B">12B (E)</option>
        </select>
      </div>
      <div class="filter-item">
        <span class="filter-label">Genre:</span>
        <select id="genre-select">
          <option value="">All Genres</option>
          <option value="blues">Blues</option>
          <option value="jazz">Jazz</option>
          <option value="rock">Rock</option>
          <option value="metal">Metal</option>
          <option value="punk">Punk</option>
          <option value="folk">Folk</option>
          <option value="country">Country</option>
          <option value="soul">Soul</option>
          <option value="funk">Funk</option>
          <option value="hip hop">Hip Hop</option>
          <option value="reggae">Reggae</option>
          <option value="electronic">Electronic</option>
          <option value="ambient">Ambient</option>
          <option value="pop">Pop</option>
          <option value="latin">Latin</option>
          <option value="world">World</option>
          <option value="classical">Classical</option>
        </select>
      </div>
      <div class="filter-item">
        <span class="filter-label">Mood:</span>
        <select id="mood-select">
          <option value="">All Moods</option>
          <option value="happy">Happy</option>
          <option value="tender">Tender</option>
          <option value="sad">Sad</option>
          <option value="angry">Angry</option>
          <option value="neutral">Neutral</option>
        </select>
      </div>
      <div class="filter-item">
        <span class="filter-label">Hops:</span>
        <select id="hops-select">
          <option value="1">1</option>
          <option value="2" selected>2</option>
          <option value="3">3</option>
        </select>
      </div>
      <div class="filter-item">
        <span class="filter-label">Limit:</span>
        <select id="limit-select">
          <option value="2">2</option>
          <option value="4" selected>4</option>
          <option value="6">6</option>
          <option value="8">8</option>
        </select>
      </div>
      <div class="filter-item">
        <span class="filter-label" title="Only follow links where these facts hold between the two songs">Must share:</span>
        <div class="chips" id="require-chips">
          <button type="button" class="chip" data-fact="chords" aria-pressed="false">Chords</button>
          <button type="button" class="chip" data-fact="sound" aria-pressed="false">Sound</button>
          <button type="button" class="chip" data-fact="lyrics" aria-pressed="false">Lyrics</button>
          <button type="button" class="chip" data-fact="mood" aria-pressed="false">Mood</button>
          <button type="button" class="chip" data-fact="key" aria-pressed="false">Key</button>
          <button type="button" class="chip" data-fact="tempo" aria-pressed="false">Tempo</button>
          <button type="button" class="chip" data-fact="genre" aria-pressed="false">Genre</button>
        </div>
      </div>
      <div class="filter-item">
        <span class="filter-label" title="Only follow links where at least this many facts hold">Min facts:</span>
        <select id="min-facts-select">
          <option value="0" selected>any</option>
          <option value="2">2+</option>
          <option value="3">3+</option>
          <option value="4">4+</option>
        </select>
      </div>
      <div class="filter-item">
        <span class="filter-label">Color:</span>
        <select id="color-select" onchange="recolor()">
          <option value="key">Key</option>
          <option value="mood">Mood</option>
          <option value="genre">Genre</option>
          <option value="energy">Energy</option>
        </select>
      </div>
      <button onclick="loadGraph()" class="btn-apply">Filter</button>
      <button onclick="resetFilters()" class="btn-secondary">Reset</button>
      <span id="graph-stats-badge" class="stats-badge">0 nodes / 0 edges</span>
    </div>
  </header>

  <div class="main">
    <div id="cy"></div>
    <div id="collector-float">🧺 Drop to collect</div>
    <div id="sidebar">
      <div class="card" id="details-card">
        <h3 id="d-title">Select a Song Node</h3>
        <div class="artist" id="d-artist">Click any track in the graph to inspect harmony</div>
        <div class="meta-grid" id="d-meta" style="display: none;">
          <div class="meta-item">
            <div class="meta-label">Camelot Key</div>
            <div class="meta-val" id="d-camelot">-</div>
          </div>
          <div class="meta-item">
            <div class="meta-label">BPM</div>
            <div class="meta-val" id="d-bpm">-</div>
          </div>
          <div class="meta-item">
            <div class="meta-label">Form</div>
            <div class="meta-val" id="d-form">-</div>
          </div>
          <div class="meta-item">
            <div class="meta-label">Fifths Ratio</div>
            <div class="meta-val" id="d-fifths">-</div>
          </div>
          <div class="meta-item">
            <div class="meta-label">Mood / Feeling</div>
            <div class="meta-val" id="d-mood">-</div>
          </div>
          <div class="meta-item">
            <div class="meta-label">Sentiment Hits</div>
            <div class="meta-val" id="d-sentiment">-</div>
          </div>
          <div class="meta-item">
            <div class="meta-label">Genre</div>
            <div class="meta-val" id="d-genre">-</div>
          </div>
          <div class="meta-item">
            <div class="meta-label">Chords</div>
            <div class="meta-val" id="d-chords">-</div>
          </div>
        </div>
        <div class="mood-arc" id="d-moodarc" style="display: none;">
          <div class="meta-label">Mood over time</div>
          <div class="mood-strip" id="d-moodstrip"></div>
          <div class="mood-arc-text" id="d-moodarc-text"></div>
        </div>
        <div class="actions" id="d-actions" style="display: none;">
          <button onclick="playCurrentSelected()">▶ Play on Desktop</button>
          <button class="btn-secondary" onclick="recenterOnSelected()">🔍 Re-center Constellation</button>
          <button class="btn-secondary" onclick="collectSelected()">🧺 + Collect</button>
        </div>
      </div>
      <div class="card" id="why-card" style="display: none;">
        <strong>Why linked</strong>
        <div class="artist" id="why-pair"></div>
        <ul class="why-list" id="why-list"></ul>
      </div>
      <div class="card" id="collector-card">
        <strong>🧺 Collector <span id="collector-count">(0)</span></strong>
        <div class="collector-drop" id="collector-drop">
          <div class="collector-hint" id="collector-hint">Drag songs from the graph here, or use + Collect</div>
          <div id="collector-list"></div>
        </div>
        <div class="collector-actions">
          <input id="collector-name" placeholder="Playlist name (optional)" maxlength="80">
          <button onclick="saveCollection()">💾 Save playlist</button>
          <button onclick="playCollection()">▶ Play queue</button>
          <button class="btn-secondary" onclick="clearCollection()">Clear</button>
        </div>
        <div class="collector-status" id="collector-status"></div>
      </div>
      <div class="card">
        <div class="legend">
          <strong>Constellation Insights</strong><br>
          • <strong>Node Color:</strong> pick Key, Mood, Genre or Energy under Color.<br>
          • <strong>Links from:</strong> several searches at once; every link is then scored on all seven facts (chords, sound, lyrics, mood, key, tempo, genre).<br>
          • <strong>Edge Thickness &amp; Brightness:</strong> how many facts the two songs share. Click a link to see which.<br>
          • <strong>Must share / Min facts:</strong> the graph only grows through links that pass, so filtering reshapes it instead of emptying it.<br>
          • <strong>Seed Nodes:</strong> Highlighted with bright blue border.<br>
          • <strong>Dashed Ring:</strong> the song's mood swings (two or more changes); the inspector strip shows its mood over time, striped where borderline.<br>
          • <strong>Node filters:</strong> tempo, key, genre and mood apply while the graph grows.
        </div>
      </div>
    </div>
  </div>
  <div id="status-toast">Ready</div>

  <script>
    let cy;
    let selectedTrackId = null;

    function toast(msg) {
      const el = document.getElementById('status-toast');
      el.textContent = msg;
    }

    async function loadPlaylists() {
      try {
        const resp = await fetch('/api/playlists');
        if (resp.ok) {
          const list = await resp.json();
          const sel = document.getElementById('playlist-select');
          sel.innerHTML = '<option value="">Load Playlist...</option>';
          list.forEach(p => {
            const opt = document.createElement('option');
            opt.value = p.playlist_id;
            opt.textContent = `${p.name} (${p.track_count || 0})`;
            sel.appendChild(opt);
          });
        }
      } catch (err) {
        console.warn('Could not load playlists:', err);
      }
    }

    function onPlaylistSelected() {
      const plId = document.getElementById('playlist-select').value;
      if (plId) {
        document.getElementById('seed-input').value = '';
        loadGraph({ playlist_id: plId });
      }
    }

    function resetFilters() {
      document.getElementById('min-score').value = '0.50';
      document.getElementById('min-score-val').textContent = '0.50';
      document.getElementById('bpm-min').value = '';
      document.getElementById('bpm-max').value = '';
      document.getElementById('camelot-select').value = '';
      document.getElementById('genre-select').value = '';
      document.getElementById('mood-select').value = '';
      document.getElementById('hops-select').value = '2';
      document.getElementById('limit-select').value = '4';
      document.getElementById('min-facts-select').value = '0';
      document.querySelectorAll('#require-chips .chip').forEach(c => c.setAttribute('aria-pressed', 'false'));
      loadGraph();
    }

    async function loadGraph(customParams = {}) {
      toast('Loading similarity constellation...');
      const urlParams = new URLSearchParams(window.location.search);
      const trackInput = document.getElementById('seed-input').value.trim();
      const strategy = customParams.strategy || selectedStrategies().join(',');
      const hops = customParams.hops || document.getElementById('hops-select').value;
      const limit = customParams.limit || document.getElementById('limit-select').value;
      const minScore = document.getElementById('min-score').value;
      const bpmMin = document.getElementById('bpm-min').value;
      const bpmMax = document.getElementById('bpm-max').value;
      const camelot = document.getElementById('camelot-select').value;
      const genre = document.getElementById('genre-select').value;
      const mood = document.getElementById('mood-select').value;

      let fetchUrl = `/api/graph_data?strategy=${strategy}&hops=${hops}&limit=${limit}`;
      if (minScore) fetchUrl += `&min_score=${minScore}`;
      if (bpmMin) fetchUrl += `&bpm_min=${bpmMin}`;
      if (bpmMax) fetchUrl += `&bpm_max=${bpmMax}`;
      if (camelot) fetchUrl += `&camelot=${encodeURIComponent(camelot)}`;
      if (genre) fetchUrl += `&genre=${encodeURIComponent(genre)}`;
      if (mood) fetchUrl += `&mood=${encodeURIComponent(mood)}`;
      const require = pressed('#require-chips', 'fact');
      if (require.length) fetchUrl += `&require=${require.join(',')}`;
      const minFacts = document.getElementById('min-facts-select').value;
      if (minFacts !== '0') fetchUrl += `&min_facts=${minFacts}`;

      if (customParams.track_id) {
        document.getElementById('playlist-select').value = '';
        fetchUrl += `&track_id=${customParams.track_id}`;
      } else if (customParams.query) {
        document.getElementById('playlist-select').value = '';
        fetchUrl += `&query=${encodeURIComponent(customParams.query)}`;
      } else if (customParams.playlist_id) {
        document.getElementById('seed-input').value = '';
        fetchUrl += `&playlist_id=${encodeURIComponent(customParams.playlist_id)}`;
      } else if (trackInput) {
        document.getElementById('playlist-select').value = '';
        if (!isNaN(trackInput)) {
          fetchUrl += `&track_id=${trackInput}`;
        } else {
          fetchUrl += `&query=${encodeURIComponent(trackInput)}`;
        }
      } else if (document.getElementById('playlist-select').value) {
        fetchUrl += `&playlist_id=${encodeURIComponent(document.getElementById('playlist-select').value)}`;
      } else if (urlParams.get('track_id')) {
        fetchUrl += `&track_id=${urlParams.get('track_id')}`;
      } else if (urlParams.get('playlist_id')) {
        fetchUrl += `&playlist_id=${urlParams.get('playlist_id')}`;
      }

      try {
        const resp = await fetch(fetchUrl);
        const data = await resp.json();
        renderCytoscape(data);
        const badge = document.getElementById('graph-stats-badge');
        if (badge) badge.textContent = `${data.nodes.length} nodes / ${data.edges.length} edges`;
        toast(`Loaded ${data.nodes.length} nodes & ${data.edges.length} edges (${strategy})`);
      } catch (err) {
        console.error(err);
        toast('Error loading graph: ' + err.message);
      }
    }

    // Ask the server to place a song: it queues processing when the song is
    // missing or unanalysed, and the graph re-seeds once it is ready.
    async function locateAndSeed(artist, title, quiet) {
      const resp = await fetch('/api/locate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ artist, title }),
      });
      const loc = await resp.json();
      if (loc.track_id) seedGraph(loc.track_id);
      if (loc.status === 'queued') {
        toast(`${title}: ${loc.pending.length ? 'needs ' + loc.pending.join(', ') : 'adding to library'}; processing…`);
        waitUntilPlaceable(artist, title, loc.track_id);
      } else if (!quiet) {
        toast(`Now playing: ${artist} — ${title}`);
      }
      return loc;
    }

    async function loadNowPlaying() {
      toast('Checking current desktop playing track...');
      try {
        const resp = await fetch('/api/now_playing');
        const data = await resp.json();
        if (data.title) {
          await locateAndSeed(data.artist || '', data.title, false);
        } else if (data.track_id) {
          seedGraph(data.track_id);
        } else if (data.artist || data.title) {
          const q = (data.artist && data.title) ? `${data.artist} - ${data.title}` : (data.title || data.artist);
          document.getElementById('seed-input').value = q;
          document.getElementById('playlist-select').value = '';
          loadGraph({ query: q });
        } else {
          toast('No active track recognized in library');
        }
      } catch (e) {
        toast('Could not fetch now playing: ' + e.message);
      }
    }

    function seedGraph(trackId) {
      document.getElementById('seed-input').value = trackId;
      document.getElementById('playlist-select').value = '';
      history.replaceState(null, '', `?track_id=${trackId}`);
      loadGraph({ track_id: trackId });
    }

    let songrecPoll = null;

    // --- Follow mode: re-center on each new song ---------------------------
    const FOLLOW_EVERY_MS = 10000;
    // Hands off while the user explores: a re-seed would yank the graph away.
    const FOLLOW_QUIET_MS = 20000;
    let followTimer = null;
    let followedKey = null;
    let lastInteraction = 0;

    function setFollowButton(on) {
      const btn = document.getElementById('follow-btn');
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
      btn.classList.toggle('active', on);
      btn.textContent = on ? '📡 Following' : '📡 Follow';
    }

    async function followTick() {
      if (Date.now() - lastInteraction < FOLLOW_QUIET_MS) return;
      try {
        const data = await (await fetch('/api/now_playing')).json();
        if (data.status !== 'playing' || !data.title) return;
        const key = `${(data.artist || '').toLowerCase()}|${data.title.toLowerCase()}`;
        if (key === followedKey) return;
        followedKey = key;
        const via = data.player === 'radio' || data.player === 'tui-radio' ? ' (radio)' : '';
        toast(`Following${via}: ${data.artist} — ${data.title}`);
        await locateAndSeed(data.artist || '', data.title, true);
      } catch (e) { /* transient; next tick retries */ }
    }

    function toggleFollow(force) {
      const on = typeof force === 'boolean' ? force : !followTimer;
      if (followTimer) { clearInterval(followTimer); followTimer = null; }
      if (on) {
        followedKey = null;            // place the current song right away
        lastInteraction = 0;
        followTick();
        followTimer = setInterval(followTick, FOLLOW_EVERY_MS);
      }
      setFollowButton(on);
      try { localStorage.setItem('graph-follow', on ? '1' : '0'); } catch (e) {}
    }

    document.addEventListener('DOMContentLoaded', () => {
      let saved = null;
      try { saved = localStorage.getItem('graph-follow'); } catch (e) {}
      const param = new URLSearchParams(location.search).get('follow');
      if (param === '1' || (param === null && saved === '1')) toggleFollow(true);
    });

    async function findWithSongrec() {
      const btn = document.getElementById('songrec-btn');
      btn.disabled = true;
      btn.textContent = '🎙 Listening…';
      toast('Listening with songrec (up to ~20s)…');
      try {
        const resp = await fetch('/api/identify', { method: 'POST' });
        const data = await resp.json();
        const via = { radio: 'radio mode', 'tui-radio': 'radio mode', mic: 'the mic', output: 'the output' }[data.via] || data.via || '';
        const song = data.title ? `${data.artist} — ${data.title}` : '';
        if (data.status === 'found') {
          toast(`Found ${song} via ${via}`);
          seedGraph(data.track_id);
        } else if (data.status === 'queued') {
          toast(`${song} needs ${data.pending.length ? data.pending.join(', ') : 'adding'}; processing… it will be placed when ready`);
          if (data.track_id) seedGraph(data.track_id);   // show what exists meanwhile
          waitUntilPlaceable(data.artist, data.title, data.track_id);
        } else {
          toast(data.message || `No match (${data.status})`);
        }
      } catch (e) {
        toast('songrec failed: ' + e.message);
      } finally {
        btn.disabled = false;
        btn.textContent = '🎙 Find with songrec';
      }
    }

    // Post-processing downloads and analyses the song, which takes minutes.
    // Poll gently and re-seed once its chords and vectors exist.
    function waitUntilPlaceable(artist, title, knownId) {
      if (songrecPoll) clearInterval(songrecPoll);
      const started = Date.now();
      songrecPoll = setInterval(async () => {
        if (Date.now() - started > 15 * 60 * 1000) {
          clearInterval(songrecPoll);
          toast(`${title} is still processing; try Find again later`);
          return;
        }
        try {
          const q = new URLSearchParams({ artist, title });
          const st = await (await fetch(`/api/identify/status?${q}`)).json();
          if (st.ready) {
            clearInterval(songrecPoll);
            toast(`${title} is processed — placing it`);
            seedGraph(st.track_id);
          } else if (st.track_id && !knownId) {
            knownId = st.track_id;
            seedGraph(st.track_id);                     // added; analysis still running
          }
        } catch (e) { /* transient; keep polling */ }
      }, 10000);
    }

    // --- Collector: gather songs, save as playlist, play as queue -----------
    let collection = [];
    try { collection = JSON.parse(localStorage.getItem('graph-collection') || '[]'); } catch (e) {}
    let draggingNode = null, dragStartPos = null, pointer = {x: -1, y: -1}, listDragIdx = null;

    function persistCollection() {
      try { localStorage.setItem('graph-collection', JSON.stringify(collection)); } catch (e) {}
    }

    function setCollectorStatus(msg) { document.getElementById('collector-status').textContent = msg || ''; }

    function renderCollection() {
      const list = document.getElementById('collector-list');
      list.innerHTML = '';
      collection.forEach((t, i) => {
        const row = document.createElement('div');
        row.className = 'collector-item';
        row.draggable = true;
        row.innerHTML = `<span class="num">${i + 1}</span><span class="what"></span><button class="rm" title="Remove">✕</button>`;
        row.querySelector('.what').textContent = `${t.artist} — ${t.title}`;
        row.querySelector('.what').title = `${t.artist} — ${t.title}`;
        row.querySelector('.rm').onclick = () => { collection.splice(i, 1); persistCollection(); renderCollection(); };
        // Reorder by dragging within the list.
        row.addEventListener('dragstart', () => { listDragIdx = i; row.classList.add('dragging'); });
        row.addEventListener('dragend', () => { listDragIdx = null; row.classList.remove('dragging'); });
        row.addEventListener('dragover', e => e.preventDefault());
        row.addEventListener('drop', e => {
          e.preventDefault();
          if (listDragIdx === null || listDragIdx === i) return;
          const [moved] = collection.splice(listDragIdx, 1);
          collection.splice(i, 0, moved);
          persistCollection(); renderCollection();
        });
        list.appendChild(row);
      });
      document.getElementById('collector-count').textContent = `(${collection.length})`;
      document.getElementById('collector-hint').style.display = collection.length ? 'none' : 'block';
    }

    function collectTrack(t) {
      if (!t || t.track_id == null) return;
      if (collection.some(x => x.track_id === t.track_id)) {
        setCollectorStatus(`${t.title} is already collected`);
        return;
      }
      collection.push({track_id: t.track_id, artist: t.artist, title: t.title});
      persistCollection(); renderCollection();
      setCollectorStatus(`Added ${t.title}`);
    }

    function collectSelected() {
      if (!cy) return;
      const sel = cy.$('node:selected');
      if (sel.length) collectTrack(sel[0].data('raw'));
    }

    function overBox(id) {
      const el = document.getElementById(id);
      if (!el || el.offsetParent === null && getComputedStyle(el).position !== 'fixed') return false;
      const r = el.getBoundingClientRect();
      return r.width > 0 && pointer.x >= r.left && pointer.x <= r.right && pointer.y >= r.top && pointer.y <= r.bottom;
    }

    // Either drop target counts: the box in the side pane, or the floating one.
    function overCollector() { return overBox('collector-float') || overBox('collector-drop'); }

    function showFloat(on) {
      const f = document.getElementById('collector-float');
      f.style.display = on ? 'block' : 'none';
      if (!on) f.classList.remove('over');
    }

    // Graph nodes live on a canvas, so HTML drag-and-drop does not see them:
    // follow the pointer while a node is grabbed and drop it on release.
    document.addEventListener('mousemove', e => {
      pointer = {x: e.clientX, y: e.clientY};
      if (draggingNode) {
        document.getElementById('collector-float').classList.toggle('over', overBox('collector-float'));
        document.getElementById('collector-drop').classList.toggle('over', overBox('collector-drop'));
      }
    }, true);

    function bindCollectorDrag() {
      cy.on('grab', 'node', evt => {
        draggingNode = evt.target;
        dragStartPos = Object.assign({}, evt.target.position());
      });
      // Only a real drag shows the floating target, not a click to select.
      cy.on('drag', 'node', () => { if (draggingNode) showFloat(true); });
      cy.on('free', 'node', evt => {
        const node = evt.target;
        const dropped = draggingNode === node && overCollector();
        document.getElementById('collector-drop').classList.remove('over');
        showFloat(false);
        draggingNode = null;
        if (dropped) {
          collectTrack(node.data('raw'));
          if (dragStartPos) node.position(dragStartPos);   // back to its place in the graph
        }
      });
    }

    async function collectionCall(path) {
      if (!collection.length) { setCollectorStatus('Collect some songs first'); return null; }
      const resp = await fetch(path, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({name: document.getElementById('collector-name').value,
                              track_ids: collection.map(t => t.track_id)}),
      });
      return resp.json();
    }

    async function saveCollection() {
      setCollectorStatus('Saving…');
      try {
        const res = await collectionCall('/api/collection/save');
        if (!res) return;
        if (res.status !== 'saved') { setCollectorStatus(res.message || 'Could not save'); return; }
        setCollectorStatus(`Saved "${res.name}" (${res.count} songs) — in Load Playlist`);
        await loadPlaylists();
      } catch (e) { setCollectorStatus('Save failed: ' + e.message); }
    }

    async function playCollection() {
      setCollectorStatus('Starting the queue…');
      try {
        const res = await collectionCall('/api/collection/play');
        if (!res) return;
        const skipped = (res.skipped || []).length;
        const skipNote = skipped ? ` · ${skipped} without a YouTube source skipped` : '';
        if (res.status === 'playing') {
          setCollectorStatus(res.mode === 'ytmusic_queue'
            ? `Playing ${res.count} songs as a YouTube Music queue${skipNote}`
            : `Playing the first song; queue saved for the TUI (${res.message || ''})${skipNote}`);
        } else if (res.status === 'nothing_playable') {
          setCollectorStatus('None of these songs has a YouTube source to play');
        } else {
          setCollectorStatus(res.message || 'Could not start the queue');
        }
      } catch (e) { setCollectorStatus('Play failed: ' + e.message); }
    }

    function clearCollection() {
      collection = []; persistCollection(); renderCollection(); setCollectorStatus('');
    }

    document.addEventListener('DOMContentLoaded', renderCollection);

    // --- Combined facts ----------------------------------------------------
    // Seven facts, scored on every link by graph_view.edge_facts.
    const FACT_LABELS = {
      chords: 'Chord motion', sound: 'Sound (CLAP)', lyrics: 'Lyric meaning',
      mood: 'Same mood', key: 'Key mixes', tempo: 'Tempo (±6%)', genre: 'Shared genre',
    };
    // Dim for a link that rests on one fact, bright when several agree.
    const EDGE_STRENGTH_COLORS = ['#1e293b', '#334155', '#475569', '#0e7490', '#0891b2', '#22d3ee', '#a5f3fc'];
    let factThresholds = {};

    function pressed(groupSel, attr) {
      return [...document.querySelectorAll(`${groupSel} .chip[aria-pressed="true"]`)].map(c => c.dataset[attr]);
    }

    function selectedStrategies() {
      const picked = pressed('#strategy-chips', 'strategy');
      return picked.length ? picked : ['structural'];
    }

    function bindChips() {
      document.querySelectorAll('#strategy-chips .chip, #require-chips .chip').forEach(chip => {
        chip.addEventListener('click', () => {
          const on = chip.getAttribute('aria-pressed') !== 'true';
          chip.setAttribute('aria-pressed', String(on));
          // Links need at least one search behind them.
          if (!on && chip.dataset.strategy && !pressed('#strategy-chips', 'strategy').length) {
            chip.setAttribute('aria-pressed', 'true');
          }
        });
      });
      const fromUrl = new URLSearchParams(window.location.search).get('strategy');
      if (fromUrl) {
        const want = fromUrl.split(',');
        document.querySelectorAll('#strategy-chips .chip').forEach(c =>
          c.setAttribute('aria-pressed', String(want.includes(c.dataset.strategy))));
        if (!pressed('#strategy-chips', 'strategy').length) {
          document.querySelector('#strategy-chips .chip').setAttribute('aria-pressed', 'true');
        }
      }
    }

    function hashHue(text) {
      let h = 0;
      for (const ch of text) h = (h * 31 + ch.charCodeAt(0)) % 360;
      return h;
    }

    function nodeColor(n) {
      const by = document.getElementById('color-select').value;
      if (by === 'mood') return MOOD_COLORS[(n.dominant_mood || 'neutral').toLowerCase()] || '#94a3b8';
      if (by === 'genre') {
        const g = (n.artist_genres && n.artist_genres[0]) || n.genre;
        return g ? `hsl(${hashHue(g)}, 65%, 55%)` : '#6c757d';
      }
      if (by === 'energy') {
        if (n.energy == null) return '#6c757d';
        const e = Math.max(0, Math.min(1, n.energy));
        return `hsl(${Math.round(220 - 220 * e)}, 75%, 55%)`;  // calm blue to intense red
      }
      return n.key_color || n.color || '#38bdf8';
    }

    function recolor() {
      if (!cy) return;
      cy.nodes().forEach(node => node.data('color', nodeColor(node.data('raw'))));
    }

    function factValue(fact, val) {
      if (val === null || val === undefined) return 'unknown';
      if (typeof val === 'number') {
        const cut = factThresholds[fact];
        return cut != null ? `${val.toFixed(2)} (≥${cut})` : val.toFixed(2);
      }
      if (val === true) return 'yes';
      if (val === false) return 'no';
      return String(val);
    }

    function showWhyLinked(edge) {
      if (!edge || !edge.facts) return;
      const a = cy.getElementById(String(edge.source)).data('raw') || {};
      const b = cy.getElementById(String(edge.target)).data('raw') || {};
      document.getElementById('why-pair').textContent =
        `${a.title || edge.source} ↔ ${b.title || edge.target} · found by ${(edge.found_by || []).join(', ')}`;
      const list = document.getElementById('why-list');
      list.innerHTML = '';
      const agree = new Set(edge.agree || []);
      Object.keys(FACT_LABELS).forEach(fact => {
        const li = document.createElement('li');
        li.className = agree.has(fact) ? 'yes' : 'no';
        const name = document.createElement('span');
        name.className = 'why-name';
        name.textContent = FACT_LABELS[fact];
        const val = document.createElement('span');
        val.className = 'why-val';
        val.textContent = factValue(fact, edge.facts[fact]);
        li.append(name, val);
        list.appendChild(li);
      });
      document.getElementById('why-card').style.display = 'block';
    }

    function renderCytoscape(data) {
      factThresholds = (data.stats && data.stats.fact_thresholds) || {};
      document.getElementById('why-card').style.display = 'none';
      const elements = [];
      data.nodes.forEach(n => {
        elements.push({
          data: {
            id: n.id,
            label: n.artist + '\\n' + n.title,
            color: nodeColor(n),
            raw: n,
            borderWidth: n.is_seed ? 4 : ((n.mood_swings || 0) >= MOOD_SWINGY ? 3 : 1),
            borderColor: n.is_seed ? '#38bdf8' : ((n.mood_swings || 0) >= MOOD_SWINGY ? '#e2e8f0' : '#1e293b'),
            borderStyle: (n.mood_swings || 0) >= MOOD_SWINGY ? 'dashed' : 'solid',
          }
        });
      });
      const nodeIds = new Set(data.nodes.map(n => String(n.id)));
      data.edges.forEach(e => {
        if (nodeIds.has(String(e.source)) && nodeIds.has(String(e.target))) {
          elements.push({
            data: {
              id: e.id,
              source: String(e.source),
              target: String(e.target),
              weight: e.weight,
              raw: e,
              width: 1 + (e.strength || 0) * 1.2,
              lineColor: EDGE_STRENGTH_COLORS[Math.min(e.strength || 0, EDGE_STRENGTH_COLORS.length - 1)],
            }
          });
        }
      });

      if (cy) cy.destroy();
      cy = cytoscape({
        container: document.getElementById('cy'),
        elements: elements,
        style: [
          {
            selector: 'node',
            style: {
              'background-color': 'data(color)',
              'label': 'data(label)',
              'color': '#f8fafc',
              'font-size': '10px',
              'text-valign': 'bottom',
              'text-margin-y': 5,
              'text-wrap': 'wrap',
              'text-max-width': '90px',
              'border-width': 'data(borderWidth)',
              'border-color': 'data(borderColor)',
              'border-style': 'data(borderStyle)',
              'width': '32px',
              'height': '32px',
              'transition-property': 'background-color, border-color, width, height',
              'transition-duration': '0.2s'
            }
          },
          {
            selector: 'node:selected',
            style: {
              'border-width': 4,
              'border-color': '#f59e0b',
              'width': '40px',
              'height': '40px',
            }
          },
          {
            selector: 'edge',
            style: {
              'width': 'data(width)',
              'line-color': 'data(lineColor)',
              'curve-style': 'bezier',
              'opacity': 0.75
            }
          },
          {
            selector: 'edge:selected',
            style: { 'line-color': '#f59e0b', 'opacity': 1 }
          }
        ],
        layout: {
          name: 'cose',
          animate: elements.length <= 50,
          randomize: false,
          nodeRepulsion: elements.length > 50 ? 200000 : 450000,
          idealEdgeLength: elements.length > 50 ? 60 : 100,
          gravity: 0.25,
          numIter: elements.length > 50 ? 150 : 600
        }
      });

      cy.on('tap', 'node', function(evt) {
        const node = evt.target;
        showNodeDetails(node.data('raw'));
      });
      cy.on('tap', 'edge', function(evt) {
        showWhyLinked(evt.target.data('raw'));
      });
      cy.on('tap', function(evt) {
        if (evt.target === cy) document.getElementById('why-card').style.display = 'none';
      });
      bindCollectorDrag();
      cy.on('tap grab pan zoom', function(evt) {
        // Programmatic fits fire zoom too; only count what a person did.
        if (evt.originalEvent) lastInteraction = Date.now();
      });
      // The layout fits the graph to the canvas, which blows a lone node (a
      // song not analysed yet) up to fill the screen. Cap the fit, not the
      // user: manual zoom stays free.
      cy.one('layoutstop', function() {
        const MAX_FIT_ZOOM = 1.5;
        if (cy.zoom() > MAX_FIT_ZOOM) {
          cy.zoom(MAX_FIT_ZOOM);
          cy.center();
        }
      });

      // Auto-select the seed node and show its details in the sidebar
      const seedNode = data.nodes.find(n => n.is_seed) || data.nodes[0];
      if (seedNode) {
        showNodeDetails(seedNode);
        const cyNode = cy.getElementById(String(seedNode.id));
        if (cyNode) {
          cyNode.select();
        }
      }
    }

    // Same colours as /mood and the Mood strategy (graph_view.MOOD_COLORS).
    const MOOD_COLORS = __MOOD_COLORS__;
    // Two or more mood changes inside a song earn the dashed ring.
    const MOOD_SWINGY = 2;

    function fmtTime(s) {
      s = Math.max(0, Math.round(s));
      return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
    }

    function renderMoodArc(track) {
      const box = document.getElementById('d-moodarc');
      const segs = track.mood_segments || [];
      if (!segs.length) { box.style.display = 'none'; return; }
      const total = track.mood_duration || segs[segs.length - 1][1] || 1;
      const strip = document.getElementById('d-moodstrip');
      strip.innerHTML = '';
      segs.forEach(([start, end, mood, alt, feeling, contrast]) => {
        const div = document.createElement('div');
        const c1 = MOOD_COLORS[mood] || MOOD_COLORS.neutral;
        const c2 = alt ? (MOOD_COLORS[alt] || MOOD_COLORS.neutral) : null;
        div.style.width = `${Math.max(0.5, (end - start) / total * 100)}%`;
        // Borderline stretches are striped in both moods.
        div.style.background = c2
          ? `repeating-linear-gradient(45deg, ${c1} 0 6px, ${c2} 6px 12px)` : c1;
        div.title = `${fmtTime(start)}–${fmtTime(end)}  ${feeling || mood}`
          + (feeling ? ` (${mood})` : (alt ? ' ↔ ' + alt : ''))
          + (contrast ? ` · ${contrast}` : '');
        strip.appendChild(div);
      });
      const swings = track.mood_swings || 0;
      const border = Math.round((track.mood_borderline || 0) * 100);
      document.getElementById('d-moodarc-text').textContent =
        `${track.mood_arc || ''} · ${swings} swing${swings === 1 ? '' : 's'}` +
        (border ? ` · ${border}% borderline` : '');
      box.style.display = 'block';
    }

    function showNodeDetails(track) {
      selectedTrackId = track.track_id;
      document.getElementById('d-title').textContent = track.title;
      document.getElementById('d-artist').textContent = track.artist;
      document.getElementById('d-camelot').textContent = (track.camelot || '?') + ' (' + (track.key || 'unknown') + ')';
      document.getElementById('d-bpm').textContent = track.bpm ? track.bpm + ' BPM' : 'N/A';
      document.getElementById('d-form').textContent = track.form || 'diatonic';
      document.getElementById('d-fifths').textContent = track.fifth_ratio != null ? Math.round(track.fifth_ratio * 100) + '%' : 'N/A';
      document.getElementById('d-mood').textContent = (track.dominant_mood || 'neutral').toUpperCase();
      document.getElementById('d-sentiment').textContent = track.sentiment_hits != null ? track.sentiment_hits + ' hits' : 'N/A';
      document.getElementById('d-genre').textContent = track.genre || 'Various';
      document.getElementById('d-chords').textContent = track.distinct_chords ? track.distinct_chords + ' distinct' : 'N/A';

      renderMoodArc(track);

      document.getElementById('d-meta').style.display = 'grid';
      document.getElementById('d-actions').style.display = 'flex';
    }

    async function playCurrentSelected() {
      if (!selectedTrackId) return;
      toast('Triggering playback for track ' + selectedTrackId + '...');
      try {
        const resp = await fetch(`/api/play?track_id=${selectedTrackId}`, {
          method: 'POST',
          headers: { 'Accept': 'application/json' }
        });
        const res = await resp.json();
        if (resp.ok && res.status === 'launched') {
          toast('Playing: ' + (res.title ? (res.artist + ' - ' + res.title) : ('Track ' + selectedTrackId)));
        } else if (res.title) {
          toast('Playing: ' + res.title);
        } else {
          toast('Playback: ' + (res.error || resp.statusText || 'Triggered'));
        }
      } catch (err) {
        toast('Playback failed: ' + err.message);
      }
    }

    function recenterOnSelected() {
      if (!selectedTrackId) return;
      document.getElementById('seed-input').value = selectedTrackId;
      loadGraph({ track_id: selectedTrackId });
    }

    window.addEventListener('DOMContentLoaded', () => {
      bindChips();
      loadPlaylists();
      loadGraph();
    });
  </script>
</body>
</html>
"""


# The page's mood colours come from MOOD_COLORS, so the strip, the Mood
# strategy and /mood cannot drift apart.
GRAPH_HTML = GRAPH_HTML.replace("__MOOD_COLORS__", json.dumps(MOOD_COLORS))


async def serve_graph_ui(request: Request) -> HTMLResponse:
    """Serve the interactive Cytoscape.js similarity graph UI."""
    return HTMLResponse(GRAPH_HTML)


async def api_playlists(request: Request) -> JSONResponse:
    """Return available saved playlists for graph exploration."""
    res: list[dict[str, Any]] = []
    seen: set[str] = set()
    try:
        with localcache.connect() as conn:
            try:
                dj_pl = localcache.find_saved_playlist_by_id("dj-list", conn=conn)
                if dj_pl:
                    res.append({
                        "playlist_id": dj_pl["playlist_id"],
                        "name": dj_pl["name"],
                        "track_count": dj_pl.get("track_count", 0),
                    })
                    seen.add(dj_pl["playlist_id"])
            except Exception:
                pass
            pls = localcache.get_saved_playlists(limit=100, conn=conn)
            for p in pls:
                pid = p["playlist_id"]
                if pid not in seen:
                    seen.add(pid)
                    res.append({
                        "playlist_id": pid,
                        "name": p["name"],
                        "track_count": p.get("track_count", 0),
                    })
    except Exception as exc:
        log.warning("api_playlists error: %s", exc)
    return JSONResponse(res)


async def api_graph_data(request: Request) -> JSONResponse:
    """Return JSON node-link graph data for a track or playlist."""
    params = request.query_params
    track_id_str = params.get("track_id")
    playlist_id = params.get("playlist_id")
    strategy = params.get("strategy", "structural")
    hops = int(params.get("hops", "2"))
    limit = int(params.get("limit", "4"))
    query = params.get("query", "").strip()

    min_score = float(params["min_score"]) if params.get("min_score") not in (None, "") else None
    bpm_min = float(params["bpm_min"]) if params.get("bpm_min") not in (None, "") else None
    bpm_max = float(params["bpm_max"]) if params.get("bpm_max") not in (None, "") else None
    genre_filter = params.get("genre", "").strip() or None
    camelot_filter = params.get("camelot", "").strip() or None
    mood_filter = params.get("mood", "").strip() or None
    require = parse_facts(params.get("require", ""))
    min_facts = int(params["min_facts"]) if params.get("min_facts", "").isdigit() else 0

    track_id: Optional[int] = None
    if track_id_str and track_id_str.isdigit():
        track_id = int(track_id_str)
    elif query:
        # Search relaxed: handle "Artist - Title", "Title (Official Audio)", etc.
        with localcache.connect() as conn:
            import re
            if any(sep in query for sep in (" - ", " – ", " — ")):
                parts = re.split(r"\s*[-–—]\s*", query, maxsplit=1)
                track_id = localcache.find_track_id_relaxed(parts[0].strip(), parts[1].strip(), conn)
            if track_id is None:
                track_id = localcache.find_track_id_relaxed("", query, conn)
            if track_id is None:
                cur = conn.cursor()
                cur.execute("""
                    SELECT track_id FROM tracks
                    WHERE artist ILIKE %s OR title ILIKE %s OR (artist || ' ' || title) ILIKE %s
                    ORDER BY play_count DESC LIMIT 1
                """, (f"%{query}%", f"%{query}%", f"%{query}%"))
                row = cur.fetchone()
                if row:
                    track_id = row["track_id"]

    data = build_similarity_graph(
        track_id=track_id,
        playlist_id=playlist_id,
        strategy=strategy,
        hops=hops,
        limit_per_node=limit,
        min_score=min_score,
        bpm_min=bpm_min,
        bpm_max=bpm_max,
        genre_filter=genre_filter,
        camelot_filter=camelot_filter,
        mood_filter=mood_filter,
        require=require,
        min_facts=min_facts,
    )
    return JSONResponse(data)


async def api_identify(request: Request) -> JSONResponse:
    """Identify the audible song with songrec (or radio mode) and locate it.

    songrec listens for up to ~20s, so it runs off the event loop.
    """
    import anyio

    from . import song_finder

    source = request.query_params.get("source", "auto")
    if source not in ("auto", "mic", "output"):
        return JSONResponse({"status": "error", "message": f"unknown source {source!r}"},
                            status_code=400)
    try:
        result = await anyio.to_thread.run_sync(song_finder.identify, source)
    except song_finder.Busy as exc:
        return JSONResponse({"status": "busy", "message": str(exc)}, status_code=409)
    except Exception as exc:
        log.warning("api_identify failed: %s", exc)
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=500)
    return JSONResponse(result)


async def api_locate(request: Request) -> JSONResponse:
    """Place a named song, queueing processing when the graph cannot yet.

    Separate from now_playing on purpose: that is also an MCP tool, and a
    lookup should not start downloads as a side effect.
    """
    import anyio

    from . import song_finder

    try:
        body = await request.json()
    except Exception:
        body = {}
    artist = str(body.get("artist") or "").strip()
    title = str(body.get("title") or "").strip()
    if not title:
        return JSONResponse({"status": "error", "message": "title is required"},
                            status_code=400)
    result = await anyio.to_thread.run_sync(song_finder.locate, artist, title)
    return JSONResponse(result)


async def api_identify_status(request: Request) -> JSONResponse:
    """Whether a song queued by api_identify can be placed in the graph yet."""
    import anyio

    from . import song_finder

    artist = request.query_params.get("artist", "")
    title = request.query_params.get("title", "")
    if not title:
        return JSONResponse({"status": "error", "message": "title is required"},
                            status_code=400)
    result = await anyio.to_thread.run_sync(song_finder.status, artist, title)
    return JSONResponse(result)


async def _collection_request(request: Request) -> tuple[str, list[int]]:
    try:
        body = await request.json()
    except Exception:
        body = {}
    ids = []
    for raw in body.get("track_ids") or []:
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    return str(body.get("name") or ""), ids


async def api_collection_save(request: Request) -> JSONResponse:
    """Save the collector's songs as a playlist (offered in Load Playlist)."""
    import anyio

    from . import collection

    name, ids = await _collection_request(request)
    if not ids:
        return JSONResponse({"status": "error", "message": "nothing collected"}, status_code=400)

    def work():
        with localcache.connect() as conn:
            return collection.save(name, ids, conn)
    return JSONResponse({"status": "saved", **await anyio.to_thread.run_sync(work)})


async def api_collection_play(request: Request) -> JSONResponse:
    """Play the collector's songs in order (YouTube Music queue + active queue)."""
    import anyio

    from . import collection

    name, ids = await _collection_request(request)
    if not ids:
        return JSONResponse({"status": "error", "message": "nothing collected"}, status_code=400)

    def work():
        with localcache.connect() as conn:
            return collection.play(name, ids, conn)
    try:
        return JSONResponse(await anyio.to_thread.run_sync(work))
    except Exception as exc:
        log.warning("api_collection_play failed: %s", exc)
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=500)


async def api_play(request: Request) -> JSONResponse:
    """Trigger playback of a track ID via the MCP player launcher."""
    params = request.query_params
    track_id_str = params.get("track_id")
    if not track_id_str or not track_id_str.isdigit():
        if request.method == "POST":
            try:
                body = await request.json()
                if isinstance(body, dict) and "track_id" in body:
                    track_id_str = str(body["track_id"])
            except Exception:
                pass
    if not track_id_str or not track_id_str.isdigit():
        return JSONResponse({"error": "Missing valid track_id"}, status_code=400)

    from .mcp_server import play_track
    res = play_track(int(track_id_str))
    return JSONResponse(json.loads(res))
