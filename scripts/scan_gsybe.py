"""Scan a specific local music folder: tags/fingerprint -> find source -> analyse -> vectors."""
import sys
from pathlib import Path

from karaoke import folder_scan, vector_index

folder = Path("/run/media/tina/52C3-BEF5/Music-backup-DATA/Godspeed You! Black Emperor/")
print(f"Scanning: {folder}", flush=True)

def on_progress(event, payload):
    idx = payload.get("index")
    total = payload.get("total")
    prefix = f"[{idx}/{total}] " if idx and total else ""
    name = payload.get("name") or payload.get("root") or ""
    artist = payload.get("artist", "")
    title = payload.get("title", "")
    track = f" -> {artist} - {title}" if artist or title else ""
    if event == "found":
        print(f"Found {payload.get('total')} audio files.", flush=True)
    elif event == "item_start":
        print(f"{prefix}Scanning {name} ...", flush=True)
    elif event == "analysis_done":
        print(f"{prefix}   analysed key={payload.get('key')} bpm={payload.get('bpm')}", flush=True)
    elif event == "source_done":
        srcs = []
        if payload.get("yt_url"):
            srcs.append("YouTube")
        if payload.get("spotify_uri"):
            srcs.append("Spotify")
        print(f"{prefix}   sources: {', '.join(srcs) or 'none'}", flush=True)
    elif event == "item_done":
        print(f"{prefix}DONE{track} (genre={payload.get('genre')})", flush=True)
    elif event == "skip":
        print(f"{prefix}SKIP {name}: {payload.get('reason')}", flush=True)
    elif event == "error":
        print(f"{prefix}ERROR {name}: {payload.get('error')}", flush=True)

stats = folder_scan.scan_and_ingest_folder(
    folder,
    use_fingerprint=True,
    classify_audio=True,
    resolve_streaming=True,
    dry_run=False,
    progress=on_progress,
)

print("\n=== SCAN COMPLETE ===", flush=True)
for k in ("seen", "processed", "fingerprinted", "classified", "sourced", "errors"):
    print(f" {k:12s}: {stats.get(k)}", flush=True)

print("\nRebuilding OpenSearch vectors ...", flush=True)
st = vector_index.rebuild_from_sqlite(embed=True, include_lines=True)
print(f"Vector index updated: {st.indexed} tracks, {st.line_docs} line docs", flush=True)
print("ALL DONE", flush=True)
