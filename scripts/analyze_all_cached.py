"""Analyze all cached YouTube audio files and store their key/BPM in the database."""
from pathlib import Path
from karaoke.config import settings
from karaoke.analyze import analyze_audio
from karaoke import localcache, track_analysis

def main():
    yt_dir = Path(settings.youtube_dir)
    if not yt_dir.exists():
        print(f"YouTube cache dir {yt_dir} does not exist.")
        return

    conn = localcache.connect()
    track_analysis.ensure_schema(conn)

    # Map video ID to track_id from sources table
    cur = conn.cursor()
    cur.execute("SELECT track_id, url FROM sources WHERE kind = 'youtube'")
    url_to_track = {row["url"]: row["track_id"] for row in cur.fetchall() if row["url"]}

    # Get already analyzed track IDs
    cur.execute("SELECT track_id FROM track_analysis")
    analyzed_tracks = {row["track_id"] for row in cur.fetchall()}

    files = list(yt_dir.glob("*.webm"))
    print(f"Found {len(files)} files in YouTube cache.")

    analyzed = 0
    skipped = 0
    missing = 0
    for i, file_path in enumerate(files):
        vid_id = file_path.stem
        url = f"https://www.youtube.com/watch?v={vid_id}"

        track_id = url_to_track.get(url)
        if not track_id:
            missing += 1
            continue

        if track_id in analyzed_tracks:
            skipped += 1
            continue

        print(f"[{i+1}/{len(files)}] Analysing {file_path.name} (track_id={track_id}) ...")
        try:
            result = analyze_audio(str(file_path))
            key = result.key
            
            kwargs = {
                "detected_key": key,
                "key_confidence": result.key_confidence,
                "key_agreement": result.key_agreement,
                "bpm": result.bpm,
                "method": result.method,
                "analyzer_version": result.version,
                "conn": conn,
            }
            if hasattr(result, "energy"):
                kwargs["energy"] = getattr(result, "energy", None)
            if hasattr(result, "brightness"):
                kwargs["brightness"] = getattr(result, "brightness", None)
                
            import inspect
            sig = inspect.signature(track_analysis.save_detected)
            for k in list(kwargs.keys()):
                if k not in sig.parameters:
                    del kwargs[k]
                    
            track_analysis.save_detected(track_id, **kwargs)
            print(f"  -> key: {key.name if key else 'unknown'}, bpm: {result.bpm if result.bpm else 'unknown'}")
            analyzed += 1
        except Exception as e:
            print(f"  -> Error: {e}")

    conn.close()
    print(f"Done. Analyzed {analyzed} tracks; skipped {skipped} already-analyzed; {missing} files with no track mapping.")

if __name__ == "__main__":
    main()
