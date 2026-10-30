"""FastAPI Backend-for-Frontend (BFF) aggregator for the Karaoke Webapp.

Combines:
- Host-side stage/prompter state, live lyrics & mood visualizer (:8765)
- Similarity graph & MCP tools (:8888)
- OpenSearch artist cache & Wikidata persistent ID relations (:9200)
- Local Ollama AI DJ chat with live UI actions (:11434)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
import urllib.parse
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from opensearchpy import OpenSearch, NotFoundError
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

_log = logging.getLogger("karaoke.dashboard_api")
logging.basicConfig(level=logging.INFO)

CTRL_API_URL = os.environ.get("KARAOKE_CTRL_API_URL", "http://127.0.0.1:8765")
MCP_API_URL = os.environ.get("KARAOKE_MCP_API_URL", "http://127.0.0.1:8888")
OPENSEARCH_URL = os.environ.get("OPENSEARCH_URL", "http://127.0.0.1:9200")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
OLLAMA_MODEL = os.environ.get("KARAOKE_DJ_MODEL", "qwen3:0.6b")

USER_AGENT = "KaraokeDashboard/1.0 (contact: admin@karaoke.local)"
COMMONS_THUMB_URL = "https://commons.wikimedia.org/wiki/Special:FilePath/{filename}?width=400"

app = FastAPI(title="Karaoke Unified Dashboard API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# OpenSearch client
os_client = OpenSearch([OPENSEARCH_URL], timeout=5, max_retries=2)
CACHE_INDEX = "karaoke-artist-cache"


def ensure_cache_index():
    try:
        if not os_client.indices.exists(index=CACHE_INDEX):
            os_client.indices.create(
                index=CACHE_INDEX,
                body={
                    "settings": {"number_of_shards": 1, "number_of_replicas": 0},
                    "mappings": {
                        "properties": {
                            "qid": {"type": "keyword"},
                            "name": {"type": "text", "fields": {"keyword": {"type": "keyword"}}},
                            "mbid": {"type": "keyword"},
                            "updated_at": {"type": "date"},
                        }
                    },
                },
            )
            _log.info("Created OpenSearch cache index %s", CACHE_INDEX)
    except Exception as e:
        _log.warning("Could not ensure OpenSearch cache index: %s", e)


ensure_cache_index()


def _format_image_url(image_name: Optional[str]) -> Optional[str]:
    if not image_name:
        return None
    if image_name.startswith("http://") or image_name.startswith("https://"):
        return image_name
    cleaned = urllib.parse.quote(image_name.strip())
    return COMMONS_THUMB_URL.format(filename=cleaned)


# ---------------------------------------------------------------------------
# Wikidata & Relations Helpers
# ---------------------------------------------------------------------------

async def fetch_wikidata_entity(qid: str) -> Optional[Dict[str, Any]]:
    """Fetch Wikidata claims for a QID via Wikidata REST/action API."""
    if not re.match(r"^Q\d+$", qid):
        return None
    url = f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        try:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                return None
            data = resp.json()
            return data.get("entities", {}).get(qid)
        except Exception as exc:
            _log.warning("Wikidata fetch failed for %s: %s", qid, exc)
            return None


async def resolve_wikidata_labels(qids: List[str]) -> Dict[str, str]:
    """Bulk resolve labels for a list of QIDs."""
    if not qids:
        return {}
    unique_qids = list(set([q for q in qids if re.match(r"^Q\d+$", q)]))[:50]
    if not unique_qids:
        return {}
    params = {
        "action": "wbgetentities",
        "ids": "|".join(unique_qids),
        "props": "labels",
        "languages": "en|nl|de|fr",
        "format": "json",
    }
    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            r = await client.get("https://www.wikidata.org/w/api.php", params=params, headers=headers)
            if r.status_code != 200:
                return {}
            entities = r.json().get("entities", {})
            out = {}
            for q, ent in entities.items():
                labels = ent.get("labels", {})
                for lang in ["en", "nl", "de", "fr"]:
                    if lang in labels:
                        out[q] = labels[lang].get("value")
                        break
            return out
        except Exception as e:
            _log.warning("Error resolving wikidata labels: %s", e)
            return {}


async def search_wikidata_qid(name: str) -> Optional[Dict[str, str]]:
    """Find the top music-artist QID for a given artist name."""
    params = {
        "action": "wbsearchentities",
        "search": name,
        "language": "en",
        "format": "json",
        "limit": 5,
    }
    headers = {"User-Agent": USER_AGENT}
    async with httpx.AsyncClient(timeout=8.0) as client:
        try:
            r = await client.get("https://www.wikidata.org/w/api.php", params=params, headers=headers)
            if r.status_code == 200:
                search_res = r.json().get("search", [])
                for item in search_res:
                    labels = [item.get("label", ""), *(item.get("aliases") or []), item.get("match", {}).get("text", "")]
                    if any(label.strip().casefold() == name.strip().casefold() for label in labels):
                        return {"qid": item["id"], "label": item.get("label", name), "description": item.get("description", "")}
        except Exception as e:
            _log.warning("Wikidata search failed for %s: %s", name, e)
    return None


async def enrich_artist_profile(artist_doc: Dict[str, Any]) -> Dict[str, Any]:
    """Inspect artist document, resolve side projects and band relations, and cache in OpenSearch."""
    name = artist_doc.get("name", "")
    qid = artist_doc.get("wikidata")

    # Check cache first
    try:
        cache_id = qid or f"name_{name.lower()}"
        cached = os_client.get(index=CACHE_INDEX, id=cache_id)
        if cached and cached.get("_source"):
            return cached["_source"]
    except NotFoundError:
        pass
    except Exception as e:
        _log.debug("Cache lookup error: %s", e)

    # If no QID, try searching Wikidata
    if not qid:
        found = await search_wikidata_qid(name)
        if found:
            qid = found["qid"]
            artist_doc["wikidata"] = qid
            if not artist_doc.get("description"):
                artist_doc["description"] = found.get("description", "")

    member_of_names = list(artist_doc.get("member_of_names") or [])
    member_names = list(artist_doc.get("member_names") or [])
    side_projects: List[Dict[str, str]] = []

    # If we have a QID, fetch entity to extract side projects (P463: member of) & members (P527)
    if qid:
        ent = await fetch_wikidata_entity(qid)
        if ent:
            if name == qid:
                labels = ent.get("labels", {})
                artist_doc["name"] = labels.get("en", next(iter(labels.values()), {})).get("value", name)
            claims = ent.get("claims", {})
            # P463 = member of (bands/projects this person joined)
            p463_qids = [c["mainsnak"]["datavalue"]["value"]["id"] for c in claims.get("P463", []) if "datavalue" in c.get("mainsnak", {})]
            # P527 = has part / members
            p527_qids = [c["mainsnak"]["datavalue"]["value"]["id"] for c in claims.get("P527", []) if "datavalue" in c.get("mainsnak", {})]

            all_related_qids = list(set(p463_qids + p527_qids))
            labels_map = await resolve_wikidata_labels(all_related_qids)
            artist_doc["member_entities"] = [{"name": labels_map[q], "wikidata": q} for q in p527_qids if q in labels_map]
            artist_doc["member_of_entities"] = [{"name": labels_map[q], "wikidata": q} for q in p463_qids if q in labels_map]

            for b_qid in p463_qids:
                b_name = labels_map.get(b_qid)
                if b_name and b_name not in member_of_names:
                    member_of_names.append(b_name)
                if b_name:
                    side_projects.append({"qid": b_qid, "name": b_name, "relation": "member_of"})

            for m_qid in p527_qids:
                m_name = labels_map.get(m_qid)
                if m_name and m_name not in member_names:
                    member_names.append(m_name)

            if not artist_doc.get("image"):
                # P18 = image
                p18 = claims.get("P18", [])
                if p18 and "datavalue" in p18[0].get("mainsnak", {}):
                    artist_doc["image"] = p18[0]["mainsnak"]["datavalue"]["value"]

    artist_doc["member_of_names"] = member_of_names
    artist_doc["member_names"] = member_names
    artist_doc["side_projects"] = side_projects
    artist_doc["image_url"] = _format_image_url(artist_doc.get("image"))
    artist_doc["cached_at"] = int(time.time())

    # Cache into OpenSearch
    try:
        cache_id = qid or f"name_{name.lower()}"
        os_client.index(index=CACHE_INDEX, id=cache_id, body=artist_doc)
    except Exception as e:
        _log.warning("Failed to store artist cache in OpenSearch: %s", e)

    return artist_doc


# ---------------------------------------------------------------------------
# API Endpoints
# ---------------------------------------------------------------------------

@app.get("/mood/embedded", response_class=HTMLResponse)
def embedded_mood():
    """Reuse the real /mood canvas renderer in a compact dashboard square."""
    from .stage_view import render_mood_html

    html = render_mood_html()
    compact_style = """<style>
      body { width: 100%; height: 100vh; background: #000; }
      #hud-top, #info-box { display: none !important; }
      #ambient-glow { width: 100%; height: 100%; }
      #stage-container, #canvas-wrap { width: 100%; height: 100%; }
      #canvas-wrap { padding: 0; border: 0; border-radius: 0; }
      #mood-canvas { width: 100%; height: 100%; max-width: none; max-height: none; border-radius: 0; }
      #chord-overlay { left: 10px; bottom: 10px; }
      #chord-name { font-size: clamp(28px, 14vw, 52px); }
      #chord-roman { font-size: 14px; }
      #chord-next { font-size: 12px; }
    </style>"""
    html = html.replace("</head>", compact_style + "</head>")
    # Embedded animation follows the song only; never request microphone access.
    html = html.replace("async function toggleMic() {", "async function toggleMic() { return;")
    start = html.index("    // Listen to live captions stream")
    end = html.index("    // Render loop running at 60 FPS", start)
    return html[:start] + html[end:]


@app.get("/api/players/artwork")
async def player_artwork():
    """Serve current player artwork over HTTP, including host-local MPRIS files."""
    from fastapi.responses import Response

    async with httpx.AsyncClient(timeout=5.0, follow_redirects=True) as client:
        try:
            state = await client.get(f"{CTRL_API_URL}/api/stage/state")
            state.raise_for_status()
            art_url = state.json().get("art_url")
            if not art_url:
                raise HTTPException(status_code=404, detail="No current artwork")
            # Only the current player's URL is accepted, never a client-supplied file path.
            image = await client.get(f"{CTRL_API_URL}/api/mood-cover-image", params={"art_url": art_url})
            image.raise_for_status()
            content = image.content
            media_type = image.headers.get("content-type", "application/octet-stream")
            if content.startswith(b"\x89PNG\r\n\x1a\n"):
                media_type = "image/png"
            elif content.startswith(b"\xff\xd8\xff"):
                media_type = "image/jpeg"
            elif content.startswith(b"RIFF") and content[8:12] == b"WEBP":
                media_type = "image/webp"
            if not media_type.startswith("image/"):
                raise HTTPException(status_code=502, detail="Artwork is not an image")
            return Response(content, media_type=media_type, headers={"Cache-Control": "no-store"})
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail="Artwork unavailable") from exc


@app.get("/api/mood-feeling-image")
async def proxy_mood_feeling_image(mood: str = "neutral"):
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            r = await client.get(f"{CTRL_API_URL}/api/mood-feeling-image?mood={mood}")
            return StreamingResponse(r.aiter_bytes(), media_type=r.headers.get("content-type", "image/png"))
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/mood-cover-image")
async def proxy_mood_cover_image(mood: str = "neutral", art_url: Optional[str] = None):
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            url = f"{CTRL_API_URL}/api/mood-cover-image?mood={mood}"
            if art_url:
                url += f"&art_url={urllib.parse.quote(art_url)}"
            r = await client.get(url)
            return StreamingResponse(r.aiter_bytes(), media_type=r.headers.get("content-type", "image/jpeg"))
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/health")
async def health_check():
    """Verify backend and upstream dependencies."""
    results = {
        "status": "ok",
        "ctrl_api_8765": False,
        "mcp_api_8888": False,
        "opensearch_9200": False,
        "ollama_11434": False,
        "kiosk_active": False,
    }
    import subprocess
    try:
        proc = subprocess.run(["systemctl", "--user", "is-active", "karaoke-kiosk.service"], capture_output=True, text=True, timeout=2)
        results["kiosk_active"] = proc.stdout.strip() == "active"
    except Exception:
        pass
    async with httpx.AsyncClient(timeout=1.5) as client:
        try:
            r = await client.get(f"{CTRL_API_URL}/api/health")
            results["ctrl_api_8765"] = r.status_code == 200
        except Exception:
            pass
        try:
            r = await client.get(f"{MCP_API_URL}/")
            results["mcp_api_8888"] = r.status_code == 200
        except Exception:
            pass
        try:
            r = await client.get(f"{OPENSEARCH_URL}")
            results["opensearch_9200"] = r.status_code == 200
        except Exception:
            pass
        try:
            r = await client.get(f"{OLLAMA_URL}/api/tags")
            results["ollama_11434"] = r.status_code == 200
        except Exception:
            pass
    return results


class RadioModePayload(BaseModel):
    enabled: bool


def _radio_mode_status() -> dict[str, Any]:
    """Return service state plus the latest songrec match, if any."""
    from . import live_state

    result = subprocess.run(
        ["systemctl", "--user", "is-active", "karaoke-radio.service"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    active = result.returncode == 0 and result.stdout.strip() == "active"
    live = live_state.read("radio") if active else None
    return {
        "enabled": active,
        "status": "listening" if active and not (live or {}).get("title") else (
            "recognized" if active else "stopped"
        ),
        "artist": (live or {}).get("artist", ""),
        "title": (live or {}).get("title", ""),
        "processing": "Full processing is queued automatically for every recognized song" if active else "",
    }


@app.get("/api/radio")
async def radio_mode_status():
    """Report whether dashboard-controlled songrec radio mode is running."""
    return await asyncio.to_thread(_radio_mode_status)


@app.post("/api/radio")
async def set_radio_mode(body: RadioModePayload):
    """Start or stop songrec radio mode through its systemd user unit."""
    action = "start" if body.enabled else "stop"

    def change_state():
        result = subprocess.run(
            ["systemctl", "--user", action, "karaoke-radio.service"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"systemctl {action} failed"
            raise RuntimeError(detail)
        status = _radio_mode_status()
        if status["enabled"] != body.enabled:
            raise RuntimeError(f"radio mode did not {action} successfully")
        return status

    try:
        return await asyncio.to_thread(change_state)
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


class FollowModePayload(BaseModel):
    enabled: bool


def _follow_mode_status() -> dict[str, Any]:
    """Return queue-follower service state plus the row it is on."""
    from . import localcache

    result = subprocess.run(
        ["systemctl", "--user", "is-active", "karaoke-queue-follow.service"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    active = result.returncode == 0 and result.stdout.strip() == "active"

    name = ""
    index = 0
    total = 0
    current = None
    try:
        q = localcache.load_active_queue()
        if q and q.get("rows"):
            rows = q["rows"]
            name = q.get("name", "") or ""
            index = int(q.get("current_index", 0) or 0)
            total = len(rows)
            if 0 <= index < total:
                cur = rows[index]
                current = {"artist": cur.get("artist", ""), "title": cur.get("title", "")}
    except Exception as exc:
        _log.debug("follow status: queue read failed: %s", exc)

    return {
        "enabled": active,
        "status": "following" if active else "stopped",
        "queue_name": name,
        "index": index,
        "total": total,
        "current": current,
    }


@app.get("/api/follow")
async def follow_mode_status():
    """Report whether the dashboard-controlled queue follower is running."""
    return await asyncio.to_thread(_follow_mode_status)


@app.post("/api/follow")
async def set_follow_mode(body: FollowModePayload):
    """Start or stop the local-queue follower through its systemd user unit."""
    action = "start" if body.enabled else "stop"

    def change_state():
        result = subprocess.run(
            ["systemctl", "--user", action, "karaoke-queue-follow.service"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"systemctl {action} failed"
            raise RuntimeError(detail)
        status = _follow_mode_status()
        if status["enabled"] != body.enabled:
            raise RuntimeError(f"queue follower did not {action} successfully")
        return status

    try:
        return await asyncio.to_thread(change_state)
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


class PlayPayload(BaseModel):
    artist: Optional[str] = None
    title: Optional[str] = None
    url: Optional[str] = None


@app.get("/api/players/kiosk/status")
async def get_kiosk_status():
    import subprocess
    try:
        proc = subprocess.run(["systemctl", "--user", "is-active", "karaoke-kiosk.service"], capture_output=True, text=True, timeout=2)
        return {"active": proc.stdout.strip() == "active"}
    except Exception:
        return {"active": False}


@app.post("/api/players/kiosk/toggle")
async def toggle_kiosk():
    import subprocess
    try:
        chk = subprocess.run(["systemctl", "--user", "is-active", "karaoke-kiosk.service"], capture_output=True, text=True, timeout=2)
        is_active = chk.stdout.strip() == "active"
        cmd = "stop" if is_active else "start"
        subprocess.run(["systemctl", "--user", cmd, "karaoke-kiosk.service"], capture_output=True, text=True, timeout=5)
        return {"status": "ok", "action": cmd, "active": not is_active}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/play")
async def play_track_endpoint(req: PlayPayload):
    """Play track and ensure kiosk player is running."""
    import subprocess
    try:
        chk = subprocess.run(["systemctl", "--user", "is-active", "karaoke-kiosk.service"], capture_output=True, text=True, timeout=2)
        if chk.stdout.strip() != "active":
            _log.info("Auto-starting karaoke-kiosk.service before play...")
            subprocess.run(["systemctl", "--user", "start", "karaoke-kiosk.service"], capture_output=True, text=True, timeout=5)
            await asyncio.sleep(1.2)
    except Exception as e:
        _log.warning("Could not auto-start kiosk: %s", e)

    async with httpx.AsyncClient(timeout=8.0) as client:
        try:
            r = await client.post(
                f"{CTRL_API_URL}/api/play",
                json={"artist": req.artist or "", "title": req.title or "", "url": req.url or ""},
            )
            return r.json()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Play failed: {exc}")


@app.post("/api/players/play-pause")
async def player_play_pause():
    async with httpx.AsyncClient(timeout=3.0) as client:
        try:
            r = await client.post(f"{CTRL_API_URL}/api/players/play-pause", json={})
            return r.json()
        except Exception as e:
            return {"status": "error", "error": str(e)}


@app.post("/api/players/pause")
async def player_pause():
    async with httpx.AsyncClient(timeout=3.0) as client:
        try:
            r = await client.post(f"{CTRL_API_URL}/api/players/pause", json={})
            return r.json()
        except Exception as e:
            return {"status": "error", "error": str(e)}


@app.post("/api/players/next")
async def player_next():
    async with httpx.AsyncClient(timeout=3.0) as client:
        try:
            r = await client.post(f"{CTRL_API_URL}/api/players/next", json={})
            return r.json()
        except Exception as e:
            return {"status": "error", "error": str(e)}


@app.post("/api/players/previous")
async def player_previous():
    async with httpx.AsyncClient(timeout=3.0) as client:
        try:
            r = await client.post(f"{CTRL_API_URL}/api/players/previous", json={})
            return r.json()
        except Exception as e:
            return {"status": "error", "error": str(e)}


class SeekPayload(BaseModel):
    offset_s: float


@app.post("/api/players/seek")
async def player_seek(body: SeekPayload):
    async with httpx.AsyncClient(timeout=3.0) as client:
        try:
            r = await client.post(f"{CTRL_API_URL}/api/players/seek", json={"offset_s": body.offset_s})
            return r.json()
        except Exception as e:
            return {"status": "error", "error": str(e)}


@app.get("/api/players/current")
async def player_current():
    async with httpx.AsyncClient(timeout=2.0) as client:
        try:
            r = await client.get(f"{CTRL_API_URL}/api/players/current")
            return r.json()
        except Exception as e:
            return {"status": "error", "error": str(e)}


@app.get("/api/playlists")
async def get_playlists():
    """Proxy saved + smart playlists from the similarity server (:8888)."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            r = await client.get(f"{MCP_API_URL}/api/playlists")
            if r.status_code == 200:
                return {"playlists": r.json()}
        except Exception as e:
            _log.warning("Failed to fetch playlists: %s", e)
    return {"playlists": []}


class PlaylistPlayPayload(BaseModel):
    playlist_id: str


@app.post("/api/playlists/play")
async def play_playlist(body: PlaylistPlayPayload):
    """Resolve a saved playlist's tracks and start them in the active queue."""
    import anyio
    from . import collection, localcache

    # Ensure the kiosk player is up first.
    import subprocess
    try:
        chk = subprocess.run(["systemctl", "--user", "is-active", "karaoke-kiosk.service"], capture_output=True, text=True, timeout=2)
        if chk.stdout.strip() != "active":
            subprocess.run(["systemctl", "--user", "start", "karaoke-kiosk.service"], capture_output=True, text=True, timeout=5)
            await asyncio.sleep(1.2)
    except Exception:
        pass

    def work():
        with localcache.connect() as conn:
            tracks = localcache.get_saved_playlist_tracks(body.playlist_id, conn=conn)
            ids = [int(t["track_id"]) for t in tracks if t.get("track_id") is not None]
            name = localcache._slug_display_name(body.playlist_id)
            if not ids:
                return {"status": "error", "message": "playlist has no resolvable tracks"}
            return collection.play(name, ids, conn)

    try:
        return await anyio.to_thread.run_sync(work)
    except Exception as e:
        _log.warning("play_playlist failed: %s", e)
        return {"status": "error", "error": str(e)}


@app.get("/api/song/mood-timeline")
def song_mood_timeline(track_id: int):
    """Expose the same stored mood summary and segments as the graph inspector."""
    from .graph_view import _add_mood_timelines

    nodes: dict[int, dict[str, Any]] = {track_id: {"track_id": track_id}}
    _add_mood_timelines(nodes, "mood")
    node = nodes[track_id]
    return node if node.get("mood_source") == "timeline" else None


@app.get("/api/song/library-context")
def song_library_context(artist: str = "", title: str = ""):
    """Read play count and saved-playlist membership without changing playback."""
    from . import localcache

    with localcache.connect() as conn:
        track_id = localcache.find_track_id(artist, title, conn) if artist and title else None
        if track_id is None:
            return {"track_id": None, "play_count": None, "playlists": []}
        row = conn.execute("SELECT play_count FROM tracks WHERE track_id = %s", (track_id,)).fetchone()
        playlists = conn.execute(
            """SELECT DISTINCT p.playlist_id, p.name
               FROM saved_playlists p
               JOIN saved_playlist_tracks pt ON pt.playlist_id = p.playlist_id
               WHERE pt.track_id = %s ORDER BY p.name""", (track_id,),
        ).fetchall()
        return {"track_id": track_id, "play_count": row["play_count"] or 0,
                "playlists": [dict(p) for p in playlists]}


class SongActionPayload(BaseModel):
    track_id: int | None = None
    artist: str = ""
    title: str = ""
    url: str = ""
    video_id: str = ""
    query: str = ""


def _best_source_url_for_track(track_id: int, conn) -> str:
    """Return the most playable source URL for a track_id (YT Music preferred)."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT s.url FROM sources s
        WHERE s.track_id = %s
        ORDER BY
            CASE
                WHEN s.kind = 'youtube_music' THEN 0
                WHEN s.kind = 'youtube' THEN 1
                WHEN s.url LIKE 'http%%' THEN 2
                ELSE 3
            END,
            s.source_id
        LIMIT 1
        """,
        (track_id,),
    )
    r = cur.fetchone()
    if not r:
        return ""
    return (dict(r).get("url") if hasattr(r, "keys") else r[0]) or ""


@app.post("/api/artist/now-playing")
async def refresh_playing_artist(body: SongActionPayload):
    """Resolve missing artist links on song changes, then return a fresh profile."""
    import anyio
    from . import localcache, postprocess_worker

    if not body.artist.strip() or not body.title.strip():
        raise HTTPException(status_code=422, detail="Artist and title are required")

    def resolve():
        with localcache.connect() as conn:
            track_id = localcache.find_track_id(body.artist, body.title, conn)
            if track_id is None:
                track_id = localcache.add_track_source(body.artist, body.title, url=body.url or None, kind="player", conn=conn)
            linked = conn.execute("SELECT 1 FROM track_artists WHERE track_id = %s LIMIT 1", (track_id,)).fetchone()
            if linked:
                return "known"
            return postprocess_worker.run_artist_logic(track_id, conn)

    status = await anyio.to_thread.run_sync(resolve)
    profile = await get_artist_info(body.artist)
    return {"artist": profile, "processing_status": status}


@app.post("/api/song/process")
async def process_song(body: SongActionPayload):
    """Fully (re)process a song: vectors, mood, chords, classification, timings."""
    import anyio
    from . import localcache, postprocess_queue

    def work():
        with localcache.connect() as conn:
            artist, title, url = body.artist, body.title, body.url
            if body.track_id is not None and (not artist or not title or not url):
                cur = conn.cursor()
                cur.execute("SELECT artist, title FROM tracks WHERE track_id = %s", (body.track_id,))
                r = cur.fetchone()
                if r:
                    rd = dict(r) if hasattr(r, "keys") else {"artist": r[0], "title": r[1]}
                    artist = artist or rd.get("artist", "")
                    title = title or rd.get("title", "")
                if not url:
                    url = _best_source_url_for_track(body.track_id, conn)
            if not (artist and title):
                return {"status": "error", "message": "artist/title required"}
            # include_timings=True → force the full pipeline for this song.
            queued = postprocess_queue.enqueue_if_needed(artist, title, url, conn=conn, include_timings=True)
            if not queued:
                # Nothing was pending OR no downloadable source; force a task anyway
                # so the worker resolves + fully re-analyses an unknown/new song.
                try:
                    postprocess_queue.publish_postprocess_task(artist, title, url, include_timings=True)
                    queued = True
                except Exception:
                    pass
            return {
                "status": "queued" if queued else "noop",
                "artist": artist,
                "title": title,
                "message": "Full processing queued" if queued else "Nothing to process or no source",
            }

    try:
        return await anyio.to_thread.run_sync(work)
    except Exception as e:
        _log.warning("process_song failed: %s", e)
        return {"status": "error", "error": str(e)}


@app.post("/api/song/dj_add")
async def dj_add_song(body: SongActionPayload):
    """Add a song to the AI DJ playlist (dj-list)."""
    import anyio
    from . import mcp_server

    def work():
        raw = mcp_server.add_to_dj_playlist(
            track_id=body.track_id,
            artist=body.artist or None,
            title=body.title or None,
        )
        try:
            return json.loads(raw)
        except Exception:
            return {"status": "ok", "raw": raw}

    try:
        return await anyio.to_thread.run_sync(work)
    except Exception as e:
        _log.warning("dj_add_song failed: %s", e)
        return {"status": "error", "error": str(e)}


@app.post("/api/song/queue")
async def queue_song(body: SongActionPayload):
    """Add a song to the local karaoke queue so it plays next.

    Inserts the track into the active queue (the one the stage's Up Next and the
    queue follower track), right after the current position -- not YouTube
    Music's transient player queue.
    """
    import anyio
    from . import collection, localcache

    def work():
        with localcache.connect() as conn:
            track_id = body.track_id
            if track_id is None and body.artist and body.title:
                track_id = localcache.find_track_id(body.artist, body.title, conn)
            if track_id is None and body.query:
                from .mcp_server import search_songs
                matches = json.loads(search_songs(query=body.query, limit=1)).get("tracks", [])
                if matches:
                    track_id = matches[0].get("track_id")
            if track_id is None:
                return {"status": "error", "message": "Could not find this song in the library"}
            return collection.enqueue_next(int(track_id), conn)

    try:
        return await anyio.to_thread.run_sync(work)
    except Exception as e:
        _log.warning("queue_song failed: %s", e)
        return {"status": "error", "error": str(e)}


@app.get("/api/stage/state")
async def get_stage_state():
    """Proxy live stage/playback/prompter state from :8765."""
    async with httpx.AsyncClient(timeout=2.0) as client:
        try:
            r = await client.get(f"{CTRL_API_URL}/api/stage/state")
            if r.status_code == 200:
                data = r.json()
                # Attach Wikimedia/Wikidata image url if available
                return data
        except Exception as e:
            _log.debug("Ctrl API unreachable: %s", e)
    return {
        "artist": "Karaoke Standby",
        "title": "Waiting for playback...",
        "album": "",
        "art_url": "",
        "status": "Stopped",
        "bpm": 120,
        "key": "8B",
        "energy": 0.5,
        "mood": "neutral",
        "lines": [],
        "upcoming_queue": [],
    }


@app.get("/api/stage/stream")
async def get_stage_stream():
    """Proxy real-time SSE stream from :8765."""
    async def sse_generator():
        async with httpx.AsyncClient(timeout=None) as client:
            try:
                async with client.stream("GET", f"{CTRL_API_URL}/api/stage/stream") as resp:
                    async for line in resp.aiter_lines():
                        if line:
                            yield f"{line}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'status': 'disconnected', 'error': str(e)})}\n\n"

    return StreamingResponse(sse_generator(), media_type="text/event-stream")


@app.get("/api/graph")
async def get_graph_data(
    limit: int = 6,
    query: str = "",
    track_id: str = "",
    strategy: str = "structural",
    hops: int = 2,
    genre: str = "",
    mood: str = "",
    camelot: str = "",
    bpm_min: str = "",
    bpm_max: str = "",
    min_score: str = "",
    require: str = "",
):
    """Retrieve similarity graph from :8888 and augment nodes with OpenSearch data.

    Passes through all upstream filters (strategy = structural/chords/mood/acoustic/
    harmonic, plus genre/mood/camelot/bpm/energy node filters). When neither query nor
    track_id is given, :8888 seeds from the currently playing track automatically.
    """
    qp: Dict[str, str] = {"limit": str(limit), "hops": str(hops), "strategy": strategy}
    if query:
        qp["query"] = query
    if track_id:
        qp["track_id"] = track_id
    if genre:
        qp["genre"] = genre
    if mood:
        qp["mood"] = mood
    if camelot:
        qp["camelot"] = camelot
    if bpm_min:
        qp["bpm_min"] = bpm_min
    if bpm_max:
        qp["bpm_max"] = bpm_max
    if min_score:
        qp["min_score"] = min_score
    if require:
        qp["require"] = require

    async with httpx.AsyncClient(timeout=8.0) as client:
        try:
            r = await client.get(f"{MCP_API_URL}/api/graph_data", params=qp)
            if r.status_code == 200:
                graph = r.json()
                nodes = graph.get("nodes", [])
                for node in nodes:
                    artist = node.get("artist")
                    if artist and artist != "Karaoke":
                        try:
                            s = os_client.search(
                                index="karaoke-artists",
                                body={"query": {"match": {"name": artist}}, "size": 1},
                            )
                            hits = s.get("hits", {}).get("hits", [])
                            if hits:
                                src = hits[0]["_source"]
                                node["wikidata"] = src.get("wikidata")
                                node["wikidata_url"] = f"https://www.wikidata.org/wiki/{src.get('wikidata')}" if src.get("wikidata") else None
                                node["image_url"] = _format_image_url(src.get("image"))
                                node["members"] = src.get("member_names", [])
                                node["member_of"] = src.get("member_of_names", [])
                        except Exception:
                            pass
                return graph
        except Exception as e:
            _log.warning("Failed to proxy similarity graph: %s", e)

    return {"nodes": [], "edges": [], "stats": {"error": "Similarity server offline"}}


@app.get("/api/artist_songs/{name}")
async def get_artist_songs(name: str):
    """List the library tracks for an artist (with key/bpm/energy/mood where known)."""
    decoded = urllib.parse.unquote(name).strip()
    try:
        from karaoke import localcache
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"localcache unavailable: {e}")

    def _query() -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        with localcache.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT t.track_id, t.artist, t.title, t.album, t.duration, t.play_count,
                       a.detected_key, a.bpm, a.energy
                FROM tracks t
                LEFT JOIN track_analysis a ON a.track_id = t.track_id
                WHERE t.artist ILIKE %s
                ORDER BY t.play_count DESC, t.title ASC
                LIMIT 200
                """,
                (f"%{decoded}%",),
            )
            for r in cur.fetchall():
                row = dict(r)
                camelot = None
                key_label = row.get("detected_key")
                if key_label:
                    try:
                        from karaoke import musictheory
                        k_obj = musictheory.parse_key(key_label)
                        if k_obj and k_obj.camelot and k_obj.camelot != "?":
                            camelot = k_obj.camelot
                    except Exception:
                        pass
                out.append({
                    "track_id": row["track_id"],
                    "artist": row["artist"],
                    "title": row["title"],
                    "album": row.get("album") or "",
                    "duration": row.get("duration"),
                    "play_count": row.get("play_count") or 0,
                    "key": key_label or "",
                    "camelot": camelot or "",
                    "bpm": round(row["bpm"], 1) if row.get("bpm") else None,
                    "energy": round(row["energy"], 2) if row.get("energy") is not None else None,
                })
        return out

    try:
        songs = await asyncio.to_thread(_query)
    except Exception as e:
        _log.warning("Artist songs query failed for %s: %s", decoded, e)
        songs = []
    return {"artist": decoded, "count": len(songs), "songs": songs}


@app.get("/api/artist/{identifier}")
async def get_artist_info(identifier: str):
    """Look up an artist by Name or Wikidata QID from OpenSearch / Wikidata cache."""
    decoded = urllib.parse.unquote(identifier).strip()
    artist_doc: Optional[Dict[str, Any]] = None

    is_qid = bool(re.fullmatch(r"Q\d+", decoded))
    is_mbid = bool(re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", decoded))
    try:
        if is_qid or is_mbid:
            field = "wikidata" if is_qid else "mbid"
            query = {"term": {field: decoded}}
        else:
            query = {"bool": {"should": [
                {"match": {"name": {"query": decoded, "operator": "and"}}},
                {"match": {"aliases": {"query": decoded, "operator": "and"}}},
            ], "minimum_should_match": 1}}
        result = os_client.search(index="karaoke-artists", body={"query": query, "size": 20})
        for hit in result.get("hits", {}).get("hits", []):
            candidate = hit["_source"]
            names = [candidate.get("name", ""), *(candidate.get("aliases") or [])]
            if is_qid or is_mbid or any(n.strip().casefold() == decoded.casefold() for n in names):
                artist_doc = candidate
                break
    except Exception as exc:
        _log.warning("OpenSearch artist query error: %s", exc)

    if artist_doc is None and is_mbid:
        # A relation can point to an artist not yet indexed in our library.
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(f"https://musicbrainz.org/ws/2/artist/{decoded}",
                                        params={"fmt": "json", "inc": "url-rels"},
                                        headers={"User-Agent": USER_AGENT})
            response.raise_for_status()
            entity = response.json()
            artist_doc = {"name": entity["name"], "mbid": decoded}
            for relation in entity.get("relations", []):
                resource = relation.get("url", {}).get("resource", "")
                if resource.startswith("https://www.wikidata.org/wiki/"):
                    artist_doc["wikidata"] = resource.rsplit("/", 1)[-1]

    if artist_doc is None:
        artist_doc = {"name": decoded, "wikidata": decoded if is_qid else None}

    # Enrich with relations (side projects, members, Wikidata images) & cache
    enriched = await enrich_artist_profile(artist_doc)
    # Use the original relation objects; index name/MBID arrays are NOT aligned.
    try:
        from . import localcache
        with localcache.connect() as conn:
            row: Any = conn.execute(
                "SELECT members, member_of, influenced_by FROM artist_wikidata WHERE qid = %s OR mbid = %s LIMIT 1",
                (enriched.get("wikidata") or "", enriched.get("mbid") or ""),
            ).fetchone()
            if row:
                for field, target in [("members", "member_entities"), ("member_of", "member_of_entities"), ("influenced_by", "influence_entities")]:
                    enriched[target] = [{"name": r["label"], "wikidata": r.get("qid")} for r in row[field] or [] if r.get("label")]
    except Exception as exc:
        _log.debug("Relation ID lookup unavailable: %s", exc)
    return enriched


@app.get("/api/artist_relations/{name_or_qid}")
async def get_artist_relations_graph(name_or_qid: str):
    """Generate a Cytoscape-formatted relation graph around an artist (bands, members, side projects)."""
    artist = await get_artist_info(name_or_qid)
    artist_name = artist.get("name", name_or_qid)
    artist_qid = artist.get("wikidata")

    nodes = []
    edges = []

    # Central artist node
    center_id = f"artist_{artist_qid or artist_name}"
    nodes.append({
        "data": {
            "id": center_id,
            "label": artist_name,
            "type": "center",
            "wikidata": artist_qid,
            "image": artist.get("image_url"),
            "color": "#00f2fe",
            "size": 65,
        }
    })

    # Bands they belong to (member_of)
    for relation in artist.get("member_of_entities") or [{"name": n} for n in artist.get("member_of_names", [])]:
        band_name = relation["name"]
        band_id = f"band_{relation.get('wikidata') or band_name}"
        nodes.append({
            "data": {
                "id": band_id,
                "label": band_name,
                "type": "band",
                "wikidata": relation.get("wikidata"),
                "color": "#ff007f",
                "size": 50,
            }
        })
        edges.append({
            "data": {
                "id": f"{center_id}->{band_id}",
                "source": center_id,
                "target": band_id,
                "label": "member of",
                "color": "#ff007f",
            }
        })

    # Band members (if this artist is a band or group)
    for relation in artist.get("member_entities") or [{"name": n} for n in artist.get("member_names", [])]:
        member_name = relation["name"]
        m_id = f"member_{relation.get('wikidata') or member_name}"
        nodes.append({
            "data": {
                "id": m_id,
                "label": member_name,
                "type": "member",
                "wikidata": relation.get("wikidata"),
                "color": "#ffbe0b",
                "size": 42,
            }
        })
        edges.append({
            "data": {
                "id": f"{center_id}<-{m_id}",
                "source": m_id,
                "target": center_id,
                "label": "member",
                "color": "#ffbe0b",
            }
        })

    # Side projects / related bands
    for proj in artist.get("side_projects", []):
        proj_name = proj.get("name")
        if not proj_name:
            continue
        p_id = f"side_{proj_name}"
        if not any(n["data"]["id"] == p_id for n in nodes):
            nodes.append({
                "data": {
                    "id": p_id,
                    "label": proj_name,
                    "type": "side_project",
                    "wikidata": proj.get("qid"),
                    "color": "#9d4edd",
                    "size": 46,
                }
            })
            edges.append({
                "data": {
                    "id": f"{center_id}->{p_id}",
                    "source": center_id,
                    "target": p_id,
                    "label": "side project",
                    "color": "#9d4edd",
                }
            })

    # Influenced by
    for relation in (artist.get("influence_entities") or [{"name": n} for n in artist.get("influenced_by_names", [])])[:4]:
        infl = relation["name"]
        inf_id = f"inf_{relation.get('wikidata') or infl}"
        nodes.append({
            "data": {
                "id": inf_id,
                "label": infl,
                "type": "influence",
                "wikidata": relation.get("wikidata"),
                "color": "#48cae4",
                "size": 38,
            }
        })
        edges.append({
            "data": {
                "id": f"{inf_id}->{center_id}",
                "source": inf_id,
                "target": center_id,
                "label": "influenced",
                "color": "#48cae4",
            }
        })

    return {"nodes": nodes, "edges": edges, "artist": artist}


# ---------------------------------------------------------------------------
# AI DJ Chat Streaming (Ollama)
# ---------------------------------------------------------------------------

class ChatRequest(BaseModel):
    message: str
    current_artist: Optional[str] = None
    current_title: Optional[str] = None


def _dj_chat_system_prompt(now_artist: str, now_title: str) -> str:
    """Build the AI DJ system prompt.

    The booth must never recommend the song already on stage. A small local
    model asked to "suggest next song" happily parrots back the current track
    unless the prompt explicitly rules it out, which is why the DJ booth kept
    offering the song that was already playing.
    """
    return (
        f"You are the energetic, charismatic AI DJ and MC for Karaoke Night. "
        f"The track currently on stage is '{now_title}' by {now_artist}. "
        f"You have deep knowledge of music history, bands, side-projects, BPM, and Camelot keys. "
        f"Keep answers punchy, entertaining, and brief (2-3 sentences). "
        f"If the user asks about an artist, highlight their story or side-projects. "
        f"Answer questions about vibe, history, or recommendations without taking playback actions. "
        f"Never claim you played or queued anything. "
        f"The song '{now_title}' by {now_artist} is ALREADY PLAYING: never suggest, "
        f"recommend, or queue it. Any 'next song' you name must be a DIFFERENT track. "
        f"Only if explicitly asked to queue a track, offer [QUEUE: Artist - Track Title] "
        f"as a suggestion the user must confirm with a button. "
        f"You may use [FOCUS: Artist Name] for artist inspection."
    )


@app.post("/api/dj/chat")
async def dj_chat_endpoint(body: ChatRequest):
    """Server-Sent Events streaming chat with local Ollama model."""
    user_msg = body.message.strip()
    now_artist = body.current_artist or "Unknown Artist"
    now_title = body.current_title or "Unknown Track"

    system_prompt = _dj_chat_system_prompt(now_artist, now_title)

    ollama_payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        "stream": True,
    }

    async def chat_event_stream():
        async with httpx.AsyncClient(timeout=60.0) as client:
            try:
                async with client.stream("POST", f"{OLLAMA_URL}/api/chat", json=ollama_payload) as resp:
                    full_text = ""
                    async for chunk in resp.aiter_lines():
                        if not chunk:
                            continue
                        try:
                            item = json.loads(chunk)
                            token = item.get("message", {}).get("content", "")
                            full_text += token
                            if token:
                                yield {"event": "token", "data": json.dumps({"token": token})}
                            if item.get("done", False):
                                # Check for command triggers
                                focus_match = re.search(r"\[FOCUS:\s*([^\]]+)\]", full_text)
                                if focus_match:
                                    target_artist = focus_match.group(1).strip()
                                    yield {"event": "action", "data": json.dumps({"action": "focus_artist", "artist": target_artist})}
                                queue_match = re.search(r"\[QUEUE:\s*([^\]]+)\]", full_text)
                                if queue_match:
                                    target_track = queue_match.group(1).strip()
                                    yield {"event": "action", "data": json.dumps({"action": "queue_suggestion", "track": target_track})}
                                yield {"event": "done", "data": json.dumps({"status": "complete"})}
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                _log.warning("Ollama stream error: %s", e)
                yield {"event": "error", "data": json.dumps({"error": f"Ollama connection error: {str(e)}"})}

    return EventSourceResponse(chat_event_stream())


# ---------------------------------------------------------------------------
# Static frontend delivery
# ---------------------------------------------------------------------------

dist_dir = os.path.join(os.path.dirname(__file__), "webapp_dist")
if os.path.isdir(dist_dir):
    app.mount("/", StaticFiles(directory=dist_dir, html=True), name="static")
else:
    @app.get("/")
    def index_placeholder():
        return HTMLResponse(
            "<h3>Karaoke Unified Dashboard API is running.</h3>"
            "<p>Run <code>npm run build</code> in <code>src/karaoke/webapp</code> to build the Vue frontend.</p>"
            "<p><a href='/docs'>Swagger API Documentation</a></p>"
        )


def main():
    import uvicorn
    port = int(os.environ.get("DASHBOARD_PORT", "8900"))
    host = os.environ.get("DASHBOARD_HOST", "0.0.0.0")
    print(f"Starting Karaoke Unified Dashboard API on http://{host}:{port}")
    uvicorn.run("karaoke.dashboard_api:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
