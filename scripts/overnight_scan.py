import os
from pathlib import Path
import json

# Set data directory for karaoke logs and cache
base_dir = Path(__file__).resolve().parents[2]
data_dir = base_dir / "karaoke_data"
os.environ["KARAOKE_DATA_DIR"] = str(data_dir)

# Ensure log directory exists (avoids read‑only errors)
(data_dir / "logs").mkdir(parents=True, exist_ok=True)

# Import after environment is set
from karaoke.folder_scan import scan_and_ingest_folder

music_dir = Path.home() / "Music"
if not music_dir.is_dir():
    print(f"Music directory {music_dir} does not exist", flush=True)
else:
    result = scan_and_ingest_folder(music_dir)
    out_path = data_dir / "overnight_scan_result.json"
    out_path.write_text(json.dumps(result, indent=2))
    print(f"Overnight scan completed, results written to {out_path}")
