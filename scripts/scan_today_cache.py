"""Scan only today's cached YouTube audio files: analysis + genre + vectors."""
import datetime
import time
from pathlib import Path

from karaoke.config import settings
from karaoke import folder_scan, vector_index

yt_dir = Path(settings.youtube_dir).expanduser()
cutoff = time.time() - 86400  # last 24 hours

today_files = sorted(
    [f for f in yt_dir.glob("*.*") if f.is_file() and f.stat().st_mtime >= cutoff],
    key=lambda f: f.stat().st_mtime,
)
print(f"YouTube cache dir: {yt_dir}", flush=True)
print(f"Files modified in last 24h: {len(today_files)}", flush=True)

# Stage today's files into an isolated temp dir of symlinks so folder_scan
# only touches them (folder_scan takes a directory, not a file list).
import tempfile
staging = Path(tempfile.mkdtemp(prefix="karaoke_today_cache_"))
for f in today_files:
    try:
        (staging / f.name).symlink_to(f)
    except FileExistsError:
        pass

def on_progress(event, payload):
    idx = payload.get("index")
    total = payload.get("total")
    prefix = f"[{idx}/{total}] " if idx and total else ""
    name = payload.get("name") or payload.get("root") or ""
    artist = payload.get("artist", "")
    title = payload.get("title", "")
    track = f" -> {artist} - {title}" if artist or title else ""
    if event == "found":
        print(f"Found {payload.get('total')} files to scan.", flush=True)
    elif event == "item_start":
        print(f"{prefix}Scanning {name} ...", flush=True)
    elif event == "item_done":
        print(f"{prefix}DONE {name}{track} (key={payload.get('key')}, "
              f"bpm={payload.get('bpm')}, genre={payload.get('genre')})", flush=True)
    elif event == "skip":
        print(f"{prefix}SKIP {name}: {payload.get('reason')}", flush=True)
    elif event == "error":
        print(f"{prefix}ERROR {name}: {payload.get('error')}", flush=True)

stats = folder_scan.scan_and_ingest_folder(
    staging,
    use_fingerprint=True,
    classify_audio=True,
    resolve_streaming=True,
    dry_run=False,
    progress=on_progress,
)

print("\n=== SCAN COMPLETE ===", flush=True)
print(f" Seen:       {stats.get('seen')}", flush=True)
print(f" Processed:  {stats.get('processed')}", flush=True)
print(f" Classified: {stats.get('classified')}", flush=True)
print(f" Sourced:    {stats.get('sourced')}", flush=True)
print(f" Errors:     {stats.get('errors')}", flush=True)

print("\nRebuilding OpenSearch vectors ...", flush=True)
st = vector_index.rebuild_from_sqlite(embed=True, include_lines=True)
print(f"Vector index updated: {st.indexed} tracks, {st.line_docs} line docs", flush=True)
print("ALL DONE", flush=True)
