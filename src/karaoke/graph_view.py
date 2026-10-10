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
) -> dict[str, Any]:
    """Build a node-link similarity graph centered on a track or playlist."""
    hops = max(1, min(hops, 3))
    limit_per_node = max(1, min(limit_per_node, 8))
    seed_ids: list[int] = []

    with localcache.connect() as conn:
        if playlist_id:
            try:
                pl_tracks = localcache.get_saved_playlist_tracks(playlist_id, conn=conn)
                seed_ids = [t["track_id"] for t in pl_tracks if t.get("track_id")]
            except Exception as exc:
                log.warning("Could not read playlist %s: %s", playlist_id, exc)

        if not seed_ids and track_id is not None:
            seed_ids = [track_id]

        if not seed_ids:
            # Fallback: look for most played analyzed track
            cur = conn.cursor()
            cur.execute("""
                SELECT t.track_id FROM tracks t
                JOIN track_analysis a ON a.track_id = t.track_id
                WHERE a.bpm IS NOT NULL
                ORDER BY t.play_count DESC, t.track_id ASC
                LIMIT 1
            """)
            row = cur.fetchone()
            if row:
                seed_ids = [row["track_id"]]
            else:
                seed_ids = [14082]

    visited: set[int] = set()
    queue: list[tuple[int, int]] = [(sid, 0) for sid in seed_ids]
    edges: list[dict[str, Any]] = []
    seen_edges: set[tuple[int, int]] = set()

    while queue:
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
                                SELECT t.track_id, a.bpm
                                FROM tracks t
                                JOIN track_analysis a ON a.track_id = t.track_id
                                WHERE t.track_id != %s AND a.detected_key IS NOT NULL
                                ORDER BY t.play_count DESC
                                LIMIT 40
                            """, (curr_id,))
                            for r in cur.fetchall():
                                rk = musictheory.parse_key(arow["detected_key"])
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
            edge_key = tuple(sorted([curr_id, tid]))
            if edge_key not in seen_edges:
                seen_edges.add(edge_key)
                edges.append({
                    "source": curr_id,
                    "target": tid,
                    "weight": nbr.get("score", 0.8),
                    "relation": nbr.get("space", strategy),
                })
            if tid not in visited and depth + 1 < hops:
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

    # Enrich with OpenSearch progression, chord, and mood data if available
    try:
        from .key_progression import PROGRESSION_INDEX, doc_id
        from .osclient import client as get_os_client
        osc = get_os_client()
        if osc:
            if osc.indices.exists(index=PROGRESSION_INDEX):
                for tid in all_ids:
                    if tid in nodes:
                        doc = osc.get(index=PROGRESSION_INDEX, id=doc_id(tid), ignore=404)
                        if doc.get("found"):
                            src = doc["_source"]
                            nodes[tid]["form"] = src.get("form", "unknown")
                            nodes[tid]["fifth_ratio"] = round(src["fifth_ratio"], 3) if src.get("fifth_ratio") is not None else None
                            nodes[tid]["distinct_chords"] = src.get("distinct_chords")

            if osc.indices.exists(index="tracks"):
                for tid in all_ids:
                    tdoc = osc.get(index="tracks", id=f"sqlite:{tid}", ignore=404)
                    if not tdoc.get("found"):
                        s_res = osc.search(index="tracks", body={"size": 1, "query": {"term": {"track_id": tid}}})
                        hits = s_res.get("hits", {}).get("hits", [])
                        if hits:
                            tdoc = hits[0]
                    if tdoc.get("found") or "_source" in tdoc:
                        tsrc = tdoc["_source"]
                        dm = tsrc.get("dominant_mood") or "neutral"
                        if tid in nodes:
                            nodes[tid]["dominant_mood"] = dm
                            nodes[tid]["sentiment_hits"] = tsrc.get("sentiment_hits", 0)
                            if strategy in ("mood", "feeling", "sentiment"):
                                nodes[tid]["color"] = MOOD_COLORS.get(dm.lower(), "#94a3b8")
                        else:
                            # Node missing in Postgres: populate directly from OpenSearch
                            color = MOOD_COLORS.get(dm.lower(), "#94a3b8") if strategy in ("mood", "feeling", "sentiment") else "#6c757d"
                            nodes[tid] = {
                                "id": str(tid),
                                "track_id": tid,
                                "label": f"{tsrc.get('artist', '')} — {tsrc.get('title', '')}",
                                "artist": tsrc.get("artist", ""),
                                "title": tsrc.get("title", ""),
                                "key": "",
                                "camelot": "?",
                                "color": color,
                                "bpm": None,
                                "energy": None,
                                "genre": "",
                                "play_count": 0,
                                "is_seed": tid in seed_ids,
                                "dominant_mood": dm,
                                "sentiment_hits": tsrc.get("sentiment_hits", 0),
                            }
    except Exception as exc:
        log.warning("OpenSearch node enrichment error: %s", exc)

    formatted_edges = [
        {
            "id": f"e_{e['source']}_{e['target']}",
            "source": str(e["source"]),
            "target": str(e["target"]),
            "weight": e["weight"],
            "relation": e["relation"],
        }
        for e in edges
        if e["source"] in nodes and e["target"] in nodes
    ]

    return {
        "nodes": list(nodes.values()),
        "edges": formatted_edges,
        "stats": {
            "node_count": len(nodes),
            "edge_count": len(edges),
            "strategy": strategy,
            "seed_ids": seed_ids,
            "hops": hops,
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
      padding: 12px 20px;
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
      gap: 10px;
      flex-wrap: wrap;
    }
    input, select, button {
      background: #23283b;
      color: #f1f5f9;
      border: 1px solid #3b4261;
      padding: 6px 12px;
      border-radius: 6px;
      font-size: 0.85rem;
      outline: none;
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
    <div class="brand">
      <span>✨ Karaoke Similarity Constellation</span>
    </div>
    <div class="controls">
      <input type="text" id="seed-input" placeholder="Track ID or Artist / Title..." style="width: 200px;">
      <select id="strategy-select">
        <option value="structural">Structural (Chord Motion)</option>
        <option value="context">Context (Lyrics & Meaning)</option>
        <option value="mood">Mood (Emotion & Feeling)</option>
        <option value="acoustic">Acoustic (CLAP Timbre)</option>
        <option value="harmonic">Harmonic (Camelot Key)</option>
      </select>
      <select id="hops-select">
        <option value="1">1 Hop</option>
        <option value="2" selected>2 Hops</option>
        <option value="3">3 Hops</option>
      </select>
      <button onclick="loadGraph()">Explore</button>
      <button class="btn-secondary" onclick="loadNowPlaying()">Now Playing</button>
      <button class="btn-secondary" onclick="loadPlaylist('jazz-funk-happy')">Jazz Playlist</button>
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
          • <strong>Seed Nodes:</strong> Highlighted with bright border.
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

    async function loadGraph(customParams = {}) {
      toast('Loading similarity constellation...');
      const urlParams = new URLSearchParams(window.location.search);
      const trackInput = document.getElementById('seed-input').value.trim();
      const strategy = customParams.strategy || document.getElementById('strategy-select').value;
      const hops = customParams.hops || document.getElementById('hops-select').value;

      let fetchUrl = `/api/graph_data?strategy=${strategy}&hops=${hops}`;
      if (customParams.playlist_id) {
        fetchUrl += `&playlist_id=${customParams.playlist_id}`;
      } else if (customParams.track_id) {
        fetchUrl += `&track_id=${customParams.track_id}`;
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
        } else {
          toast('No active track recognized in library');
        }
      } catch (e) {
        toast('Could not fetch now playing');
      }
    }

    function loadPlaylist(plId) {
      document.getElementById('seed-input').value = '';
      loadGraph({ playlist_id: plId });
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
          animate: true,
          randomize: false,
          nodeRepulsion: 450000,
          idealEdgeLength: 100,
          gravity: 0.25,
          numIter: 1000
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
      loadGraph();
    });
  </script>
</body>
</html>
"""


async def serve_graph_ui(request: Request) -> HTMLResponse:
    """Serve the interactive Cytoscape.js similarity graph UI."""
    return HTMLResponse(GRAPH_HTML)


async def api_graph_data(request: Request) -> JSONResponse:
    """Return JSON node-link graph data for a track or playlist."""
    params = request.query_params
    track_id_str = params.get("track_id")
    playlist_id = params.get("playlist_id")
    strategy = params.get("strategy", "structural")
    hops = int(params.get("hops", "2"))
    limit = int(params.get("limit", "4"))
    query = params.get("query", "").strip()

    track_id: Optional[int] = None
    if track_id_str and track_id_str.isdigit():
        track_id = int(track_id_str)
    elif query:
        # Search relaxed
        with localcache.connect() as conn:
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
