"""Audit a set of tracks' audio, then run the full per-track pipeline on it.

Decodes candidate audio files to WAV, audits for decodability, duration and
volume sanity, and runs missing pipeline steps (key/BPM analysis, harmony,
CLAP audio vectors, Whisper sync for plain-only lyrics, and OpenSearch search documents).
"""
from __future__ import annotations

import csv
import json
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import clap_vector, localcache
from .config import settings
from .logger import log

# Same disk as batch_enrich_chords_and_vectors.py. Local source rows point at
# either root depending on when they were scanned; the other may hold the copy.
BACKUP_DISK_ROOT = Path("/run/media/tina/52C3-BEF5/Music-backup-DATA")
HOME_MUSIC_ROOT = Path.home() / "Music"

WAV_RATE = 44100
WAV_CHANNELS = 2
MIN_SECONDS = 30.0
SILENT_MAX_DB = -50.0
# A length within this of tracks.duration is the same recording. Video edits
# and truncations are well outside it; encoder padding is well inside.
DURATION_TOLERANCE_S = 5.0
DURATION_TOLERANCE_RATIO = 0.05

SOURCE_ORDER = {"local": 0, "youtube_music": 1, "youtube": 2}


@dataclass
class Candidate:
    path: Path
    kind: str
    url: str


@dataclass
class Audit:
    status: str = "missing"  # ok | suspect | broken | missing
    candidate: Optional[Candidate] = None
    demuxer: str = ""
    decoded_s: Optional[float] = None
    max_db: Optional[float] = None
    decode_errors: int = 0
    has_video: bool = False
    issues: list[str] = field(default_factory=list)
    wav: Optional[Path] = None


def counterpart_paths(path: Path) -> list[Path]:
    """The path itself, then the same relative path under the other root."""
    out = [path]
    for here, there in ((BACKUP_DISK_ROOT, HOME_MUSIC_ROOT),
                        (HOME_MUSIC_ROOT, BACKUP_DISK_ROOT)):
        try:
            out.append(there / path.relative_to(here))
        except ValueError:
            continue
    return out


def cached_youtube_file(url: str) -> Optional[Path]:
    vid = localcache.extract_youtube_id(url or "")
    if not vid:
        return None
    for path in sorted(Path(settings.youtube_dir).glob(f"{vid}.*")):
        if path.is_file() and not path.name.endswith((".part", ".json")):
            return path
    return None


def candidates(track_id: int, conn, *, download: bool) -> list[Candidate]:
    """Every file that could be this track's audio, best source first.

    Local before YouTube Music before YouTube: a library file is the album
    cut, a YT Music source is usually the audio (ATV) upload, and a plain
    YouTube watch link is as often the video edit with its intro.
    """
    rows = conn.execute(
        "SELECT kind, url FROM sources WHERE track_id = %s", (track_id,)
    ).fetchall()
    rows = sorted(rows, key=lambda r: SOURCE_ORDER.get(r["kind"], 9))
    found: list[Candidate] = []
    seen: set[Path] = set()

    def add(path: Optional[Path], kind: str, url: str) -> None:
        if path is not None and path.is_file() and path not in seen:
            seen.add(path)
            found.append(Candidate(path, kind, url))

    for row in rows:
        url = row["url"] or ""
        if row["kind"] == "local":
            raw = Path(url.removeprefix("file://"))
            for path in counterpart_paths(raw):
                add(path, "local", url)
        elif localcache.extract_youtube_id(url):
            add(cached_youtube_file(url), row["kind"], url)

    if download and not found:
        from . import postprocess_worker

        for row in rows:
            url = row["url"] or ""
            if row["kind"] in ("youtube_music", "youtube") and localcache.extract_youtube_id(url):
                add(postprocess_worker.run_download_logic(url, None), row["kind"], url)
                if found:
                    break
    return found


def probe_streams(path: Path, demuxer: str = "") -> Optional[list[dict[str, Any]]]:
    cmd = ["ffprobe", "-v", "error"]
    if demuxer:
        cmd += ["-f", demuxer]
    cmd += ["-show_entries", "stream=codec_type,codec_name", "-of", "json", str(path)]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        return None
    try:
        return json.loads(res.stdout).get("streams", [])
    except json.JSONDecodeError:
        return None


def decode(cand: Candidate, wav: Path) -> Audit:
    """Decode one candidate to WAV and judge it from what decoding saw."""
    audit = Audit(candidate=cand)
    demuxer = ""
    streams = probe_streams(cand.path)
    if streams is None:
        # The RIFF-wrapped mp3s: the container sniffer picks wav and fails.
        demuxer = "mp3"
        streams = probe_streams(cand.path, demuxer)
    if streams is None:
        audit.status = "broken"
        audit.issues.append("unreadable")
        return audit
    if not any(s.get("codec_type") == "audio" for s in streams):
        audit.status = "broken"
        audit.issues.append("no_audio_stream")
        return audit
    # Cover art arrives as a single-frame mjpeg/png video stream; that is not video.
    audit.has_video = any(s.get("codec_type") == "video"
                          and s.get("codec_name") not in ("mjpeg", "png", "bmp")
                          for s in streams)
    audit.demuxer = demuxer or "auto"

    cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "level+info", "-y"]
    if demuxer:
        cmd += ["-f", demuxer]
    cmd += ["-i", str(cand.path), "-vn", "-ac", str(WAV_CHANNELS),
            "-ar", str(WAV_RATE), "-af", "volumedetect", "-c:a", "pcm_s16le", str(wav)]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if res.returncode != 0 or not wav.is_file():
        audit.status = "broken"
        audit.issues.append("decode_failed")
        return audit
    audit.wav = wav
    audit.decode_errors = sum(1 for line in res.stderr.splitlines()
                              if line.startswith(("[error]", "[fatal]")))
    peak = re.search(r"max_volume:\s*(-?[\d.]+|-inf) dB", res.stderr)
    if peak:
        audit.max_db = float("-inf") if peak.group(1) == "-inf" else float(peak.group(1))
    audit.decoded_s = max(wav.stat().st_size - 44, 0) / (WAV_RATE * WAV_CHANNELS * 2)
    return audit


def judge(audit: Audit, db_duration: Optional[float]) -> None:
    """Settle status and issues for a decoded file against the track row."""
    if audit.status == "broken":
        return
    if audit.max_db is not None and audit.max_db < SILENT_MAX_DB:
        audit.status = "broken"
        audit.issues.append(f"silent(max {audit.max_db:.0f} dB)")
        return
    secs = audit.decoded_s or 0.0
    if secs < MIN_SECONDS:
        audit.issues.append(f"short({secs:.0f}s)")
    if localcache.is_album_upload(secs):
        audit.issues.append(f"album_length({secs:.0f}s)")
    if db_duration:
        tolerance = max(DURATION_TOLERANCE_S, DURATION_TOLERANCE_RATIO * db_duration)
        if abs(secs - db_duration) > tolerance:
            audit.issues.append(f"duration_mismatch({secs:.0f}s vs db {db_duration:.0f}s)")
    else:
        audit.issues.append("no_db_duration")
    if audit.decode_errors:
        audit.issues.append(f"decode_errors({audit.decode_errors})")
    if audit.has_video:
        audit.issues.append("video_source")
    if audit.demuxer == "mp3":
        audit.issues.append("riff_wrapped_mp3")

    # Informational issues describe the file without making it doubtful.
    doubtful = [i for i in audit.issues
                if not i.startswith(("no_db_duration", "video_source", "riff_wrapped_mp3"))]
    audit.status = "suspect" if doubtful else "ok"


RANK = {"ok": 0, "suspect": 1, "broken": 2, "missing": 3}


def audit_track(track_id: int, db_duration: Optional[float], conn,
                workdir: Path, *, download: bool) -> Audit:
    """The best decodable file for a track, trying each candidate in turn."""
    best = Audit()
    for i, cand in enumerate(candidates(track_id, conn, download=download)):
        wav = workdir / f"{track_id}-{i}.wav"
        audit = decode(cand, wav)
        judge(audit, db_duration)
        if RANK[audit.status] < RANK[best.status]:
            if best.wav is not None:
                best.wav.unlink(missing_ok=True)
            best = audit
        elif audit.wav is not None:
            audit.wav.unlink(missing_ok=True)
        if best.status == "ok":
            break
    if best.status == "missing":
        best.issues.append("no_audio_available")
    return best


def process(track_id: int, track: dict, wav: Path, conn, *,
            skip_sync: bool) -> tuple[list[str], list[str]]:
    """Run each pipeline step the track lacks, on the audited WAV."""
    from . import postprocess_worker, search, track_analysis
    from .postprocess_queue import needs_postprocessing

    done: list[str] = []
    failed: list[str] = []
    pending = needs_postprocessing(track_id, conn)
    artist, title = track["artist"] or "", track["title"] or ""

    if "analysis" in pending:
        ok = postprocess_worker.run_analysis_logic(track_id, wav, conn)
        (done if ok else failed).append("analysis")

    if "chords" in pending:
        ok = postprocess_worker.run_harmony_logic(track_id, wav, conn,
                                                  artist=artist, title=title)
        (done if ok else failed).append("chords")

    if search.clap_vector_for(track_id) is None:
        if not clap_vector.available():
            failed.append("clap(unavailable)")
        else:
            vector = clap_vector.embed_audio(str(wav))
            analysis = track_analysis.get_analysis(track_id, conn)
            key = getattr(analysis, "detected_key", None) if analysis else None
            ok = vector is not None and clap_vector.store(
                track_id, vector, artist=artist, title=title,
                album=track.get("album") or "",
                detected_key=getattr(key, "name", key) or "",
                bpm=analysis.bpm if analysis else None)
            (done if ok else failed).append("clap")

    if "sync" in pending and not skip_sync:
        ok = postprocess_worker.run_sync_logic(track_id, wav, conn)
        (done if ok else failed).append("sync")

    # Last, so the search document picks up the analysis written above.
    if "vectors" in pending or "analysis" in done:
        ok = postprocess_worker.run_vectors_logic(track_id, conn)
        (done if ok else failed).append("vectors")

    if "timings" in pending:
        failed.append("timings(left to worker)")
    return done, failed


def missing_clap_ids(conn) -> list[int]:
    from opensearchpy import helpers
    from .osclient import client as os_client

    embedded = {int(h["_source"]["track_id"]) for h in helpers.scan(
        os_client(), index=clap_vector.CLAP_INDEX,
        query={"_source": ["track_id"], "query": {"match_all": {}}})}
    rows = conn.execute(
        "SELECT track_id FROM tracks ORDER BY play_count DESC NULLS LAST, track_id"
    ).fetchall()
    return [int(r["track_id"]) for r in rows if int(r["track_id"]) not in embedded]


def read_ids(path: Path) -> list[int]:
    with path.open(newline="") as fh:
        return [int(r["track_id"]) for r in csv.DictReader(fh) if r.get("track_id")]


REPORT_FIELDS = ["track_id", "artist", "title", "status", "source_kind",
                 "source_path", "demuxer", "db_duration", "decoded_s", "max_db",
                 "issues", "done", "failed"]


def run(ids: list[int], *, report: Path, dry_run: bool, include_suspect: bool,
        download: bool, skip_sync: bool, limit: Optional[int]) -> int:
    if not dry_run and not clap_vector.available():
        print("CLAP is unavailable: torch and transformers are required.")
        return 1
    conn = localcache.connect()
    tracks = {int(r["track_id"]): r for r in conn.execute(
        "SELECT track_id, artist, title, album, duration FROM tracks"
        " WHERE track_id = ANY(%s)", (ids,)).fetchall()}
    allowed = {"ok", "suspect"} if include_suspect else {"ok"}
    tally: dict[str, int] = {}
    processed = 0

    report.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="reprocess-") as tmp, \
            report.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        for n, track_id in enumerate(ids, start=1):
            track = tracks.get(track_id)
            if track is None:
                continue
            if limit and processed >= limit:
                break
            t0 = time.time()
            audit = audit_track(track_id, track["duration"], conn, Path(tmp),
                                download=download)
            tally[audit.status] = tally.get(audit.status, 0) + 1
            done: list[str] = []
            failed: list[str] = []
            try:
                if not dry_run and audit.status in allowed and audit.wav is not None:
                    done, failed = process(track_id, track, audit.wav, conn,
                                           skip_sync=skip_sync)
                    processed += 1
            except Exception:
                log.exception("reprocess: track %s failed", track_id)
                failed.append("exception")
            finally:
                if audit.wav is not None:
                    audit.wav.unlink(missing_ok=True)
            cand = audit.candidate
            writer.writerow({
                "track_id": track_id, "artist": track["artist"], "title": track["title"],
                "status": audit.status,
                "source_kind": cand.kind if cand else "",
                "source_path": str(cand.path) if cand else "",
                "demuxer": audit.demuxer,
                "db_duration": track["duration"],
                "decoded_s": round(audit.decoded_s, 1) if audit.decoded_s else "",
                "max_db": audit.max_db if audit.max_db is not None else "",
                "issues": ";".join(audit.issues),
                "done": ";".join(done), "failed": ";".join(failed),
            })
            fh.flush()
            print(f"[{n}/{len(ids)}] {track_id:>6} {(track['artist'] or '')[:20]:20} "
                  f"{(track['title'] or '')[:28]:28} {audit.status:8} "
                  f"{','.join(done) or '-':28} {';'.join(audit.issues)} "
                  f"({time.time() - t0:.1f}s)")
    conn.close()
    print(f"\n{tally}  processed={processed}  report={report}")
    return 0
