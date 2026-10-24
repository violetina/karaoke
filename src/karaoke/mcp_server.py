"""Model Context Protocol (MCP) server for the Karaoke AI DJ & Music Library.

Exposes tools for Obot, Claude, and local LLMs (Ollama) to inspect the 18,000+
song library, track harmonic key and Camelot codes, query current desktop
playback, perform smart DJ track transitions, and trigger playback.
"""
from __future__ import annotations

import argparse
import json
import logging
from typing import Any, Optional

import uvicorn
from mcp.server import MCPServer
from mcp.server.sse import SseServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from . import localcache, musictheory, playerctl, sentiment, visuals
from .logger import log

dj_mcp = MCPServer(
    name="karaoke-dj",
    version="1.3.1",
    instructions=(
        "You are the resident AI Karaoke DJ & MC. Use these tools to query the "
        "local song library (18,000+ tracks), search by v2 chord progressions (Roman numerals "
        "like 'ii V I' or chord names like 'Am F C G'), functional harmony styles ('jazz-functional', "
        "'blues', 'andalusian', 'triadic', 'mixed') and cadences, analyze playback vibes, find "
        "harmonically compatible tracks using the Camelot wheel and BPM, manage YouTube Music-synced "
        "DJ playlists (prefer batch `track_ids` when adding multiple songs), and trigger playback. "
        "If a track lacks key/BPM, chords, CLAP vectors or lyric sync, call `scan_track` to find a source "
        "and run the analysis pipeline."
    ),
)


@dj_mcp.tool()
def search_songs(
    query: str = "",
    genre: str = "",
    key: str = "",
    min_bpm: float = 0.0,
    max_bpm: float = 300.0,
    only_synced_lyrics: bool = False,
    limit: int = 10,
    offset: int = 0,
) -> str:
    """Search tracks in the karaoke library by title, artist, genre, key (e.g. 'Am', '8A', 'C'), or BPM range.

    Args:
        query: Artist, title, or album substring match.
        genre: Genre filter, matched like the TUI's H filter: the artist's broad genre
            (blues, jazz, rock, metal, punk, folk, country, electronic, hip hop, soul,
            reggae, classical, pop, ambient, latin, world) or a specific one (e.g. 'funk').
        key: Musical key (e.g. 'A minor', 'Am') or Camelot wheel code (e.g. '8A', '8B').
        min_bpm: Minimum tempo in beats per minute.
        max_bpm: Maximum tempo in beats per minute.
        only_synced_lyrics: If true, only return tracks that have timestamped karaoke lyrics ready.
        limit: Max results to return (default 10, max 50).
        offset: Skip this many matches first, for paging through the library.
    """
    limit = max(1, min(limit, 50))
    offset = max(0, offset)
    target_camelot = key.strip().upper() if key else ""

    with localcache.connect() as conn:
        from . import chord_timeline
        try:
            chord_timeline.ensure_schema(conn)
        except Exception:
            pass
        cur = conn.cursor()
        sql = """
            SELECT t.track_id, t.artist, t.title, t.album, t.duration, t.play_count,
                   a.detected_key, a.bpm, a.energy, a.brightness, g.genre, s.url, s.kind,
                   tc.harmony,
                   EXISTS(
                       SELECT 1 FROM lyrics l
                       WHERE l.track_id = t.track_id AND l.synced_lyrics != ''
                   ) AS has_synced
            FROM tracks t
            LEFT JOIN track_analysis a ON a.track_id = t.track_id
            LEFT JOIN track_genre g ON g.track_id = t.track_id
            LEFT JOIN track_chords tc ON tc.track_id = t.track_id
            LEFT JOIN sources s ON s.source_id = (
                SELECT s2.source_id FROM sources s2
                WHERE s2.track_id = t.track_id
                ORDER BY
                    CASE
                        WHEN s2.kind = 'youtube_music' THEN 0
                        WHEN s2.kind = 'youtube' THEN 1
                        WHEN s2.url LIKE 'http%%' THEN 2
                        WHEN s2.kind = 'spotify' THEN 3
                        ELSE 4
                    END,
                    s2.source_id
                LIMIT 1
            )
        """
        where_clauses = []
        params: list[Any] = []

        if query.strip():
            pat = f"%{query.strip()}%"
            where_clauses.append("(t.artist ILIKE %s OR t.title ILIKE %s OR t.album ILIKE %s OR (t.artist || ' ' || t.title) ILIKE %s)")
            params.extend([pat, pat, pat, pat])

        if genre.strip():
            # Same match as the TUI's H genre filter: an artist's genres (broad
            # or specific) when the artist is classified, the track's own CLAP
            # genre only otherwise. CLAP alone tags Django Reinhardt "blues"
            # and Hendrix "punk rock".
            if localcache.table_exists(conn, "artist_genres"):
                where_clauses.append("""(
                    EXISTS (SELECT 1 FROM artist_genres ag
                            WHERE (lower(trim(ag.broad_genre)) = lower(trim(%s))
                                   OR lower(trim(ag.genre)) = lower(trim(%s)))
                              AND ag.artist_normalized = lower(trim(t.artist)))
                    OR (NOT EXISTS (SELECT 1 FROM artist_genres ag
                                    WHERE ag.artist_normalized = lower(trim(t.artist)))
                        AND lower(trim(COALESCE(g.genre, ''))) = lower(trim(%s))))""")
                params.extend([genre.strip()] * 3)
            else:
                where_clauses.append("lower(trim(COALESCE(g.genre, ''))) = lower(trim(%s))")
                params.append(genre.strip())

        if min_bpm > 0:
            where_clauses.append("a.bpm >= %s")
            params.append(min_bpm)

        if max_bpm < 300:
            where_clauses.append("a.bpm <= %s")
            params.append(max_bpm)

        if only_synced_lyrics:
            where_clauses.append("EXISTS (SELECT 1 FROM lyrics l WHERE l.track_id = t.track_id AND l.synced_lyrics != '')")

        if where_clauses:
            sql += f" WHERE {' AND '.join(where_clauses)}"

        sql += " ORDER BY t.play_count DESC, t.artist, t.title LIMIT %s"
        # Paging happens after the Camelot filter below, which runs in Python,
        # so the window has to cover offset as well as limit -- otherwise page
        # two of a key-filtered browse would start mid-way through page one.
        window = limit + offset
        # One extra row answers has_more without a second query. Camelot is
        # filtered in Python after the fetch, and a given key is a small slice
        # of the library, so that path needs a far wider net or later pages
        # come back empty.
        fetch_limit = max((window + 1) * 20, 200) if target_camelot else window + 1
        params.append(fetch_limit)

        cur.execute(sql, params)
        rows = cur.fetchall()

        results = []
        for r in rows:
            detected_key = r["detected_key"] or ""
            key_obj = musictheory.parse_key(detected_key) if detected_key else None
            camelot = key_obj.camelot if key_obj else "?"

            if target_camelot:
                # Match either Camelot code (8A) or key name (Am / A minor)
                matches_camelot = (camelot.upper() == target_camelot)
                matches_name = key_obj and (
                    key_obj.short.upper() == target_camelot
                    or key_obj.name.upper() == target_camelot
                )
                if not (matches_camelot or matches_name):
                    continue

            harmony_doc = r.get("harmony") if isinstance(r.get("harmony"), dict) else {}
            item: dict[str, Any] = {
                "track_id": r["track_id"],
                "artist": r["artist"],
                "title": r["title"],
                "album": r["album"] or "",
                "duration_s": round(r["duration"] or 0, 1),
                "key": detected_key,
                "camelot": camelot,
                "bpm": round(r["bpm"], 1) if r["bpm"] else None,
                "energy": round(r["energy"], 2) if r["energy"] is not None else None,
                "genre": r["genre"] or "",
                "has_synced_lyrics": bool(r["has_synced"]),
                "play_count": r["play_count"] or 0,
                "url": r["url"] or "",
            }
            if harmony_doc and harmony_doc.get("style"):
                item["harmony_style"] = harmony_doc["style"]
            results.append(item)

            if len(results) > window:
                break

        page = results[offset:offset + limit]
        return json.dumps({
            "count": len(page),
            "offset": offset,
            "has_more": len(results) > offset + limit,
            "tracks": page,
        }, indent=2)


@dj_mcp.tool()
def get_now_playing() -> str:
    """Check what song is currently playing on the desktop (Spotify, VLC, browser/YouTube, MPRIS).

    Returns track title, artist, album, player, position, and detected key/BPM/sentiment if known in library.
    """
    active_player = None
    try:
        active_player = playerctl.playing_player()
        meta = playerctl.current_metadata(active_player) if active_player else None
    except Exception:
        meta = None
    if not meta or not (meta.artist or meta.title):
        # Nothing playing on the desktop: radio mode may know what the room is
        # hearing. Checked before the paused-player fallback below, which
        # would otherwise report a stale browser tab over the live radio.
        from types import SimpleNamespace

        from . import live_state

        live = live_state.current()
        if live:
            active_player = live["source"]
            meta = SimpleNamespace(artist=live.get("artist") or "",
                                   title=live.get("title") or "", album="",
                                   url="", duration=None, player=live["source"])
    if not meta or not (meta.artist or meta.title):
        try:
            # Fallback to playerctl default or recent play
            meta = playerctl.current_metadata()
        except Exception:
            meta = None

    if meta and (meta.artist or meta.title):
        artist = meta.artist
        title = meta.title
        # Look up in library
        with localcache.connect() as conn:
            tid = None
            # If title is in "Artist - Song" format (common when browser player reports uploader channel as artist)
            if title and any(sep in title for sep in (" - ", " – ", " — ")):
                import re
                parts = re.split(r"\s*[-–—]\s*", title, maxsplit=1)
                if len(parts) == 2 and parts[0].strip() and parts[1].strip():
                    tid = localcache.find_track_id_relaxed(parts[0].strip(), parts[1].strip(), conn)
            if not tid:
                tid = localcache.find_track_id_relaxed(artist, title, conn)
            if not tid and meta.url:
                found = localcache.find_track_by_url(meta.url, conn)
                if found:
                    tid = found[0]

            cur = conn.cursor()
            if tid is not None:
                cur.execute("""
                    SELECT t.track_id, t.artist, t.title, a.detected_key, a.bpm, a.energy, g.genre
                    FROM tracks t
                    LEFT JOIN track_analysis a ON a.track_id = t.track_id
                    LEFT JOIN track_genre g ON g.track_id = t.track_id
                    WHERE t.track_id = %s
                """, (tid,))
            else:
                cur.execute("""
                    SELECT t.track_id, t.artist, t.title, a.detected_key, a.bpm, a.energy, g.genre
                    FROM tracks t
                    LEFT JOIN track_analysis a ON a.track_id = t.track_id
                    LEFT JOIN track_genre g ON g.track_id = t.track_id
                    WHERE (t.artist ILIKE %s AND t.title ILIKE %s)
                       OR ((t.artist || ' ' || t.title) ILIKE %s)
                    LIMIT 1
                """, (f"%{artist}%", f"%{title}%", f"%{artist} {title}%"))
            row = cur.fetchone()

            track_id = row["track_id"] if row else tid
            key_info = None
            if row and row["detected_key"]:
                k = musictheory.parse_key(row["detected_key"])
                key_info = {
                    "key": row["detected_key"],
                    "camelot": k.camelot if k else "?",
                    "bpm": round(row["bpm"], 1) if row["bpm"] else None,
                    "energy": round(row["energy"], 2) if row["energy"] is not None else None,
                    "genre": row["genre"] or "",
                }

            if key_info is not None and track_id:
                try:
                    from .key_progression import PROGRESSION_INDEX, doc_id
                    from .osclient import client as get_os_client
                    osc = get_os_client()
                    if osc and osc.indices.exists(index=PROGRESSION_INDEX):
                        pdoc = osc.get(index=PROGRESSION_INDEX, id=doc_id(track_id), ignore=404)
                        if pdoc.get("found"):
                            psrc = pdoc["_source"]
                            key_info["form"] = psrc.get("form")
                            key_info["fifth_ratio"] = round(psrc["fifth_ratio"], 3) if psrc.get("fifth_ratio") is not None else None
                            key_info["distinct_chords"] = psrc.get("distinct_chords")
                except Exception:
                    pass

            return json.dumps({
                "status": "playing",
                "player": meta.player or active_player or "mpris",
                "artist": row["artist"] if row else artist,
                "title": row["title"] if row else title,
                "track_id": track_id,
                "album": meta.album or "",
                "duration_s": meta.duration,
                "url": meta.url,
                "library_match": key_info,
            }, indent=2)

    # If nothing is playing right now, fetch the last played song from stats
    summary = localcache.summarize(limit=5)
    last_tracks = summary.top_tracks[:3] if summary.top_tracks else []
    return json.dumps({
        "status": "idle",
        "message": "No active media player detected playing right now.",
        "recent_popular_tracks": [
            {"artist": a, "title": t, "plays": p} for a, t, p in last_tracks
        ],
    }, indent=2)


@dj_mcp.tool()
def suggest_next_tracks(
    track_id: Optional[int] = None,
    artist: Optional[str] = None,
    title: Optional[str] = None,
    strategy: str = "flow",
    limit: int = 5,
    exclude_playlist: str = "dj-list",
) -> str:
    """Suggest the best next songs to follow a given track for smooth DJ mixing or energy flow.

    Args:
        track_id: The ID of the seed track (optional if artist & title provided).
        artist: Artist name of the current track (used if track_id is omitted).
        title: Title of the current track (used if track_id is omitted).
        exclude_playlist: Skip songs already in this playlist, so repeated calls
            keep offering fresh material. Defaults to the DJ list; pass an empty
            string to allow songs already queued.
        strategy:
            - 'flow' (default): what fits next -- same sound (CLAP timbre) and a mood that follows on from how this song ends. Measured on real radio transitions it ranks the song a DJ actually played next ~8x better than key/BPM matching.
            - 'harmonic': the best-fitting songs in a compatible Camelot key (same, +/-1 hour, relative) within +/-8% tempo (half/double time allowed).
            - 'energy_up': best-fitting songs that open more intense than this one ends (mood timeline), or faster.
            - 'cool_down': best-fitting songs that open calmer than this one ends, or slower.
            - 'mood': strongest weight on the mood handover (end of this song -> start of the next).
            - 'acoustic': Timbre / sound similarity via CLAP vector embeddings.
            - 'structural': Harmonic shape and chord progression root-motion similarity (via OpenSearch chord_motion / progression_vector k-NN).
        limit: Number of recommendations (default 5, max 20).
    """
    limit = max(1, min(limit, 20))
    seed_track = None

    with localcache.connect() as conn:
        cur = conn.cursor()
        if track_id is not None:
            cur.execute("""
                SELECT t.track_id, t.artist, t.title, a.detected_key, a.bpm, a.energy, g.genre
                FROM tracks t
                LEFT JOIN track_analysis a ON a.track_id = t.track_id
                LEFT JOIN track_genre g ON g.track_id = t.track_id
                WHERE t.track_id = %s
            """, (track_id,))
            seed_track = cur.fetchone()
        elif artist and title:
            cur.execute("""
                SELECT t.track_id, t.artist, t.title, a.detected_key, a.bpm, a.energy, g.genre
                FROM tracks t
                LEFT JOIN track_analysis a ON a.track_id = t.track_id
                LEFT JOIN track_genre g ON g.track_id = t.track_id
                WHERE t.artist ILIKE %s AND t.title ILIKE %s
                LIMIT 1
            """, (f"%{artist}%", f"%{title}%"))
            seed_track = cur.fetchone()

        if not seed_track:
            cur.execute("""
                SELECT t.track_id, t.artist, t.title, a.detected_key, a.bpm, a.energy, g.genre
                FROM tracks t
                JOIN track_analysis a ON a.track_id = t.track_id
                LEFT JOIN track_genre g ON g.track_id = t.track_id
                WHERE a.bpm IS NOT NULL
                ORDER BY t.play_count DESC, t.track_id ASC
                LIMIT 1
            """)
            seed_track = cur.fetchone()

        if not seed_track:
            return json.dumps({
                "error": "Could not find seed track in library. Provide a valid track_id or artist/title.",
            })

        seed_id = seed_track["track_id"]
        seed_bpm = seed_track["bpm"] or 120.0
        seed_key_str = seed_track["detected_key"] or ""
        seed_key = musictheory.parse_key(seed_key_str) if seed_key_str else None
        seed_camelot = seed_key.camelot if seed_key else "?"

        candidates = []

        # Songs already queued are not suggestions. Without this a DJ working
        # through /suggest keeps being handed the same tracks they just added,
        # and `/queue all` would append duplicates. Read locally: this runs on
        # every suggestion, so it must not depend on YouTube Music being
        # reachable.
        excluded: set[int] = set()
        if exclude_playlist:
            try:
                excluded = {
                    t["track_id"]
                    for t in localcache.get_saved_playlist_tracks(exclude_playlist, conn=conn)
                    if t.get("track_id")
                }
            except Exception as exc:
                log.warning("Could not read playlist %s for exclusion: %s", exclude_playlist, exc)

        # Fit-based ranking (karaoke.dj_suggest): sound + mood handover, with
        # the strategy as the constraint. Falls through to the older paths
        # below when it finds nothing (e.g. a seed without analysis).
        new_strategy = {"chords": "structural", "chord_progression": "structural",
                        "feeling": "mood", "sentiment": "mood"}.get(strategy, strategy)
        fell_back_from_flow = False
        if new_strategy in ("flow", "harmonic", "energy_up", "cool_down", "mood", "structural"):
            from . import dj_suggest
            try:
                ranked = dj_suggest.suggest(seed_id, new_strategy, limit=limit,
                                            excluded=excluded, conn=conn)
            except Exception as exc:
                log.warning("dj_suggest failed for %s (%s); using the older path", seed_id, exc)
                ranked = []
            if ranked:
                return json.dumps({
                    "seed": {
                        "track_id": seed_id,
                        "artist": seed_track["artist"],
                        "title": seed_track["title"],
                        "key": seed_key_str,
                        "camelot": seed_camelot,
                        "bpm": round(seed_bpm, 1),
                    },
                    "strategy": new_strategy,
                    "suggestions": ranked,
                }, indent=2)
            if strategy == "flow":
                strategy = "harmonic"
                fell_back_from_flow = True

        if strategy in ("structural", "chords", "chord_progression"):
            from . import search
            raw = search.similar_chords_for_track(seed_id, k=limit + len(excluded))
            suggestions = [s for s in raw if s["track_id"] not in excluded][:limit]
            payload = {
                "seed": {
                    "track_id": seed_id,
                    "artist": seed_track["artist"],
                    "title": seed_track["title"],
                    "key": seed_key_str,
                    "camelot": seed_camelot,
                    "bpm": seed_bpm,
                },
                "strategy": "structural_chord_motion",
                "suggestions": suggestions,
            }
            if not suggestions:
                payload["note"] = (
                    f"No structural chord suggestions found. The seed track '{seed_track['artist']} - {seed_track['title']}' "
                    f"may lack chord analysis. Run scan_track(track_id={seed_id}) to analyse chords."
                )
            return json.dumps(payload, indent=2)

        if strategy in ("context", "lyrics", "meaning"):
            from . import search
            raw = search.similar_lyrics_for_track(seed_id, k=limit + len(excluded))
            suggestions = [s for s in raw if s["track_id"] not in excluded][:limit]
            payload = {
                "seed": {
                    "track_id": seed_id,
                    "artist": seed_track["artist"],
                    "title": seed_track["title"],
                    "key": seed_key_str,
                    "camelot": seed_camelot,
                    "bpm": seed_bpm,
                },
                "strategy": "context_lyrics_semantic",
                "suggestions": suggestions,
            }
            if not suggestions:
                payload["note"] = (
                    f"No semantic lyric suggestions found. The seed track '{seed_track['artist']} - {seed_track['title']}' "
                    f"may lack synced lyrics or text embeddings. Run scan_track(track_id={seed_id}) to resolve."
                )
            return json.dumps(payload, indent=2)

        if strategy in ("mood", "feeling", "sentiment"):
            from . import search
            raw = search.similar_mood_for_track(seed_id, k=limit + len(excluded))
            suggestions = [s for s in raw if s["track_id"] not in excluded][:limit]
            payload = {
                "seed": {
                    "track_id": seed_id,
                    "artist": seed_track["artist"],
                    "title": seed_track["title"],
                    "key": seed_key_str,
                    "camelot": seed_camelot,
                    "bpm": seed_bpm,
                },
                "strategy": "mood_emotion_sentiment",
                "suggestions": suggestions,
            }
            if not suggestions:
                payload["note"] = (
                    f"No mood suggestions found. The seed track '{seed_track['artist']} - {seed_track['title']}' "
                    f"may lack a mood timeline or sentiment scores. Run scan_track(track_id={seed_id}) to analyse mood."
                )
            return json.dumps(payload, indent=2)

        if strategy == "acoustic":
            from . import queue_suggest
            # Over-fetch so filtering cannot leave us short of `limit`.
            raw = queue_suggest.suggest_for_track(seed_id, limit=limit + len(excluded))
            suggestions = [s for s in raw if s.track_id not in excluded][:limit]
            payload = {
                "seed": {
                    "track_id": seed_id,
                    "artist": seed_track["artist"],
                    "title": seed_track["title"],
                    "key": seed_key_str,
                    "camelot": seed_camelot,
                    "bpm": seed_bpm,
                },
                "strategy": "acoustic_vector",
                "suggestions": [
                    {
                        "track_id": s.track_id,
                        "artist": s.artist,
                        "title": s.title,
                        "score": round(s.score, 3),
                        "similarity_space": s.space,
                    }
                    for s in suggestions
                ],
            }
            if not suggestions:
                payload["note"] = (
                    f"No acoustic suggestions found. The seed track '{seed_track['artist']} - {seed_track['title']}' "
                    f"may lack a CLAP audio embedding. Run scan_track(track_id={seed_id}) to compute timbre vectors."
                )
            return json.dumps(payload, indent=2)

        # Harmonic or Energy transition
        # Determine compatible Camelot keys if seed_camelot is known
        compatible_camelots = set()
        if seed_camelot and seed_camelot != "?":
            num = int(seed_camelot[:-1])
            letter = seed_camelot[-1]
            other_letter = "B" if letter == "A" else "A"
            compatible_camelots.add(f"{num}{letter}")          # Same key
            compatible_camelots.add(f"{num}{other_letter}")    # Relative major/minor
            compatible_camelots.add(f"{(num % 12) + 1}{letter}")   # +1 hour
            compatible_camelots.add(f"{((num - 2) % 12) + 1}{letter}") # -1 hour

        bpm_filter_sql = ""
        params: list[Any] = [seed_id]

        if strategy == "energy_up":
            bpm_filter_sql = "AND a.bpm >= %s AND a.bpm <= %s"
            params.extend([seed_bpm + 3.0, seed_bpm + 30.0])
            order_sql = "ORDER BY a.bpm ASC, a.energy DESC"
        elif strategy == "cool_down":
            bpm_filter_sql = "AND a.bpm <= %s AND a.bpm >= %s"
            params.extend([seed_bpm - 3.0, max(50.0, seed_bpm - 35.0)])
            order_sql = "ORDER BY a.bpm DESC, a.energy ASC"
        else:  # harmonic
            bpm_filter_sql = "AND a.bpm >= %s AND a.bpm <= %s"
            params.extend([seed_bpm * 0.88, seed_bpm * 1.12])
            order_sql = "ORDER BY ABS(a.bpm - %s) ASC"
            params.append(seed_bpm)

        query_sql = f"""
            SELECT t.track_id, t.artist, t.title, a.detected_key, a.bpm, a.energy, g.genre,
                   EXISTS(
                       SELECT 1 FROM lyrics l
                       WHERE l.track_id = t.track_id AND l.synced_lyrics != ''
                   ) AS has_synced
            FROM tracks t
            JOIN track_analysis a ON a.track_id = t.track_id
            LEFT JOIN track_genre g ON g.track_id = t.track_id
            WHERE t.track_id != %s
              AND a.detected_key IS NOT NULL
              {bpm_filter_sql}
            {order_sql}
            LIMIT 60
        """
        cur.execute(query_sql, params)
        pool = cur.fetchall()

        for row in pool:
            if row["track_id"] in excluded:
                continue

            k = musictheory.parse_key(row["detected_key"])
            cam = k.camelot if k else "?"

            is_harmonic = cam in compatible_camelots if compatible_camelots else True
            if strategy == "harmonic" and not is_harmonic:
                continue

            reason = []
            if is_harmonic and compatible_camelots:
                if cam == seed_camelot:
                    reason.append(f"Identical key {cam}")
                elif cam[-1] != seed_camelot[-1]:
                    reason.append(f"Relative key {cam}")
                else:
                    reason.append(f"Adjacent key {cam}")

            tempo_diff = round((row["bpm"] - seed_bpm), 1)
            sign = "+" if tempo_diff >= 0 else ""
            reason.append(f"BPM {round(row['bpm'], 1)} ({sign}{tempo_diff})")

            candidates.append({
                "track_id": row["track_id"],
                "artist": row["artist"],
                "title": row["title"],
                "key": row["detected_key"],
                "camelot": cam,
                "bpm": round(row["bpm"], 1),
                "energy": round(row["energy"], 2) if row["energy"] is not None else None,
                "has_synced_lyrics": bool(row["has_synced"]),
                "reason": " • ".join(reason),
            })

            if len(candidates) >= limit:
                break

        out = {
            "seed": {
                "track_id": seed_id,
                "artist": seed_track["artist"],
                "title": seed_track["title"],
                "key": seed_key_str,
                "camelot": seed_camelot,
                "bpm": round(seed_bpm, 1),
            },
            "strategy": strategy,
            "suggestions": candidates,
        }
        if fell_back_from_flow:
            out["note"] = (
                f"Seed track '{seed_track['artist']} - {seed_track['title']}' lacks CLAP timbre embedding or mood timeline "
                f"required for 'flow'; fell back to harmonic BPM/key matching. Run scan_track(track_id={seed_id}) to fully analyse."
            )
        elif not seed_key_str or seed_track["bpm"] is None:
            out["note"] = (
                f"Seed track '{seed_track['artist']} - {seed_track['title']}' lacks detected key/BPM analysis. "
                f"Run scan_track(track_id={seed_id}) to analyse audio."
            )
        return json.dumps(out, indent=2)


@dj_mcp.tool()
def analyze_lyric_vibe(track_id: int) -> str:
    """Analyze the lyric mood, emotional arc, and singing tips for a track.

    Args:
        track_id: The ID of the track in the library.
    """
    with localcache.connect() as conn:
        lyrics = localcache.get_lyrics_by_track_id(track_id, conn)
        cur = conn.cursor()
        cur.execute("""
            SELECT t.artist, t.title, a.detected_key, a.bpm, a.energy
            FROM tracks t
            LEFT JOIN track_analysis a ON a.track_id = t.track_id
            WHERE t.track_id = %s
        """, (track_id,))
        t_row = cur.fetchone()

        if not t_row:
            return json.dumps({"error": f"Track {track_id} not found."})

        lyrics_text = lyrics.plain if lyrics else ""
        if not lyrics_text and lyrics and lyrics.synced_lyrics:
            lyrics_text = "\n".join(t for _, t in lyrics.lines)

        if not lyrics_text and t_row:
            try:
                from . import lyrics as lyrics_mod
                fetched = lyrics_mod.fetch_lrclib(t_row["artist"], t_row["title"])
                if fetched:
                    localcache.save_lyrics(track_id, fetched, conn)
                    lyrics = fetched
                    lyrics_text = lyrics.plain or "\n".join(t for _, t in lyrics.lines)
            except Exception:
                pass

        profile = visuals.analyze_sentiment(lyrics_text or "")
        arc = visuals.sentiment_arc(profile, width=20)
        bars = visuals.sentiment_bars(profile, width=10)

        key_obj = musictheory.parse_key(t_row["detected_key"]) if t_row["detected_key"] else None

        # Build singing tips
        tips = []
        if profile.dominant == "angry":
            tips.append("High-aggression vocal delivery — belt the chorus with grit!")
        elif profile.dominant == "tender":
            tips.append("Warm, emotive ballad vibe — focus on soft resonance and breath control.")
        elif profile.dominant == "happy":
            tips.append("Upbeat crowd-pleaser — get the room clapping along!")
        elif profile.dominant == "sad":
            tips.append("Moody, introspective track — lean into the emotional melancholy.")
        else:
            tips.append("Balanced neutral mood — let the rhythm drive the vocal cadence.")

        if t_row["bpm"] and t_row["bpm"] > 140:
            tips.append(f"Fast tempo ({round(t_row['bpm'])} BPM) — watch for rapid lyrical pacing.")
        elif t_row["bpm"] and t_row["bpm"] < 90:
            tips.append(f"Slow tempo ({round(t_row['bpm'])} BPM) — hold sustained notes cleanly.")

        return json.dumps({
            "track_id": track_id,
            "artist": t_row["artist"],
            "title": t_row["title"],
            "key": t_row["detected_key"],
            "key_character": key_obj.character if key_obj else "unknown",
            "dominant_mood": profile.dominant,
            "sentiment_counts": profile.counts,
            "has_synced_lyrics": lyrics.has_synced if lyrics else False,
            "line_count": len(lyrics.lines) if lyrics else 0,
            "ascii_arc": arc,
            "ascii_bars": bars,
            "dj_singer_tips": tips,
        }, indent=2)


@dj_mcp.tool()
def get_dj_stats() -> str:
    """Get overall library summary, crowd top-favorites, and playback statistics.

    Useful for understanding what the room has been listening to and picking sure-fire crowd pleasers.
    """
    summary = localcache.summarize(limit=10)
    return json.dumps({
        "total_events": summary.total_events,
        "plays": summary.plays,
        "discoveries": summary.discoveries,
        "cache_hit_rate": f"{round(summary.cache_hit_rate * 100, 1)}%",
        "distinct_tracks": summary.distinct_tracks,
        "distinct_artists": summary.distinct_artists,
        "top_tracks": [
            {"artist": a, "title": t, "plays": p} for a, t, p in summary.top_tracks
        ],
        "top_artists": [
            {"artist": a, "plays": p} for a, p in summary.top_artists
        ],
        "playback_modes": dict(summary.by_mode),
    }, indent=2)


@dj_mcp.tool()
def play_track(track_id: int, prefer_audio_only: bool = False) -> str:
    """Trigger playback of a track on the host session via the Karaoke player.

    Args:
        track_id: The ID of the track to play.
        prefer_audio_only: If true, opens audio source directly; otherwise launches full karaoke experience.
    """
    with localcache.connect() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT t.artist, t.title, s.url, s.kind
            FROM tracks t
            LEFT JOIN sources s ON s.source_id = (
                SELECT s2.source_id FROM sources s2
                WHERE s2.track_id = t.track_id
                ORDER BY
                    CASE
                        WHEN s2.kind = 'youtube_music' THEN 0
                        WHEN s2.kind = 'youtube' THEN 1
                        WHEN s2.url LIKE 'http%%' THEN 2
                        WHEN s2.kind = 'spotify' THEN 3
                        ELSE 4
                    END,
                    s2.source_id
                LIMIT 1
            )
            WHERE t.track_id = %s
        """, (track_id,))
        row = cur.fetchone()

        if not row:
            return json.dumps({"error": f"Track {track_id} not found."})

        artist = row["artist"]
        title = row["title"]
        url = row["url"]
        kind = row["kind"]

        # Call the local control API or open song URL
        from .player_open import open_song_url
        target = url or f"{artist} - {title}"
        # A pid comes back only from the xdg-open path; the kiosk/CDP path
        # returns None on success too, so absence of a pid is not a failure.
        pid = open_song_url(
            url or "",
            kind,
            artist=artist,
            title=title,
            prefer_audio=prefer_audio_only,
        )

        return json.dumps({
            "status": "launched",
            "pid": pid,
            "track_id": track_id,
            "artist": artist,
            "title": title,
            "target": target,
            "karaoke_cli_command": f'karaoke "{artist} - {title}"',
        }, indent=2)


@dj_mcp.tool()
def play_playlist(
    playlist_id: str = "dj-list",
    start_position: int = 1,
    prefer_audio_only: bool = True,
) -> str:
    """Play a whole DJ or saved playlist in YouTube Music, starting at a given song.

    Opens a watch URL carrying the playlist (`watch?v=<video>&list=<PL...>`), so
    playback starts immediately and YouTube Music advances through the rest. A
    bare playlist page would only open, not play.

    Args:
        playlist_id: Saved playlist ID or slug (default 'dj-list', or e.g. 'funny-happy').
        start_position: 1-based position of the song to start with (default 1).
        prefer_audio_only: If true, opens the audio player rather than the full karaoke view.
    """
    playlist = json.loads(get_dj_playlist(playlist_id=playlist_id))
    tracks = playlist.get("tracks") or []
    if not tracks:
        return json.dumps({"error": f"Playlist '{playlist_id}' has no tracks."})
    if not 1 <= start_position <= len(tracks):
        return json.dumps({
            "error": f"start_position must be between 1 and {len(tracks)}.",
        })

    start = tracks[start_position - 1]
    yt_pid = playlist.get("playlist_id") or ""
    video_id = start.get("video_id") or ""
    if not video_id:
        return json.dumps({
            "error": (
                f"Track at position {start_position} has no YouTube video id; "
                "use play_track for a single song."
            ),
            "track_id": start.get("track_id"),
        })

    url = f"https://music.youtube.com/watch?v={video_id}"
    if yt_pid.startswith(("PL", "VL", "RD", "OLAK5")):
        url += f"&list={yt_pid}"

    from .player_open import open_song_url
    pid = open_song_url(
        url,
        "youtube_music",
        artist=start.get("artist") or "",
        title=start.get("title") or "",
        prefer_audio=prefer_audio_only,
    )

    return json.dumps({
        "status": "launched",
        "pid": pid,
        "playlist_id": yt_pid,
        "name": playlist.get("name"),
        "track_count": len(tracks),
        "start_position": start_position,
        "artist": start.get("artist"),
        "title": start.get("title"),
        "url": url,
    }, indent=2)


@dj_mcp.tool()
def add_to_dj_playlist(
    track_id: Optional[int] = None,
    artist: Optional[str] = None,
    title: Optional[str] = None,
    playlist_id: str = "dj-list",
    track_ids: Optional[list[int]] = None,
    playlist_name: Optional[str] = None,
) -> str:
    """Add one or more songs to a DJ playlist (default 'Karaoke: DJ List' or any custom playlist_id), syncing with YouTube Music.

    Args:
        track_id: Specific track ID from the karaoke library.
        artist: Artist name (used if track_id and track_ids are omitted).
        title: Title of song (used if track_id and track_ids are omitted).
        playlist_id: Identifier of target saved playlist (default 'dj-list', or a custom slug like 'funny-happy').
        track_ids: Optional list of track IDs to add in a single batch call (much faster than calling once per song).
        playlist_name: Optional human-readable playlist title when creating a custom playlist (defaults to 'Karaoke: <Title>').
    """
    batch_ids = [int(t) for t in (track_ids or []) if t is not None]
    if track_id is not None and int(track_id) not in batch_ids:
        batch_ids.insert(0, int(track_id))

    if not batch_ids and not (artist or title):
        return json.dumps({"error": "Either track_id, track_ids, or artist/title must be provided."})

    with localcache.connect() as conn:
        cur = conn.cursor()
        rows = []
        if batch_ids:
            for tid in batch_ids:
                cur.execute("""
                    SELECT t.track_id, t.artist, t.title, a.detected_key, a.bpm, s.url, s.kind
                    FROM tracks t
                    LEFT JOIN track_analysis a ON a.track_id = t.track_id
                    LEFT JOIN sources s ON s.source_id = (
                        SELECT s2.source_id FROM sources s2
                        WHERE s2.track_id = t.track_id
                        ORDER BY
                            CASE
                                WHEN s2.kind = 'youtube_music' THEN 0
                                WHEN s2.kind = 'youtube' THEN 1
                                WHEN s2.url LIKE 'http%%' THEN 2
                                WHEN s2.kind = 'spotify' THEN 3
                                ELSE 4
                            END,
                            s2.source_id
                        LIMIT 1
                    )
                    WHERE t.track_id = %s
                """, (tid,))
                r = cur.fetchone()
                if r:
                    rows.append(r)
        else:
            pat_artist = f"%{artist.strip()}%" if artist else "%"
            pat_title = f"%{title.strip()}%" if title else "%"
            cur.execute("""
                SELECT t.track_id, t.artist, t.title, a.detected_key, a.bpm, s.url, s.kind
                FROM tracks t
                LEFT JOIN track_analysis a ON a.track_id = t.track_id
                LEFT JOIN sources s ON s.source_id = (
                    SELECT s2.source_id FROM sources s2
                    WHERE s2.track_id = t.track_id
                    ORDER BY
                        CASE
                            WHEN s2.kind = 'youtube_music' THEN 0
                            WHEN s2.kind = 'youtube' THEN 1
                            WHEN s2.url LIKE 'http%%' THEN 2
                            WHEN s2.kind = 'spotify' THEN 3
                            ELSE 4
                        END,
                        s2.source_id
                    LIMIT 1
                )
                WHERE t.artist ILIKE %s AND t.title ILIKE %s
                ORDER BY t.play_count DESC
                LIMIT 1
            """, (pat_artist, pat_title))
            r = cur.fetchone()
            if r:
                rows.append(r)

        if not rows:
            return json.dumps({"error": f"Track not found in library (track_id={track_id}, track_ids={track_ids}, artist={artist}, title={title})."})

        # Resolve real YouTube Music playlist (for default 'dj-list' AND any custom playlist slug)
        from . import ytmusic_playlist
        display_name = ytmusic_playlist.format_playlist_display_name(playlist_id, playlist_name)
        yt_pid = playlist_id
        yt_url = ""
        try:
            if playlist_id in ("dj-list", "Karaoke: DJ List") and not playlist_name:
                yt_pid, yt_url = ytmusic_playlist.resolve_or_create_dj_playlist(conn=conn)
            else:
                yt_pid, yt_url = ytmusic_playlist.resolve_or_create_dj_playlist(
                    name=display_name,
                    conn=conn,
                    playlist_key=playlist_id,
                )
        except Exception:
            yt_pid = playlist_id

        # Ensure playlist exists locally with its proper display name
        localcache.ensure_dj_playlist(playlist_id=yt_pid, name=display_name, conn=conn)

        existing_by_tid = {
            existing.get("track_id"): existing
            for existing in localcache.get_saved_playlist_tracks(yt_pid, conn=conn)
            if existing.get("track_id") is not None
        }

        added_tracks = []
        last_pos = 0
        already_present_all = True

        for row in rows:
            if row["track_id"] in existing_by_tid:
                ex = existing_by_tid[row["track_id"]]
                last_pos = ex.get("position") or last_pos
                key_obj = musictheory.parse_key(row["detected_key"]) if row["detected_key"] else None
                added_tracks.append({
                    "track_id": row["track_id"],
                    "artist": row["artist"],
                    "title": row["title"],
                    "key": row["detected_key"] or "",
                    "camelot": key_obj.camelot if key_obj else "?",
                    "bpm": round(row["bpm"], 1) if row["bpm"] else None,
                    "position": ex.get("position"),
                    "already_present": True,
                })
                continue

            already_present_all = False
            url = row["url"] or ""
            vid = localcache.extract_youtube_id(url) if url else ""

            if yt_pid.startswith(("PL", "VL", "RD", "OLAK5")):
                try:
                    ok, res_vid = ytmusic_playlist.add_track_to_ytmusic_playlist(yt_pid, row["artist"], row["title"], video_id=vid)
                    if ok and res_vid and not vid:
                        vid = res_vid
                        url = f"https://music.youtube.com/watch?v={vid}"
                except Exception as exc:
                    log.warning("Could not sync track to YouTube Music playlist %s: %s", yt_pid, exc)

            track_payload = {
                "track_id": row["track_id"],
                "artist": row["artist"],
                "title": row["title"],
                "video_id": vid,
                "url": url,
            }
            pos = localcache.append_playlist_track(yt_pid, track_payload, conn=conn)
            last_pos = pos
            existing_by_tid[row["track_id"]] = {"track_id": row["track_id"], "position": pos}
            key_obj = musictheory.parse_key(row["detected_key"]) if row["detected_key"] else None
            added_tracks.append({
                "track_id": row["track_id"],
                "artist": row["artist"],
                "title": row["title"],
                "key": row["detected_key"] or "",
                "camelot": key_obj.camelot if key_obj else "?",
                "bpm": round(row["bpm"], 1) if row["bpm"] else None,
                "url": url,
                "position": pos,
            })

        resolved_url = yt_url or (f"https://music.youtube.com/playlist?list={yt_pid}" if yt_pid.startswith("PL") else "")
        if len(rows) == 1 and already_present_all:
            first = added_tracks[0]
            return json.dumps({
                "ok": True,
                "already_present": True,
                "playlist_id": yt_pid,
                "playlist_name": display_name,
                "playlist_url": resolved_url,
                "position": first.get("position"),
                "track": {
                    "track_id": first["track_id"],
                    "artist": first["artist"],
                    "title": first["title"],
                },
                "message": f"{first['artist']} — {first['title']} is already in the playlist.",
            }, indent=2)

        out: dict[str, Any] = {
            "ok": True,
            "playlist_id": yt_pid,
            "playlist_name": display_name,
            "playlist_url": resolved_url,
            "position": last_pos,
            "track": added_tracks[0],
        }
        if len(added_tracks) > 1 or track_ids is not None:
            out["added_count"] = sum(1 for t in added_tracks if not t.get("already_present"))
            out["tracks"] = added_tracks
        return json.dumps(out, indent=2)


@dj_mcp.tool()
def get_dj_playlist(playlist_id: str = "dj-list") -> str:
    """Retrieve all songs in a DJ or saved playlist with position, metadata, and YouTube Music URL.

    Args:
        playlist_id: The saved playlist ID or slug (default 'dj-list', or e.g. 'funny-happy').
    """
    with localcache.connect() as conn:
        from . import ytmusic_playlist
        yt_pid = playlist_id
        yt_url = ""
        if playlist_id in ("dj-list", "Karaoke: DJ List"):
            try:
                yt_pid, yt_url = ytmusic_playlist.resolve_or_create_dj_playlist(conn=conn)
            except Exception:
                yt_pid = "dj-list"

        pl = localcache.find_saved_playlist_by_id(yt_pid, conn=conn)
        if not pl and playlist_id in ("dj-list", "Karaoke: DJ List"):
            pl = localcache.find_saved_playlist_by_id("dj-list", conn=conn)
        if pl and pl.get("playlist_id"):
            yt_pid = pl["playlist_id"]

        tracks = localcache.get_saved_playlist_tracks(yt_pid, conn=conn)
        if not tracks and playlist_id in ("dj-list", "Karaoke: DJ List") and yt_pid != "dj-list":
            tracks = localcache.get_saved_playlist_tracks("dj-list", conn=conn)

    default_name = ytmusic_playlist.format_playlist_display_name(playlist_id)
    return json.dumps({
        "playlist_id": yt_pid,
        "name": (pl.get("name") if pl else "") or default_name,
        "url": yt_url or (pl.get("url") if pl else "") or (f"https://music.youtube.com/playlist?list={yt_pid}" if yt_pid.startswith("PL") else ""),
        "track_count": len(tracks),
        "tracks": tracks,
    }, indent=2)


@dj_mcp.tool()
def list_playlists(source_kind: str = "", limit: int = 30) -> str:
    """List saved playlists in the karaoke library (DJ lists, custom playlists, smart playlists, YouTube Music playlists).

    Args:
        source_kind: Optional filter by kind ('dj', 'smart', 'youtube_music', 'graph').
        limit: Max playlists to return (default 30, max 100).
    """
    limit = max(1, min(limit, 100))
    with localcache.connect() as conn:
        playlists = localcache.get_saved_playlists(limit=limit, conn=conn)
        if source_kind.strip():
            sk = source_kind.strip().lower()
            playlists = [p for p in playlists if (p.get("source_kind") or "").lower() == sk]
    return json.dumps({
        "count": len(playlists),
        "playlists": playlists,
    }, indent=2)


@dj_mcp.tool()
def clear_dj_playlist(playlist_id: str = "dj-list") -> str:
    """Empty all tracks from a DJ playlist locally and on YouTube Music.

    Args:
        playlist_id: The saved playlist ID to clear (default 'dj-list').
    """
    with localcache.connect() as conn:
        yt_pid = playlist_id
        if playlist_id in ("dj-list", "Karaoke: DJ List"):
            try:
                from . import ytmusic_playlist
                yt_pid, _ = ytmusic_playlist.resolve_or_create_dj_playlist(conn=conn)
            except Exception:
                yt_pid = "dj-list"
        else:
            pl = localcache.find_saved_playlist_by_id(playlist_id, conn=conn)
            if pl and pl.get("playlist_id"):
                yt_pid = pl["playlist_id"]

        if yt_pid.startswith(("PL", "VL", "RD", "OLAK5")):
            try:
                from . import ytmusic_playlist
                ytmusic_playlist.clear_ytmusic_playlist(yt_pid)
            except Exception as exc:
                log.warning("Failed to clear remote YouTube Music playlist %s: %s", yt_pid, exc)

        localcache.clear_saved_playlist_tracks(yt_pid, conn=conn)
        if yt_pid != playlist_id:
            localcache.clear_saved_playlist_tracks(playlist_id, conn=conn)

    return json.dumps({
        "ok": True,
        "playlist_id": yt_pid,
        "message": f"Cleared all tracks from playlist '{yt_pid}'.",
    }, indent=2)


_PROTECTED_DJ_NAMES = frozenset({"dj-list", "karaoke: dj list", "karaoke ai dj list"})


@dj_mcp.tool()
def delete_playlist(playlist_id: str, delete_remote_ytmusic: bool = False) -> str:
    """Delete a saved playlist from the local database (and optionally from YouTube Music).

    Args:
        playlist_id: The saved playlist ID or name to delete (e.g. 'PL...', 'graph-test', 'funny-happy').
        delete_remote_ytmusic: If True and playlist_id is a YouTube Music playlist ('PL...'), also delete it on YouTube Music.
    """
    pid = (playlist_id or "").strip()
    if not pid:
        return json.dumps({"error": "playlist_id is required."}, indent=2)
    if pid.lower() in _PROTECTED_DJ_NAMES:
        return json.dumps({
            "error": "Refusing to delete the primary 'dj-list' playlist. Use clear_dj_playlist to empty it instead.",
        }, indent=2)

    with localcache.connect() as conn:
        pl = localcache.find_saved_playlist_by_id(pid, conn=conn)
        target_id = pl["playlist_id"] if pl and pl.get("playlist_id") else pid
        target_name = (pl.get("name") if pl else "") or pid

        if (
            target_id.lower() in _PROTECTED_DJ_NAMES
            or target_name.strip().lower() in _PROTECTED_DJ_NAMES
        ):
            return json.dumps({
                "error": "Refusing to delete the primary 'dj-list' playlist. Use clear_dj_playlist to empty it instead.",
            }, indent=2)

        remote_deleted = False
        if delete_remote_ytmusic and target_id.startswith(("PL", "VL")):
            try:
                from . import ytmusic_playlist
                yt = ytmusic_playlist.YTMusicClient()
                yt.delete_playlist(target_id)
                remote_deleted = True
            except Exception as exc:
                log.warning("Failed to delete remote YouTube Music playlist %s: %s", target_id, exc)

        deleted = localcache.delete_saved_playlist(target_id, conn=conn)
        if target_id != pid:
            deleted = localcache.delete_saved_playlist(pid, conn=conn) or deleted

    return json.dumps({
        "ok": bool(deleted),
        "playlist_id": target_id,
        "name": target_name,
        "remote_deleted": remote_deleted,
        "message": (
            f"Deleted playlist '{target_name}' ({target_id})."
            if deleted
            else f"Playlist '{pid}' was not found."
        ),
    }, indent=2)


_NOTE_TO_PITCH_CLASS: dict[str, int] = {
    "C": 0, "C#": 1, "Db": 1, "D": 2, "D#": 3, "Eb": 3,
    "E": 4, "F": 5, "F#": 6, "Gb": 6, "G": 7, "G#": 8,
    "Ab": 8, "A": 9, "A#": 10, "Bb": 10, "B": 11,
}

_CHORD_SUFFIX_TO_QUALITY: dict[str, str] = {
    "": "maj",
    "maj": "maj",
    "m": "min",
    "min": "min",
    "m7": "min7",
    "min7": "min7",
    "m6": "m6",
    "dim": "dim",
    "dim7": "dim7",
    "m7b5": "m7b5",
    "sus4": "sus4",
    "7": "7",
    "maj7": "maj7",
    "6": "6",
    "6/9": "6/9",
    "9": "9",
}


def _parse_chord_name(chord_tok: str) -> Optional[tuple[int, str]]:
    """Parse a chord token into (pitch_class 0..11, canonical quality)."""
    import re
    m = re.match(
        r"^([A-G][#b]?)(m7b5|dim7|dim|maj7|min7|m7|m6|6/9|sus4|maj|min|m|7|6|9)?$",
        chord_tok.strip(),
    )
    if not m:
        return None
    root_str, raw_qual = m.group(1), m.group(2) or ""
    root_pc = _NOTE_TO_PITCH_CLASS.get(root_str)
    qual = _CHORD_SUFFIX_TO_QUALITY.get(raw_qual)
    if root_pc is None or qual is None:
        return None
    return root_pc, qual


def _parse_roman_token(roman_tok: str) -> Optional[tuple[str, str]]:
    """Parse a Roman numeral token into (base_numeral, suffix), preserving case."""
    import re
    m = re.match(r"^(b?[iIvV]+)(maj7|6/9|sus4|ø7|°7|°|7|6|9)?$", roman_tok.strip())
    if not m:
        return None
    return m.group(1), m.group(2) or ""


def _roman_token_matches(query_tok: str, actual_roman: str) -> bool:
    """Strict functional Roman numeral matching.

    - Explicit extensions in query_tok (e.g. 'ii7', 'V7', 'Imaj7', 'I7', 'ii6') require exact match.
    - Bare uppercase major degrees ('I', 'IV', 'bVII', etc., except 'V') match major-tonic
      qualities ('', 'maj7', '6', '6/9') — NEVER dominant '7'/'9' (so D7/I7 never counts as I).
    - Bare 'V' matches triad 'V' or dominant 'V7'/'V9' — NEVER 'V6' or 'Vmaj7'.
    - Bare 'i' (minor tonic) matches 'i', 'i7', 'i6' — NEVER diminished/half-diminished.
    - Bare non-tonic minor degrees ('ii', 'iii', 'iv', 'v', 'vi', 'vii') match '', '7'
      (min, min7) — NEVER '6' (m6) or diminished/half-diminished (so Em6/ii6 never counts as ii).
    """
    q_parsed = _parse_roman_token(query_tok)
    r_parsed = _parse_roman_token(actual_roman)
    if not q_parsed or not r_parsed:
        return False
    q_num, q_suf = q_parsed
    r_num, r_suf = r_parsed
    if q_num != r_num:
        return False
    if q_suf:
        return r_suf == q_suf

    # Bare Roman numeral: enforce functional quality family
    if q_num == "V":
        return r_suf in ("", "7", "9")
    is_upper = q_num[1].isupper() if q_num.startswith("b") else q_num[0].isupper()
    if is_upper:
        return r_suf in ("", "maj7", "6", "6/9")
    if q_num == "i":
        return r_suf in ("", "7", "6")
    return r_suf in ("", "7")


def _chord_token_matches(query_tok: str, actual_name: str) -> bool:
    """Strict functional chord-name matching.

    - Explicit extensions ('D7', 'Dm7', 'Cmaj7', 'Em6', 'Bm7b5') require exact root+quality match.
    - Bare major chord ('D', 'C') matches major-tonic qualities ('maj', 'maj7', '6', '6/9')
      — NEVER dominant '7' or '9' (so 'D7' never matches 'D').
    - Bare minor chord ('Dm', 'Em') matches 'min' and 'min7' — NEVER 'm6' (so 'Em6' never matches 'Em').
    - Bare diminished chord ('Bdim') matches 'dim', 'dim7', 'm7b5'.
    """
    import re
    q_parsed = _parse_chord_name(query_tok)
    c_parsed = _parse_chord_name(actual_name)
    if not q_parsed or not c_parsed:
        return query_tok.strip().lower() == actual_name.strip().lower()
    q_root, q_qual = q_parsed
    c_root, c_qual = c_parsed
    if q_root != c_root:
        return False

    # Check if query specified an explicit extension beyond bare triad ('', 'm', 'dim')
    m = re.match(r"^[A-G][#b]?(.*)$", query_tok.strip())
    raw_suf = (m.group(1) if m else "") or ""
    if raw_suf not in ("", "m", "min", "maj", "dim"):
        return c_qual == q_qual

    if q_qual == "maj":
        return c_qual in ("maj", "maj7", "6", "6/9")
    if q_qual == "min":
        return c_qual in ("min", "min7")
    if q_qual == "dim":
        return c_qual in ("dim", "dim7", "m7b5")
    return c_qual == q_qual


def _match_progression_tokens(
    chords_list: list[dict[str, Any]],
    query_tokens: list[str],
) -> tuple[int, Optional[float], list[str]]:
    """Count occurrences of a chord/Roman progression in a chord timeline.

    Uses the exact same functional rules as :mod:`karaoke.harmony` for canonical
    cadences (`ii-V-I`, `minor ii-V-i`, `ii-V`, `Andalusian`), and strict functional
    quality families for general Roman or chord-name progressions.

    Returns (match_count, first_match_at_s, first_matched_names).
    """
    if not chords_list or not query_tokens:
        return 0, None, []

    from . import harmony

    is_roman = all(_parse_roman_token(tok) is not None for tok in query_tokens)

    # Collapse consecutive identical chords so held/repeated bars form a clean progression
    collapsed: list[dict[str, Any]] = []
    for c in chords_list:
        name = str(c.get("name") or "")
        roman_str = str(c.get("roman") or "")
        if not name and not roman_str:
            continue
        key_id = name or roman_str
        if collapsed and (collapsed[-1].get("name") or collapsed[-1].get("roman")) == key_id:
            continue
        collapsed.append(c)

    n = len(query_tokens)
    if len(collapsed) < n:
        return 0, None, []

    # Detect canonical functional cadences so search_by_chords(progression="ii V I")
    # uses the exact same cadence rules as harmony.summarise().
    q_tuple = tuple(query_tokens)
    is_major_ii_v_i = q_tuple in (
        ("ii", "V", "I"), ("ii7", "V7", "I"), ("ii", "V7", "I"),
        ("ii7", "V", "I"), ("ii7", "V7", "Imaj7"), ("ii", "V", "Imaj7"),
    )
    is_minor_ii_v_i = q_tuple in (
        ("ii°", "V", "i"), ("iiø7", "V7", "i"), ("iiø7", "V", "i"), ("ii°", "V7", "i"),
    )
    is_ii_v = q_tuple in (
        ("ii", "V"), ("ii7", "V7"), ("ii", "V7"), ("ii7", "V"),
    )

    matches = 0
    first_at: Optional[float] = None
    first_excerpt: list[str] = []

    for idx in range(len(collapsed) - n + 1):
        window = collapsed[idx : idx + n]
        ok = False

        if is_major_ii_v_i and all(w.get("name") for w in window):
            p0 = _parse_chord_name(str(window[0]["name"]))
            p1 = _parse_chord_name(str(window[1]["name"]))
            p2 = _parse_chord_name(str(window[2]["name"]))
            if p0 and p1 and p2:
                down_1 = (p1[0] - p0[0]) % 12 == 5
                down_2 = (p2[0] - p1[0]) % 12 == 5
                q0_ok = (p0[1] == "min7") if query_tokens[0] == "ii7" else (p0[1] in harmony.MINOR_TWO)
                q1_ok = p1[1] in harmony.DOMINANT
                q2_ok = (p2[1] == "maj7") if query_tokens[2] == "Imaj7" else (p2[1] in harmony.MAJOR_TONIC)
                ok = down_1 and down_2 and q0_ok and q1_ok and q2_ok
        elif is_minor_ii_v_i and all(w.get("name") for w in window):
            p0 = _parse_chord_name(str(window[0]["name"]))
            p1 = _parse_chord_name(str(window[1]["name"]))
            p2 = _parse_chord_name(str(window[2]["name"]))
            if p0 and p1 and p2:
                down_1 = (p1[0] - p0[0]) % 12 == 5
                down_2 = (p2[0] - p1[0]) % 12 == 5
                ok = (
                    down_1 and down_2
                    and p0[1] in harmony.HALF_DIM_TWO
                    and p1[1] in harmony.DOMINANT
                    and p2[1] in harmony.MINOR_TONIC
                )
        elif is_ii_v and all(w.get("name") for w in window):
            p0 = _parse_chord_name(str(window[0]["name"]))
            p1 = _parse_chord_name(str(window[1]["name"]))
            if p0 and p1:
                down_1 = (p1[0] - p0[0]) % 12 == 5
                q0_ok = (p0[1] == "min7") if query_tokens[0] == "ii7" else (
                    p0[1] in (harmony.MINOR_TWO | harmony.HALF_DIM_TWO)
                )
                ok = down_1 and q0_ok and (p1[1] in harmony.DOMINANT)
        else:
            ok = True
            for pos_i, (q_tok, item) in enumerate(zip(query_tokens, window)):
                if is_roman:
                    r_val = str(item.get("roman") or "")
                    if not r_val or not _roman_token_matches(q_tok, r_val):
                        ok = False
                        break
                    # When roman-only items are passed for ii-V-I without chord names,
                    # require V to be V7/V9 so plain triad V does not count as a ii-V-I cadence.
                    if (is_major_ii_v_i or is_minor_ii_v_i or is_ii_v) and pos_i == 1 and q_tok == "V":
                        if r_val not in ("V7", "V9"):
                            ok = False
                            break
                else:
                    c_val = str(item.get("name") or "")
                    if not c_val or not _chord_token_matches(q_tok, c_val):
                        ok = False
                        break

        if ok:
            matches += 1
            if first_at is None:
                first_at = float(window[0].get("at") or 0.0)
                first_excerpt = [
                    f"{w.get('name')} ({w.get('roman')})" if w.get("roman") else str(w.get("name"))
                    for w in window
                ]

    return matches, first_at, first_excerpt


@dj_mcp.tool()
def search_by_chords(
    progression: str = "",
    chords_used: str = "",
    style: str = "",
    cadence: str = "",
    form: str = "",
    min_blues_score: float = 0.0,
    key: str = "",
    only_synced_lyrics: bool = False,
    limit: int = 15,
) -> str:
    """Search tracks using v2 chord detection, functional harmony, and chord progressions.

    Args:
        progression: Ordered chord sequence in Roman numerals (e.g. 'ii V I', 'I V vi IV', 'i bVII bVI V')
            or chord names (e.g. 'Am F C G', 'Dm7 G7 C'). Canonical cadences ('ii V I', 'ii° V i', 'ii V')
            match functional cadences anywhere in the song (including tonicized local ii–V–Is such as
            'v7 -> I7 -> IVmaj7'), matching harmony.summarise() cadence counts.
        chords_used: Chords or qualities that must appear anywhere in the song (e.g. 'F#m7 B7', 'm7b5', 'dim7', '6/9').
        style: Functional harmony style ('jazz-functional', 'blues', 'andalusian', 'triadic', 'mixed').
        cadence: Filter for songs with specific cadences ('ii_v_i', 'minor_ii_v_i', 'ii_v', 'tritone_subs',
            'secondary_dominants', 'backdoor', 'andalusian', 'fifth_chains').
        form: Key-progression form ('static', 'static-with-mixture', 'diatonic-vamp', 'truck-driver',
            'oscillating', 'modulating', 'restless').
        min_blues_score: Minimum blues harmony score (0.0 to 1.0, e.g. 0.35 for blues).
        key: Optional musical key ('Am', 'C') or Camelot code ('8A', '8B').
        only_synced_lyrics: If true, only return tracks with synced karaoke lyrics.
        limit: Max results to return (default 15, max 50).
    """
    import re
    from . import chord_timeline

    limit = max(1, min(limit, 50))
    prog_tokens = [t for t in re.split(r"[\s,\->–—]+", progression.strip()) if t] if progression.strip() else []
    required_chords = [t for t in re.split(r"[\s,]+", chords_used.strip()) if t] if chords_used.strip() else []
    target_camelot = key.strip().upper() if key else ""

    form_map: dict[int, dict[str, Any]] = {}
    if form.strip():
        try:
            from .key_progression import PROGRESSION_INDEX
            from .osclient import client as get_os_client
            osc = get_os_client()
            if osc and osc.indices.exists(index=PROGRESSION_INDEX):
                res = osc.search(
                    index=PROGRESSION_INDEX,
                    body={
                        "size": 500,
                        "query": {"term": {"form": form.strip().lower()}},
                        "_source": ["track_id", "form", "fifth_ratio", "distinct_chords", "stability"],
                    },
                )
                for hit in res.get("hits", {}).get("hits", []):
                    src = hit.get("_source") or {}
                    if src.get("track_id") is not None:
                        form_map[int(src["track_id"])] = src
        except Exception:
            pass

    with localcache.connect() as conn:
        chord_timeline.ensure_schema(conn)
        cur = conn.cursor()
        sql = """
            SELECT t.track_id, t.artist, t.title, t.album, t.duration, t.play_count,
                   a.detected_key, a.bpm, a.energy, g.genre,
                   tc.chords_version, tc.chords, tc.harmony,
                   EXISTS(
                       SELECT 1 FROM lyrics l
                       WHERE l.track_id = t.track_id AND l.synced_lyrics != ''
                   ) AS has_synced
            FROM track_chords tc
            JOIN tracks t ON t.track_id = tc.track_id
            LEFT JOIN track_analysis a ON a.track_id = t.track_id
            LEFT JOIN track_genre g ON g.track_id = t.track_id
        """
        where_clauses = []
        params: list[Any] = []

        if style.strip():
            where_clauses.append("lower(tc.harmony->>'style') = lower(%s)")
            params.append(style.strip())

        if min_blues_score > 0:
            where_clauses.append("COALESCE((tc.harmony->>'blues_score')::float, 0.0) >= %s")
            params.append(min_blues_score)

        cad = cadence.strip().lower()
        if cad in ("ii_v_i", "minor_ii_v_i", "ii_v", "tritone_subs", "secondary_dominants", "backdoor", "andalusian"):
            where_clauses.append(f"COALESCE((tc.harmony->>'{cad}')::int, 0) > 0")
        elif cad in ("fifth_chains", "fifth_chains_per_minute"):
            where_clauses.append("COALESCE((tc.harmony->>'fifth_chains_per_minute')::float, 0.0) > 0")

        if only_synced_lyrics:
            where_clauses.append("EXISTS (SELECT 1 FROM lyrics l WHERE l.track_id = t.track_id AND l.synced_lyrics != '')")

        if where_clauses:
            sql += " WHERE " + " AND ".join(where_clauses)

        cur.execute(sql, params)
        rows = cur.fetchall()

    matches = []
    for r in rows:
        tid = int(r["track_id"])
        if form.strip() and form_map and tid not in form_map:
            continue

        detected_key = r["detected_key"] or ""
        key_obj = musictheory.parse_key(detected_key) if detected_key else None
        camelot = key_obj.camelot if key_obj else "?"
        if target_camelot:
            matches_camelot = camelot.upper() == target_camelot
            matches_name = key_obj and (
                key_obj.short.upper() == target_camelot
                or key_obj.name.upper() == target_camelot
            )
            if not (matches_camelot or matches_name):
                continue

        chords_list = r["chords"] if isinstance(r["chords"], list) else []
        harmony_doc = r["harmony"] if isinstance(r["harmony"], dict) else {}

        if required_chords:
            ok_req = True
            for req in required_chords:
                req_s = req.strip()
                if not req_s:
                    continue
                if req_s in _CHORD_SUFFIX_TO_QUALITY and req_s != "":
                    target_qual = _CHORD_SUFFIX_TO_QUALITY[req_s]
                    if any(
                        (_parse_chord_name(str(c.get("name") or "")) or (None, None))[1] == target_qual
                        for c in chords_list
                    ):
                        continue
                elif _parse_roman_token(req_s) is not None:
                    if any(_roman_token_matches(req_s, str(c.get("roman") or "")) for c in chords_list):
                        continue
                elif _parse_chord_name(req_s) is not None:
                    if any(_chord_token_matches(req_s, str(c.get("name") or "")) for c in chords_list):
                        continue
                else:
                    rl = req_s.lower()
                    if any(
                        str(c.get("name") or "").lower() == rl or str(c.get("roman") or "") == req_s
                        for c in chords_list
                    ):
                        continue
                ok_req = False
                break
            if not ok_req:
                continue

        prog_count = 0
        first_at = None
        excerpt: list[str] = []
        if prog_tokens:
            prog_count, first_at, excerpt = _match_progression_tokens(chords_list, prog_tokens)
            if prog_count == 0:
                continue

        entry: dict[str, Any] = {
            "track_id": tid,
            "artist": r["artist"],
            "title": r["title"],
            "key": detected_key,
            "camelot": camelot,
            "bpm": round(r["bpm"], 1) if r["bpm"] else None,
            "genre": r["genre"] or "",
            "has_synced_lyrics": bool(r["has_synced"]),
            "chords_version": r["chords_version"],
            "harmony_style": harmony_doc.get("style", "unknown"),
            "cadences": {
                "ii_v_i": harmony_doc.get("ii_v_i", 0),
                "minor_ii_v_i": harmony_doc.get("minor_ii_v_i", 0),
                "tritone_subs": harmony_doc.get("tritone_subs", 0),
                "secondary_dominants": harmony_doc.get("secondary_dominants", 0),
                "backdoor": harmony_doc.get("backdoor", 0),
                "andalusian": harmony_doc.get("andalusian", 0),
                "blues_score": harmony_doc.get("blues_score", 0.0),
                "fifth_chains_per_minute": harmony_doc.get("fifth_chains_per_minute", 0.0),
            },
        }
        if tid in form_map:
            entry["form"] = form_map[tid].get("form")
        if prog_tokens:
            entry["progression_matches"] = prog_count
            entry["first_match_at_s"] = round(first_at, 1) if first_at is not None else None
            entry["matched_excerpt"] = " -> ".join(excerpt)
        matches.append(entry)

    # Sort best matches first: progression count -> requested cadence -> fifth chains -> play_count
    def _sort_key(item: dict[str, Any]) -> tuple:
        cads = item.get("cadences") or {}
        cad_score = cads.get(cad, 0) if cad in cads else (
            cads.get("ii_v_i", 0) + cads.get("minor_ii_v_i", 0) + cads.get("tritone_subs", 0)
        )
        return (
            item.get("progression_matches", 0),
            cad_score,
            cads.get("fifth_chains_per_minute", 0.0),
            cads.get("blues_score", 0.0),
        )

    matches.sort(key=_sort_key, reverse=True)
    sliced = matches[:limit]
    return json.dumps({
        "count": len(sliced),
        "total_matches": len(matches),
        "tracks": sliced,
    }, indent=2)


@dj_mcp.tool()
def get_track_chords(
    track_id: Optional[int] = None,
    artist: Optional[str] = None,
    title: Optional[str] = None,
    max_chords: int = 60,
) -> str:
    """Get the v2 chord timeline, functional harmony summary, cadences, and top recurring chord progressions for a song.

    If the track does not yet have v2 chords stored in `track_chords` and its audio file is cached on disk,
    detects and stores the chords on demand.

    Args:
        track_id: Track ID in the library (optional if artist & title provided).
        artist: Artist name (used if track_id is omitted).
        title: Song title (used if track_id is omitted).
        max_chords: Maximum timed chord changes to return in the timeline (default 60, max 300).
    """
    from collections import Counter
    from . import chord_timeline, chords as chords_mod

    max_chords = max(1, min(max_chords, 300))
    with localcache.connect() as conn:
        cur = conn.cursor()
        row = None
        if track_id is not None:
            cur.execute(
                "SELECT t.track_id, t.artist, t.title, t.duration, a.detected_key, a.bpm"
                " FROM tracks t LEFT JOIN track_analysis a ON a.track_id = t.track_id"
                " WHERE t.track_id = %s",
                (track_id,),
            )
            row = cur.fetchone()
        elif artist or title:
            pat_a = f"%{artist.strip()}%" if artist else "%"
            pat_t = f"%{title.strip()}%" if title else "%"
            cur.execute(
                "SELECT t.track_id, t.artist, t.title, t.duration, a.detected_key, a.bpm"
                " FROM tracks t LEFT JOIN track_analysis a ON a.track_id = t.track_id"
                " WHERE t.artist ILIKE %s AND t.title ILIKE %s"
                " ORDER BY t.play_count DESC LIMIT 1",
                (pat_a, pat_t),
            )
            row = cur.fetchone()

        if not row:
            return json.dumps({"error": f"Track not found (track_id={track_id}, artist={artist}, title={title})."})

        tid = int(row["track_id"])
        tl = chord_timeline.load(tid, conn)
        if (tl is None or tl.get("chords_version", 0) < chords_mod.CHORDS_VERSION):
            audio_path = chord_timeline.local_audio(tid, conn)
            if audio_path is not None:
                try:
                    analysis = chords_mod.detect(str(audio_path))
                    if analysis is not None:
                        chord_timeline.save(
                            tid,
                            analysis,
                            conn,
                            source_path=str(audio_path),
                            key_text=row["detected_key"] or "",
                        )
                        tl = chord_timeline.load(tid, conn)
                except Exception as exc:
                    log.warning("On-demand chord detection failed for track %s: %s", tid, exc)

        if not tl:
            return json.dumps({
                "track_id": tid,
                "artist": row["artist"],
                "title": row["title"],
                "error": "No v2 chord timeline stored yet for this track (and audio is not cached on local disk).",
            }, indent=2)

        chords_list = tl.get("chords") or []
        harmony_doc = tl.get("harmony") or {}

        # Collapse consecutive identical chords to extract recurring 3- and 4-chord progressions
        collapsed = []
        for c in chords_list:
            if not collapsed or collapsed[-1].get("name") != c.get("name"):
                collapsed.append(c)

        name_4grams = Counter(
            " -> ".join(str(x.get("name")) for x in collapsed[i : i + 4])
            for i in range(max(0, len(collapsed) - 3))
        )
        roman_3grams = Counter(
            " -> ".join(str(x.get("roman")) for x in collapsed[i : i + 3])
            for i in range(max(0, len(collapsed) - 2))
            if all(x.get("roman") for x in collapsed[i : i + 3])
        )
        distinct_vocab = [k for k, _ in Counter(str(c.get("name")) for c in chords_list if c.get("name")).most_common(16)]

        detected_key = row["detected_key"] or ""
        key_obj = musictheory.parse_key(detected_key) if detected_key else None

        return json.dumps({
            "track_id": tid,
            "artist": row["artist"],
            "title": row["title"],
            "key": detected_key,
            "camelot": key_obj.camelot if key_obj else "?",
            "bpm": round(row["bpm"], 1) if row["bpm"] else None,
            "chords_version": tl.get("chords_version"),
            "duration_s": tl.get("duration_s"),
            "total_chord_events": len(chords_list),
            "distinct_chords": distinct_vocab,
            "harmony": harmony_doc,
            "top_chord_progressions": [
                {"progression": prog, "count": cnt}
                for prog, cnt in name_4grams.most_common(6)
            ],
            "top_roman_progressions": [
                {"roman": prog, "count": cnt}
                for prog, cnt in roman_3grams.most_common(6)
            ],
            "timeline": chords_list[:max_chords],
            "timeline_truncated": len(chords_list) > max_chords,
        }, indent=2)


def _scan_result(
    tid: int,
    artist_name: str,
    title_name: str,
    missing_before: list[str],
    source_info: Optional[dict[str, Any]],
    mode_used: str,
    status: str,
    steps_done: Optional[list[str]] = None,
    steps_failed: Optional[list[str]] = None,
    note: str = "",
    dry_run: bool = False,
) -> str:
    return json.dumps({
        "track_id": tid,
        "artist": artist_name,
        "title": title_name,
        "missing_before": missing_before,
        "source": source_info,
        "mode_used": mode_used,
        "dry_run": dry_run,
        "status": status,
        "steps_done": list(steps_done or []),
        "steps_failed": list(steps_failed or []),
        "note": note,
    }, indent=2)


@dj_mcp.tool()
def scan_track(
    track_id: Optional[int] = None,
    artist: Optional[str] = None,
    title: Optional[str] = None,
    mode: str = "auto",
    find_source: bool = True,
    sample_seconds: float = 45.0,
    include_suspect: bool = False,
    dry_run: bool = False,
) -> str:
    """Analyse a track that is missing key/BPM, chords, CLAP or lyric sync.

    Finds a playable source if the track has none, then runs or queues the
    post-processing pipeline (key/BPM analysis, chords, CLAP audio vectors,
    sync, and search documents).

    Args:
        track_id: Track ID in the library (optional if artist & title provided).
        artist: Artist name (used if track_id is omitted).
        title: Song title (used if track_id is omitted).
        mode: Processing mode:
            - 'auto' (default): queue if a downloadable source exists or is found;
              else sample desktop audio if the track is playing now; else 'no_source'.
            - 'queue': publish a task to the background Celery/RabbitMQ worker with full=True.
            - 'inline': run audio audit and pipeline synchronously in this process.
            - 'sample': record currently playing desktop audio for key/BPM/CLAP (only if this track is playing).
        find_source: If true and the track has no downloadable source, search and save a YouTube source first.
        sample_seconds: Capture duration in seconds for sample mode (default 45.0).
        include_suspect: If true, inline mode also processes files flagged as 'suspect' by the audio audit (e.g. length mismatch).
        dry_run: If true, inspect missing steps and candidate sources without saving, queuing or sampling anything.
            Anticipated statuses: 'would_queue', 'would_sample', 'would_analyse', 'complete', or 'no_source'.
    """
    if mode not in ("auto", "queue", "inline", "sample"):
        return json.dumps({"error": f"Invalid mode '{mode}'. Expected 'auto', 'queue', 'inline', or 'sample'."})

    with localcache.connect() as conn:
        cur = conn.cursor()
        row = None
        if track_id is not None:
            cur.execute(
                "SELECT t.track_id, t.artist, t.title, t.duration, t.album"
                " FROM tracks t WHERE t.track_id = %s",
                (track_id,),
            )
            row = cur.fetchone()
        elif artist or title:
            tid = localcache.find_track_id(artist or "", title or "", conn)
            if tid is not None:
                cur.execute(
                    "SELECT t.track_id, t.artist, t.title, t.duration, t.album"
                    " FROM tracks t WHERE t.track_id = %s",
                    (tid,),
                )
                row = cur.fetchone()

        if not row:
            return json.dumps({"error": f"Track not found (track_id={track_id}, artist={artist}, title={title})."})

        tid = int(row["track_id"])
        artist_name = row["artist"] or ""
        title_name = row["title"] or ""

        # Step 1. What is missing
        from . import postprocess_queue, search
        missing_before = list(postprocess_queue.needs_postprocessing(tid, conn))
        if search.clap_vector_for(tid) is None and "clap" not in missing_before:
            missing_before.append("clap")

        # Check existing source
        src_row = conn.execute(
            """
            SELECT url, kind FROM sources
            WHERE track_id = %s AND (kind = 'local' OR kind IN ('youtube', 'youtube_music'))
            ORDER BY CASE WHEN kind = 'local' THEN 0 WHEN kind = 'youtube_music' THEN 1 ELSE 2 END
            LIMIT 1
            """,
            (tid,),
        ).fetchone()

        source_info: Optional[dict[str, Any]] = None
        if src_row and src_row["url"]:
            source_info = {"url": src_row["url"], "found_now": False}

        # Timings are opt-in; treat empty or timings-only as complete
        if not missing_before or missing_before == ["timings"]:
            note = (
                "Track is complete (word timings are opt-in; no other analysis pending)."
                if missing_before == ["timings"]
                else "Track is already complete (no missing analysis or vectors)."
            )
            return _scan_result(
                tid, artist_name, title_name, missing_before, source_info,
                mode_used=mode, status="complete", note=note, dry_run=dry_run,
            )

        # Step 2. Source resolution
        has_source = postprocess_queue.has_downloadable_source(tid, conn)
        if not has_source and find_source:
            if dry_run:
                from . import find_sources
                from .lyrics import parse_lrc
                lyr_row = conn.execute(
                    "SELECT synced_lyrics FROM lyrics WHERE track_id = %s AND kind = 'approved' LIMIT 1",
                    (tid,),
                ).fetchone()
                lines = parse_lrc(lyr_row["synced_lyrics"] if lyr_row else "")
                cand = find_sources.Candidate(
                    track_id=tid,
                    artist=artist_name,
                    title=title_name,
                    duration=float(row["duration"]) if row["duration"] else None,
                    lyric_end=lines[-1][0] if lines else None,
                )
                best = find_sources.find_for(cand)
                if best and best.get("url"):
                    source_info = {"url": best["url"], "found_now": True}
            else:
                from . import find_sources
                found_url = find_sources.resolve_and_save_source(tid, conn)
                if found_url:
                    source_info = {"url": found_url, "found_now": True}

        # Step 3. Determine effective mode
        effective_mode = mode
        if effective_mode == "auto":
            has_dl = postprocess_queue.has_downloadable_source(tid, conn) or (source_info and bool(source_info.get("url")))
            if has_dl:
                effective_mode = "queue"
            else:
                is_playing = False
                try:
                    now_data = json.loads(get_now_playing())
                    if now_data.get("status") == "playing":
                        now_tid = now_data.get("track_id")
                        if not now_tid and now_data.get("artist") and now_data.get("title"):
                            now_tid = localcache.find_track_id(now_data["artist"], now_data["title"], conn)
                        if now_tid == tid:
                            is_playing = True
                except Exception as exc:
                    log.debug("Error checking now playing for scan_track: %s", exc)
                if is_playing:
                    effective_mode = "sample"
                else:
                    return _scan_result(
                        tid, artist_name, title_name, missing_before, source_info,
                        mode_used="auto", status="no_source",
                        note="No downloadable source found, and track is not currently playing.",
                        dry_run=dry_run,
                    )

        # Execute by effective mode
        if effective_mode == "queue":
            has_dl = postprocess_queue.has_downloadable_source(tid, conn) or (source_info and bool(source_info.get("url")))
            if not has_dl:
                return _scan_result(
                    tid, artist_name, title_name, missing_before, None,
                    mode_used="queue", status="no_source",
                    note="Cannot queue track: no downloadable source found.",
                    dry_run=dry_run,
                )

            if dry_run:
                return _scan_result(
                    tid, artist_name, title_name, missing_before, source_info,
                    mode_used="queue", status="would_queue",
                    note="Dry run: task would be published to RabbitMQ post-processing queue with full=True.",
                    dry_run=True,
                )

            target_url = source_info.get("url", "") if source_info else ""
            ok = postprocess_queue.publish_postprocess_task(artist_name, title_name, url=target_url, full=True)
            if ok:
                return _scan_result(
                    tid, artist_name, title_name, missing_before, source_info,
                    mode_used="queue", status="queued",
                    note="Call scan_track again (dry_run=true) to see what is still missing.",
                    dry_run=False,
                )
            else:
                return _scan_result(
                    tid, artist_name, title_name, missing_before, source_info,
                    mode_used="queue", status="error",
                    note="Failed to publish postprocess task to RabbitMQ broker.",
                    dry_run=False,
                )

        elif effective_mode == "sample":
            is_playing = False
            now_playing_tid = None
            try:
                now_data = json.loads(get_now_playing())
                if now_data.get("status") == "playing":
                    now_playing_tid = now_data.get("track_id")
                    if not now_playing_tid and now_data.get("artist") and now_data.get("title"):
                        now_playing_tid = localcache.find_track_id(now_data["artist"], now_data["title"], conn)
                    if now_playing_tid == tid:
                        is_playing = True
            except Exception as exc:
                log.debug("Error checking now playing for sample: %s", exc)

            if not is_playing:
                return _scan_result(
                    tid, artist_name, title_name, missing_before, source_info,
                    mode_used="sample", status="error",
                    note=f"Sample mode requires this track to be playing now (currently playing: track_id={now_playing_tid}).",
                    dry_run=dry_run,
                )

            if dry_run:
                return _scan_result(
                    tid, artist_name, title_name, missing_before, source_info,
                    mode_used="sample", status="would_sample",
                    note=f"Dry run: would record {sample_seconds:.0f}s from desktop audio monitor and analyse key/BPM/CLAP.",
                    dry_run=True,
                )

            from . import sample_audio
            done: list[str] = []
            failed: list[str] = []
            try:
                result = sample_audio.sample_and_analyse(
                    artist_name, title_name, seconds=sample_seconds, conn=conn
                )
                if getattr(result, "key", None) is not None or getattr(result, "bpm", None) is not None:
                    done.append("analysis")
                else:
                    failed.append("analysis")
                if search.clap_vector_for(tid) is not None:
                    done.append("clap")
            except (sample_audio.AnalysisUnavailable, sample_audio.CaptureError, sample_audio.CaptureAborted) as exc:
                return _scan_result(
                    tid, artist_name, title_name, missing_before, source_info,
                    mode_used="sample", status="error",
                    steps_failed=["sample"],
                    note=f"Sample audio capture/analysis failed: {exc}",
                    dry_run=False,
                )
            except Exception as exc:
                return _scan_result(
                    tid, artist_name, title_name, missing_before, source_info,
                    mode_used="sample", status="error",
                    steps_failed=["sample"],
                    note=f"Sample audio failed unexpectedly: {exc}",
                    dry_run=False,
                )

            return _scan_result(
                tid, artist_name, title_name, missing_before, source_info,
                mode_used="sample", status="sampled",
                steps_done=done, steps_failed=failed,
                note=f"Sampled {sample_seconds:.0f}s from desktop audio; updated key/BPM/CLAP (chords not available from sample).",
                dry_run=False,
            )

        elif effective_mode == "inline":
            if dry_run:
                return _scan_result(
                    tid, artist_name, title_name, missing_before, source_info,
                    mode_used="inline", status="would_analyse",
                    note="Dry run: would audit audio and run inline processing pipeline.",
                    dry_run=True,
                )

            if not postprocess_queue.claim(artist_name, title_name, conn):
                return _scan_result(
                    tid, artist_name, title_name, missing_before, source_info,
                    mode_used="inline", status="error",
                    note="Track is already claimed by another post-processing worker.",
                    dry_run=False,
                )

            from . import reprocess
            import tempfile
            from pathlib import Path
            done: list[str] = []
            failed: list[str] = []
            status = "analysed"
            note = ""
            allowed_statuses = ("ok", "suspect") if include_suspect else ("ok",)
            try:
                with tempfile.TemporaryDirectory(prefix="scan-track-") as tmp:
                    audit = reprocess.audit_track(tid, row["duration"], conn, Path(tmp), download=True)
                    if audit.status in allowed_statuses and audit.wav is not None:
                        track_dict = {
                            "artist": artist_name,
                            "title": title_name,
                            "album": row["album"] or "",
                            "duration": row["duration"],
                        }
                        done, failed = reprocess.process(tid, track_dict, audit.wav, conn, skip_sync=False)
                        status = "analysed"
                        note = f"Inline audit {audit.status}; processed {len(done)} steps."
                    else:
                        status = "error" if audit.status in ("broken", "suspect") else "no_source"
                        failed = [f"audit_{audit.status}"] + audit.issues
                        if audit.status == "suspect" and not include_suspect:
                            note = f"Audio audit flagged file as suspect ({'; '.join(audit.issues)}). Pass include_suspect=True to process anyway."
                        else:
                            note = f"Audio audit failed: {audit.status} ({'; '.join(audit.issues)})."
            finally:
                postprocess_queue.release(artist_name, title_name, conn)

            return _scan_result(
                tid, artist_name, title_name, missing_before, source_info,
                mode_used="inline", status=status,
                steps_done=done, steps_failed=failed, note=note,
                dry_run=False,
            )


@dj_mcp.tool()
def get_similarity_graph(
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
) -> str:
    """Generate a musical similarity graph (nodes and edges) for a track or playlist.

    Args:
        track_id: Center the constellation on this track ID (optional if playlist_id provided).
        playlist_id: Generate graph spanning songs in a saved playlist (e.g. 'dj-list', 'jazz-funk-happy').
        strategy: 'structural' (chord motion), 'context' (lyric topics & meaning), 'mood' (emotion & sentiment), 'acoustic' (CLAP timbre), or 'harmonic' (Camelot wheel).
        hops: Number of neighbor traversal hops from seed (1-3, default 2).
        limit_per_node: Maximum similarity edges drawn per node (default 4, max 8).
        min_score: Minimum similarity score threshold for drawing links (e.g. 0.6).
        bpm_min: Filter nodes with tempo below this BPM.
        bpm_max: Filter nodes with tempo above this BPM.
        genre_filter: Substring match for genre (e.g. 'jazz', 'blues', 'rock').
        camelot_filter: Exact Camelot key code filter (e.g. '8A', '9B').
        mood_filter: Dominant mood filter (e.g. 'happy', 'tender', 'sad', 'angry').

    Returns:
        JSON with 'nodes', 'edges', 'stats', and a link to the visual graph at http://localhost:8888/graph.
    """
    from . import graph_view
    data = graph_view.build_similarity_graph(
        track_id=track_id,
        playlist_id=playlist_id,
        strategy=strategy,
        hops=hops,
        limit_per_node=limit_per_node,
        min_score=min_score,
        bpm_min=bpm_min,
        bpm_max=bpm_max,
        genre_filter=genre_filter,
        camelot_filter=camelot_filter,
        mood_filter=mood_filter,
    )
    return json.dumps(data, indent=2)


class CombinedMCPApp:
    """Dispatches requests between MCP Streamable HTTP, Server-Sent Events (SSE), and health checks.

    Supports both legacy SSE clients (GET /sse -> event-stream) and Streamable HTTP clients
    (POST /sse -> JSON-RPC, like Obot) on the same endpoint without 405 Method Not Allowed.
    """

    def __init__(
        self,
        sse_transport: SseServerTransport,
        session_mgr: StreamableHTTPSessionManager,
        lowlevel_server: Any,
    ) -> None:
        self.sse = sse_transport
        self.streamable = StreamableHTTPASGIApp(session_mgr)
        self.lowlevel_server = lowlevel_server

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            method = scope.get("method", "GET")
            if method == "HEAD":
                response = Response(status_code=200)
                await response(scope, receive, send)
                return
            if method == "GET":
                headers = dict(scope.get("headers", []))
                accept = headers.get(b"accept", b"").decode("utf-8", errors="ignore")
                has_streamable_session = b"mcp-session-id" in headers
                if "text/event-stream" in accept and not has_streamable_session:
                    async with self.sse.connect_sse(scope, receive, send) as streams:
                        await self.lowlevel_server.run(
                            streams[0],
                            streams[1],
                            self.lowlevel_server.create_initialization_options(),
                        )
                    return
        await self.streamable(scope, receive, send)


def create_asgi_app(server: MCPServer = dj_mcp) -> Starlette:
    """Build the unified Starlette ASGI application supporting SSE and Streamable HTTP."""
    from . import graph_view

    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
        allowed_hosts=["*"],
        allowed_origins=["*"],
    )
    sse = SseServerTransport("/messages/", security_settings=security)
    session_manager = StreamableHTTPSessionManager(
        app=server._lowlevel_server,
        security_settings=security,
    )
    combined = CombinedMCPApp(sse, session_manager, server._lowlevel_server)

    routes = [
        Route("/", endpoint=lambda r: JSONResponse({"status": "ok", "server": "karaoke-dj"})),
        Route("/health", endpoint=lambda r: JSONResponse({"status": "ok"})),
        Route("/graph", endpoint=graph_view.serve_graph_ui),
        Route("/api/graph_data", endpoint=graph_view.api_graph_data),
        Route("/api/playlists", endpoint=graph_view.api_playlists),
        Route("/api/play", endpoint=graph_view.api_play, methods=["GET", "POST"]),
        Route("/api/identify", endpoint=graph_view.api_identify, methods=["POST"]),
        Route("/api/identify/status", endpoint=graph_view.api_identify_status),
        Route("/api/locate", endpoint=graph_view.api_locate, methods=["POST"]),
        Route("/api/collection/save", endpoint=graph_view.api_collection_save, methods=["POST"]),
        Route("/api/collection/play", endpoint=graph_view.api_collection_play, methods=["POST"]),
        Route("/api/now_playing", endpoint=lambda r: JSONResponse(json.loads(get_now_playing()))),
        Route("/sse", endpoint=combined),
        Route("/sse/", endpoint=combined),
        Route("/mcp", endpoint=combined),
        Route("/mcp/", endpoint=combined),
        Mount("/messages", app=sse.handle_post_message),
    ]

    return Starlette(routes=routes, lifespan=lambda a: session_manager.run())


def main() -> None:
    parser = argparse.ArgumentParser(description="Karaoke AI DJ Model Context Protocol (MCP) Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host interface to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8888, help="Port to listen on (default: 8888)")
    parser.add_argument("--stdio", action="store_true", help="Run in stdio mode instead of HTTP")
    args = parser.parse_args()

    if args.stdio:
        import asyncio
        asyncio.run(dj_mcp.run_stdio_async())
    else:
        app = create_asgi_app(dj_mcp)
        print(f"Starting Karaoke DJ MCP Server on http://{args.host}:{args.port} (SSE: /sse, Streamable: /sse & /mcp) ...")
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
