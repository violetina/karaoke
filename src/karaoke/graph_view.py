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
) -> dict[str, Any]:
    """Build a node-link similarity graph centered on a track or playlist."""
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
                active_player = playerctl.playing_player()
                meta = playerctl.current_metadata(active_player) if active_player else playerctl.current_metadata()
                if meta and (meta.artist or meta.title):
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
                where_clauses.append("g.genre ILIKE %s")
                params.append(f"%{genre_filter}%")
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
    total_playlist_tracks = len(seed_ids)
    if len(seed_ids) > MAX_SEEDS:
        seed_ids = seed_ids[:MAX_SEEDS]

    if len(seed_ids) > 10 and hops > 1:
        hops = 1

    MAX_TOTAL_GRAPH_NODES = 80
    visited: set[int] = set()
    queue: list[tuple[int, int]] = [(sid, 0) for sid in seed_ids]
    edges: list[dict[str, Any]] = []
    seen_edges: set[tuple[int, int]] = set()

    while queue and len(visited) < MAX_TOTAL_GRAPH_NODES:
        curr_id, depth = queue.pop(0)
        if curr_id in visited or depth >= hops:
            continue
        visited.add(curr_id)

        neighbors: list[dict[str, Any]] = []
        if strategy in ("structural", "chords", "chord_progression"):
            from . import search
            neighbors = search.similar_chords_for_track(curr_id, k=limit_per_node)
        elif strategy in ("context", "lyrics", "meaning"):
            from . import search
            neighbors = search.similar_lyrics_for_track(curr_id, k=limit_per_node)
        elif strategy in ("mood", "feeling", "sentiment"):
            from . import search
            neighbors = search.similar_mood_for_track(curr_id, k=limit_per_node)
        elif strategy == "acoustic":
            from . import queue_suggest
            raw_hits, space = queue_suggest._seed_neighbours(curr_id, k=limit_per_node)
            neighbors = [
                {"track_id": h.track_id, "score": round(h.similarity, 3), "space": space}
                for h in raw_hits
            ]
        else:  # harmonic
            # Harmonic Camelot adjacent tracks
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
                            cam = k_obj.camelot
                            num = int(cam[:-1])
                            let = cam[-1]
                            other = "B" if let == "A" else "A"
                            compat = [
                                cam,
                                f"{num}{other}",
                                f"{(num % 12) + 1}{let}",
                                f"{((num - 2) % 12) + 1}{let}",
                            ]
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
                                    if len(neighbors) >= limit_per_node:
                                        break
            except Exception:
                pass

        for nbr in neighbors:
            tid = nbr.get("track_id")
            if not tid:
                continue
            score = float(nbr.get("score", 0.8))
            if min_score is not None and score < min_score:
                continue
            edge_key = tuple(sorted([curr_id, tid]))
            if edge_key not in seen_edges:
                seen_edges.add(edge_key)
                edges.append({
                    "source": curr_id,
                    "target": tid,
                    "weight": score,
                    "relation": nbr.get("space", strategy),
                })
            if tid not in visited and depth + 1 < hops and len(visited) + len(queue) < MAX_TOTAL_GRAPH_NODES:
                queue.append((tid, depth + 1))

    all_ids = list(set(seed_ids + [e["source"] for e in edges] + [e["target"] for e in edges]))
    nodes: dict[int, dict[str, Any]] = {}

    with localcache.connect() as conn:
        cur = conn.cursor()
        try:
            cur.execute("""
                SELECT t.track_id, t.artist, t.title, a.detected_key, a.bpm, a.energy, g.genre, t.play_count
                FROM tracks t
                LEFT JOIN track_analysis a ON a.track_id = t.track_id
                LEFT JOIN track_genre g ON g.track_id = t.track_id
                WHERE t.track_id = ANY(%s)
            """, (all_ids,))
            rows = cur.fetchall()
        except Exception:
            cur.execute(f"""
                SELECT t.track_id, t.artist, t.title, a.detected_key, a.bpm, a.energy, g.genre, t.play_count
                FROM tracks t
                LEFT JOIN track_analysis a ON a.track_id = t.track_id
                LEFT JOIN track_genre g ON g.track_id = t.track_id
                WHERE t.track_id IN ({','.join(['%s'] * len(all_ids))})
            """, tuple(all_ids))
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
                "bpm": round(r["bpm"], 1) if r["bpm"] else None,
                "energy": round(r["energy"], 2) if r["energy"] is not None else None,
                "genre": r["genre"] or "",
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
                prog_ids = [doc_id(tid) for tid in all_ids if tid in nodes]
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
                track_doc_ids = [f"sqlite:{tid}" for tid in all_ids]
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

    # Filter nodes based on user sliders and criteria (seed nodes always retained)
    filtered_nodes: dict[int, dict[str, Any]] = {}
    for tid, node in nodes.items():
        if tid not in seed_ids:
            bpm = node.get("bpm")
            if bpm_min is not None and bpm is not None and bpm < bpm_min:
                continue
            if bpm_max is not None and bpm is not None and bpm > bpm_max:
                continue
            if genre_filter and genre_filter.lower() not in (node.get("genre") or "").lower():
                continue
            if camelot_filter and camelot_filter.upper() != (node.get("camelot") or "").upper():
                continue
            if mood_filter and mood_filter.lower() != (node.get("dominant_mood") or "").lower():
                continue
        filtered_nodes[tid] = node

    formatted_edges = [
        {
            "id": f"e_{e['source']}_{e['target']}",
            "source": str(e["source"]),
            "target": str(e["target"]),
            "weight": e["weight"],
            "relation": e["relation"],
        }
        for e in edges
        if e["source"] in filtered_nodes and e["target"] in filtered_nodes
    ]

    return {
        "nodes": list(filtered_nodes.values()),
        "edges": formatted_edges,
        "stats": {
            "node_count": len(filtered_nodes),
            "edge_count": len(formatted_edges),
            "strategy": strategy,
            "seed_ids": seed_ids,
            "hops": hops,
            "min_score": min_score,
            "bpm_min": bpm_min,
            "bpm_max": bpm_max,
            "genre_filter": genre_filter,
            "camelot_filter": camelot_filter,
            "mood_filter": mood_filter,
        },
        "graph_url": f"http://localhost:8888/graph?{'playlist_id=' + playlist_id if playlist_id else 'track_id=' + str(seed_ids[0])}&strategy={strategy}",
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
        <select id="strategy-select">
          <option value="structural">Structural (Chord Motion)</option>
          <option value="acoustic">Acoustic (CLAP Timbre)</option>
          <option value="harmonic">Harmonic (Camelot Key)</option>
          <option value="context">Context (Lyrics & Meaning)</option>
          <option value="mood">Mood (Emotion & Sentiment)</option>
        </select>
        <select id="playlist-select" onchange="onPlaylistSelected()">
          <option value="">Load Playlist...</option>
        </select>
        <button onclick="loadGraph()">Explore</button>
        <button class="btn-secondary" onclick="loadNowPlaying()">Now Playing</button>
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
          <option value="jazz">Jazz</option>
          <option value="blues">Blues</option>
          <option value="rock">Rock</option>
          <option value="pop">Pop</option>
          <option value="punk">Punk</option>
          <option value="stoner rock">Stoner Rock</option>
          <option value="funk">Funk</option>
          <option value="folk">Folk</option>
          <option value="metal">Metal</option>
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
      <button onclick="loadGraph()" class="btn-apply">Filter</button>
      <button onclick="resetFilters()" class="btn-secondary">Reset</button>
      <span id="graph-stats-badge" class="stats-badge">0 nodes / 0 edges</span>
    </div>
  </header>

  <div class="main">
    <div id="cy"></div>
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
        <div class="actions" id="d-actions" style="display: none;">
          <button onclick="playCurrentSelected()">▶ Play on Desktop</button>
          <button class="btn-secondary" onclick="recenterOnSelected()">🔍 Re-center Constellation</button>
        </div>
      </div>
      <div class="card">
        <div class="legend">
          <strong>Constellation Insights</strong><br>
          • <strong>Node Color:</strong> Camelot harmonic code (or Mood emotion when in Mood strategy).<br>
          • <strong>Node Size:</strong> Degree (number of similarity links).<br>
          • <strong>Edge Thickness:</strong> Cosine chord-motion, semantic lyric, or timbre match.<br>
          • <strong>Seed Nodes:</strong> Highlighted with bright blue border.<br>
          • <strong>Sliders & Filters:</strong> Real-time filtering by similarity score, tempo, key, and mood.
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
      loadGraph();
    }

    async function loadGraph(customParams = {}) {
      toast('Loading similarity constellation...');
      const urlParams = new URLSearchParams(window.location.search);
      const trackInput = document.getElementById('seed-input').value.trim();
      const strategy = customParams.strategy || document.getElementById('strategy-select').value;
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

      const plId = customParams.playlist_id || document.getElementById('playlist-select').value;
      if (plId) {
        fetchUrl += `&playlist_id=${encodeURIComponent(plId)}`;
      } else if (customParams.track_id) {
        fetchUrl += `&track_id=${customParams.track_id}`;
      } else if (customParams.query) {
        fetchUrl += `&query=${encodeURIComponent(customParams.query)}`;
      } else if (trackInput) {
        if (!isNaN(trackInput)) {
          fetchUrl += `&track_id=${trackInput}`;
        } else {
          fetchUrl += `&query=${encodeURIComponent(trackInput)}`;
        }
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

    async function loadNowPlaying() {
      toast('Checking current desktop playing track...');
      try {
        const resp = await fetch('/api/now_playing');
        const data = await resp.json();
        if (data.track_id) {
          document.getElementById('seed-input').value = data.track_id;
          loadGraph({ track_id: data.track_id });
        } else if (data.artist || data.title) {
          const q = (data.artist && data.title) ? `${data.artist} - ${data.title}` : (data.title || data.artist);
          document.getElementById('seed-input').value = q;
          loadGraph({ query: q });
        } else {
          toast('No active track recognized in library');
        }
      } catch (e) {
        toast('Could not fetch now playing');
      }
    }

    function renderCytoscape(data) {
      const elements = [];
      data.nodes.forEach(n => {
        elements.push({
          data: {
            id: n.id,
            label: n.artist + '\\n' + n.title,
            color: n.color || '#38bdf8',
            raw: n,
            borderWidth: n.is_seed ? 4 : 1,
            borderColor: n.is_seed ? '#38bdf8' : '#1e293b',
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
              width: Math.max(1, (e.weight || 0.8) * 3.5),
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
              'line-color': '#334155',
              'curve-style': 'bezier',
              'opacity': 0.7
            }
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
      loadPlaylists();
      loadGraph();
    });
  </script>
</body>
</html>
"""


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
    )
    return JSONResponse(data)


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
