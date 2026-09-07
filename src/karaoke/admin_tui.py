"""Backend Operations & Admin TUI for Karaoke Platform.

Provides live Celery worker / Flower dashboard management,
error log diagnostics, and file ingestion / folder scan triggers.
"""
from __future__ import annotations

import os
import sys
import subprocess
from typing import Any
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Header, Footer, DataTable, Static, Button, Input, OptionList
from textual.containers import Vertical, Horizontal, Container
from textual import log as textual_log

from .api_client import ApiClient
from .logger import LOG_FILE, log

__all__ = ["KaraokeAdminApp", "admin_main"]


class KaraokeAdminApp(App):
    """Admin and Operations Dashboard for Karaoke Backend."""

    TITLE = "Karaoke Platform Operations & Worker Control"
    SUB_TITLE = "Manage Workers · Error Logs · Ingestion Pipeline"

    # Every panel gets a bounded height and its own scroll region, so a long
    # error traceback stays inside #log-container instead of spilling over the
    # panels above and below it (the report that started this rework).
    CSS = """
    #admin-workspace { layout: vertical; height: 1fr; overflow-y: auto; }

    #worker-container, #pipeline-container, #align-container,
    #ingest-container, #clients-container, #log-container {
        border: round $primary; padding: 0 1; margin-bottom: 1;
        height: auto;
    }

    #worker-title { text-style: bold; }
    #worker-controls, #pipeline-controls { height: auto; margin-bottom: 1; }
    #worker-controls Button, #pipeline-controls Button { margin-right: 2; }

    /* Bounded so a long unit list cannot push the pipeline panel off-screen. */
    #worker-table { height: 5; margin-bottom: 1; }
    #worker-summary { height: auto; color: $text-muted; }

    #clients-container { height: auto; }
    #clients-title { text-style: bold; }
    #clients-table { height: 6; }
    #clients-summary { height: 1; color: $text-muted; }

    #align-container Horizontal, #ingest-container Horizontal { height: auto; }
    #align-container Input { margin-right: 2; }
    #ingest-container Input { margin-right: 2; }

    /* The error log lives in its own fixed-height, scrollable box. This is the
       fix for the log spilling over/under neighbouring panels: overflow is
       clipped to the container and #error-list scrolls inside it. */
    #log-container { height: 1fr; min-height: 6; }
    #log-title { text-style: bold; }
    #error-list { height: 1fr; overflow-y: auto; border: none; }
    """


    BINDINGS = [
        ("q", "quit", "Quit"),
        Binding("ctrl+q", "quit", "Quit", show=False, priority=True),
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
        Binding("escape", "handle_escape", "Unfocus / Quit", show=False, priority=True),
        ("r", "refresh_all", "Refresh"),
        ("plus", "scale_up", "Worker +1"),
        ("minus", "scale_down", "Worker -1"),
        ("R", "restart_workers", "Restart Workers"),
        ("b", "run_backfill", "Run Audio Backfill"),
        ("v", "rebuild_vectors", "Rebuild Vectors"),
        ("a", "analyse_recordings", "Analyse Recordings"),
        ("w", "align_whisper", "Whisper Align"),
        ("s", "scan_folder", "Scan Folder"),
        ("e", "fetch_errors", "Error Logs"),
        ("c", "refresh_clients", "Refresh Clients"),
        ("X", "shutdown_webui", "Stop Web UI"),
    ]

    def __init__(self):
        super().__init__()
        self.api = ApiClient()
        self._target_workers = 1
        self._bg_busy = False
        self._bg_lock = None

    def _acquire_bg_lock(self) -> bool:
        # In-process guard first (fast path), then a cross-process lockfile so a
        # concurrent run from another karaoke process (CLI, second TUI, cron) is
        # also refused rather than double-writing indices.
        if getattr(self, "_bg_busy", False):
            self.notify("Another task is currently running. Please wait...", severity="warning")
            return False
        from .lockfile import ProcessLock
        lock = ProcessLock("admin_pipeline")
        if not lock.acquire():
            self.notify("A pipeline task is running in another process. Please wait...", severity="warning")
            return False
        self._bg_lock = lock
        self._bg_busy = True
        return True

    def _release_bg_lock(self) -> None:
        self._bg_busy = False
        lock = getattr(self, "_bg_lock", None)
        if lock is not None:
            lock.release()
            self._bg_lock = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="admin-workspace"):
            # Top: Worker Pool Status & Scaling Controls
            with Container(id="worker-container"):
                yield Static("[bold cyan]Post-Processing Workers (Celery + Flower)[/bold cyan]", id="worker-title")
                with Horizontal(id="worker-controls"):
                    yield Button("Start Worker (+)", id="btn-scale-up", variant="success")
                    yield Button("Stop Worker (-)", id="btn-scale-down", variant="warning")
                    yield Button("Restart Workers (R)", id="btn-restart-workers", variant="primary")
                    yield Button("Stop Web UI (X)", id="btn-stop-webui", variant="default")
                    yield Button("Quit (q)", id="btn-quit", variant="error")
                yield DataTable(id="worker-table", cursor_type="row")
                yield Static("Worker Status: Loading...", id="worker-summary")

            # Connected clients: web-UI sessions and active playback sessions.
            with Container(id="clients-container"):
                yield Static("[bold cyan]Connected Clients (Web UI + Playback)[/bold cyan]", id="clients-title")
                yield DataTable(id="clients-table", cursor_type="row")
                yield Static("Clients: Loading...", id="clients-summary")

            # Middle: Audio Processing & Vector Ingestion Controls
            with Container(id="pipeline-container"):
                yield Static("[bold cyan]Audio Processing & Vector Ingestion Pipeline[/bold cyan]")
                with Horizontal(id="pipeline-controls"):
                    yield Button("Run Audio Backfill (b)", id="btn-backfill", variant="success")
                    yield Button("Rebuild Vectors (v)", id="btn-rebuild-vectors", variant="primary")
                    yield Button("Analyse Recordings (a)", id="btn-recordings", variant="warning")

            # Whisper Plain Lyrics Alignment Panel
            with Container(id="align-container"):
                yield Static("[bold cyan]Whisper Lyrics Alignment (Paste Text or Path to lyrics.txt)[/bold cyan]")
                with Horizontal():
                    yield Input(placeholder="Track ID or Artist - Title...", id="align-track-input")
                    yield Input(placeholder="Plain lyrics text OR path to file.txt...", id="align-text-input")
                    yield Button("Whisper Align (w)", id="btn-align", variant="success")

            # File Upload / Ingestion Panel
            with Container(id="ingest-container"):
                yield Static("[bold cyan]File Ingestion & Folder Scan[/bold cyan]")
                with Horizontal():
                    yield Input(placeholder="Path to folder or audio file (e.g. ~/Music)...", id="ingest-input")
                    yield Button("Scan Folder", id="btn-scan", variant="primary")

            # Bottom: Live Error Log Diagnostics
            with Container(id="log-container"):
                yield Static("[bold red]Recent Error Logs & Diagnostics[/bold red]", id="log-title")
                yield OptionList(id="error-list")

        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#worker-table", DataTable)
        table.add_columns("Worker Unit", "State", "Worker ID")
        clients = self.query_one("#clients-table", DataTable)
        clients.add_columns("Kind", "Client / Session", "Detail", "Status")
        self.refresh_all()
        self.set_interval(3.0, self.refresh_workers)
        self.set_interval(5.0, self.refresh_clients)

    def refresh_all(self) -> None:
        self.refresh_workers()
        self.refresh_clients()
        self.refresh_errors()


    def refresh_workers(self) -> None:
        try:
            res = self.api._http_get(self.api.ctrl_url, "/api/workers/status")
            if not res or res.get("status") != "ok":
                res = self._direct_worker_status()
        except Exception:
            res = self._direct_worker_status()

        if not res:
            return

        table = self.query_one("#worker-table", DataTable)
        table.clear()
        units = res.get("units", [])
        active_count = 0
        worker_active = False
        for u in units:
            uname = u.get("unit", "")
            is_act = bool(u.get("active"))
            st = "[bold green]ACTIVE[/bold green]" if is_act else "[dim]inactive[/dim]"
            if is_act:
                active_count += 1
            if uname == "karaoke-celery-worker.service" and is_act:
                worker_active = True
            table.add_row(uname, st, str(u.get("worker_id", "")))

        self._target_workers = 1 if worker_active else 0
        cpu = res.get("worker_cpu") or 0.0
        ram = res.get("worker_memory_mb") or 0.0
        q_len = res.get("queue_depth") or 0
        dash = res.get("dashboard_url", "http://127.0.0.1:5555")
        summary = f"[bold green]Celery services:[/bold green] {active_count} active | [bold yellow]RAM:[/bold yellow] {ram:.1f} MB | [bold cyan]Queue Depth:[/bold cyan] {q_len} jobs | Flower: {dash}"
        self.query_one("#worker-summary", Static).update(summary)

    def _direct_worker_status(self) -> dict[str, Any]:
        """Direct systemd fallback when ctrl_api HTTP is unreachable."""
        units = []
        active_count = 0
        names = ["karaoke-celery-worker.service", "karaoke-celery-flower.service"]
        for i, uname in enumerate(names, start=1):
            try:
                r = subprocess.run(["systemctl", "--user", "is-active", uname], capture_output=True, text=True, timeout=2)
                act = r.stdout.strip() == "active"
            except Exception:
                act = False
            if act:
                active_count += 1
            units.append({"unit": uname, "worker_id": i, "active": act, "kind": "celery"})
        return {
            "status": "ok",
            "orchestrator": "celery",
            "dashboard_url": "http://127.0.0.1:5555",
            "workers_active": active_count,
            "worker_cpu": 0.0,
            "worker_memory_mb": 0.0,
            "queue_depth": 0,
            "units": units,
        }

    def refresh_errors(self) -> None:
        try:
            res = self.api._http_get(self.api.ctrl_url, "/api/logs/errors?lines=30")
            error_list = self.query_one("#error-list", OptionList)
            error_list.clear_options()
            if res and res.get("logs"):
                for err in res["logs"]:
                    error_list.add_option(str(err))
            else:
                error_list.add_option("[dim]No recent system errors found.[/dim]")
        except Exception as exc:
            log.debug("refresh_errors failed", exc_info=True)

    # -- connected clients ------------------------------------------------
    WEBUI_PORT = int(os.environ.get("PORT", "8001"))

    def _webui_clients(self) -> list[dict[str, Any]]:
        """Web-UI (textual-serve) sessions, discovered from listeners on the port.

        The web TUI is served by scripts/web_serve.py (textual-serve) with one
        websocket per browser tab. There is no HTTP status endpoint, so count
        the ESTABLISHED connections to the serve port off `ss` — best-effort and
        never fatal when `ss` is missing or the server is down.
        """
        rows: list[dict[str, Any]] = []
        try:
            out = subprocess.run(
                ["ss", "-Htn", "state", "established", f"( sport = :{self.WEBUI_PORT} )"],
                capture_output=True, text=True, timeout=2,
            ).stdout
        except Exception:
            return rows
        for i, line in enumerate((out or "").splitlines(), start=1):
            parts = line.split()
            peer = parts[-1] if parts else "?"
            rows.append({"kind": "web-ui", "client": f"tab {i}",
                         "detail": peer, "status": "connected"})
        return rows

    def _playback_clients(self) -> list[dict[str, Any]]:
        """Active playback sessions from the control API (best-effort)."""
        rows: list[dict[str, Any]] = []
        try:
            res = self.api.list_play_sessions()
        except Exception:
            return rows
        for s in (res or {}).get("sessions", []):
            title = s.get("title") or s.get("url") or s.get("session_id", "?")
            rows.append({"kind": "playback",
                         "client": str(s.get("session_id", "?"))[:16],
                         "detail": str(title)[:40],
                         "status": s.get("status", "")})
        return rows

    def refresh_clients(self) -> None:
        try:
            table = self.query_one("#clients-table", DataTable)
        except Exception:
            return
        web = self._webui_clients()
        play = self._playback_clients()
        table.clear()
        for c in web + play:
            table.add_row(c["kind"], c["client"], c["detail"], c["status"])
        summary = (f"[bold cyan]Web UI:[/bold cyan] {len(web)} tab(s) on :{self.WEBUI_PORT}"
                   f" | [bold green]Playback:[/bold green] {len(play)} session(s)")
        self.query_one("#clients-summary", Static).update(summary)

    def action_refresh_clients(self) -> None:
        self.refresh_clients()

    def action_shutdown_webui(self) -> None:
        """`X`: gracefully stop the web-UI (textual-serve) server, if running.

        The web TUI has no systemd unit — it is `scripts/web_serve.py` started by
        hand. Signal it politely (SIGTERM) so open browser tabs get a clean
        socket close rather than a reset. Never touches this admin process.
        """
        killed = 0
        try:
            out = subprocess.run(
                ["pgrep", "-f", "web_serve.py"], capture_output=True, text=True, timeout=2
            ).stdout
            for pid in out.split():
                try:
                    os.kill(int(pid), 15)  # SIGTERM: let textual-serve close sockets
                    killed += 1
                except (ProcessLookupError, ValueError, PermissionError):
                    pass
        except Exception as exc:
            self.notify(f"Web UI shutdown failed: {exc}", severity="error")
            return
        if killed:
            self.notify(f"Web UI stopped ({killed} server process(es) signalled)")
        else:
            self.notify("No running web UI server found", severity="warning")
        self.refresh_clients()

    def action_scale_up(self) -> None:
        self._target_workers = 1
        self._scale_workers(self._target_workers)

    def action_scale_down(self) -> None:
        self._target_workers = 0
        self._scale_workers(self._target_workers)

    def _scale_workers(self, target: int) -> None:
        res = self.api._http_post(self.api.ctrl_url, "/api/workers/scale", {"target": target})
        if res and res.get("status") == "ok":
            self.notify("Celery worker started" if target else "Celery worker stopped")
            try:
                self.refresh_workers()
            except Exception:
                pass
        else:
            self.notify("Failed to update worker scaling", severity="error")

    def action_restart_workers(self) -> None:
        self._scale_workers(self._target_workers or 1)
        self.notify("Restarted Celery postprocess worker")

    def action_scan_folder(self) -> None:
        inp = self.query_one("#ingest-input", Input)
        folder = inp.value.strip() or "~/Music"
        res = self.api._http_post(self.api.ctrl_url, "/api/scan/folder", {"dir": folder})
        if res and res.get("status") in ("accepted", "ok"):
            self.notify(f"Triggered folder scan for {folder}")
        else:
            self.notify(f"Folder scan failed for {folder}", severity="error")

    def action_run_backfill(self) -> None:
        """`b`: Trigger audio gap-fill and zero-shot genre backfill."""
        if not self._acquire_bg_lock():
            return
        self.notify("Started audio gap-fill and classification backfill...")
        def _bg():
            try:
                import subprocess
                p = Path(__file__).resolve().parent.parent.parent / "scripts" / "fill_analysis_and_vector_gaps.py"
                if p.is_file():
                    subprocess.run([sys.executable, str(p)], check=True, timeout=600)
                    self.call_from_thread(self.notify, "Audio backfill completed successfully!")
            except Exception as exc:
                self.call_from_thread(self.notify, f"Backfill failed: {exc}", severity="error")
            finally:
                self.call_from_thread(self._release_bg_lock)
        self.run_worker(_bg, thread=True)

    def action_rebuild_vectors(self) -> None:
        """`v`: Rebuild OpenSearch vector indices."""
        if not self._acquire_bg_lock():
            return
        self.notify("Rebuilding OpenSearch vector indices...")
        def _bg():
            try:
                from . import vector_index, localcache
                with localcache.connect() as conn:
                    st = vector_index.rebuild_from_sqlite(embed=True, include_lines=True)
                msg = f"Vector indices updated: {st.indexed} tracks, {st.line_docs} lines"
                self.call_from_thread(self.notify, msg)
            except Exception as exc:
                self.call_from_thread(self.notify, f"Vector rebuild note: {exc}")
            finally:
                self.call_from_thread(self._release_bg_lock)
        self.run_worker(_bg, thread=True)

    def action_analyse_recordings(self) -> None:
        """`a`: Process, decompile, and ingest detected song vectors from recordings."""
        if not self._acquire_bg_lock():
            return
        self.notify("Analysing captured audio recordings and ingesting song vectors...")
        def _bg():
            try:
                from . import recording_worker, localcache, vector_index
                with localcache.connect() as conn:
                    rows = conn.execute("SELECT recording_id FROM recordings WHERE status != 'recording' ORDER BY recording_id DESC LIMIT 20").fetchall()
                    processed = 0
                    for r in rows:
                        rid = int(r["recording_id"])
                        res = recording_worker.analyse(rid)
                        if res:
                            processed += 1
                    if processed:
                        vector_index.rebuild_from_sqlite(embed=True, include_lines=True, include_notes=True)
                self.call_from_thread(self.notify, f"Processed {processed} recording(s) and ingested song vectors")
            except Exception as exc:
                self.call_from_thread(self.notify, f"Recording analysis note: {exc}")
            finally:
                self.call_from_thread(self._release_bg_lock)
        self.run_worker(_bg, thread=True)

    def action_align_whisper(self) -> None:
        """`w`: Align plain lyrics text or uploaded lyrics file using Whisper."""
        track_input = self.query_one("#align-track-input", Input).value.strip()
        text_input = self.query_one("#align-text-input", Input).value.strip()
        if not track_input or not text_input:
            self.notify("Provide track identifier and lyrics text/file path", severity="warning")
            return
        if not self._acquire_bg_lock():
            return
        self.notify(f"Aligning lyrics for '{track_input}' with Whisper...")
        def _bg():
            try:
                res = align_plain_text_for_track(track_input, text_input)
                self.call_from_thread(self.notify, res)
            except Exception as exc:
                self.call_from_thread(self.notify, f"Alignment failed: {exc}", severity="error")
            finally:
                self.call_from_thread(self._release_bg_lock)
        self.run_worker(_bg, thread=True)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-scale-up":
            self.action_scale_up()
        elif bid == "btn-scale-down":
            self.action_scale_down()
        elif bid == "btn-restart-workers":
            self.action_restart_workers()
        elif bid == "btn-backfill":
            self.action_run_backfill()
        elif bid == "btn-rebuild-vectors":
            self.action_rebuild_vectors()
        elif bid == "btn-recordings":
            self.action_analyse_recordings()
        elif bid == "btn-align":
            self.action_align_whisper()
        elif bid == "btn-scan":
            self.action_scan_folder()
        elif bid == "btn-stop-webui":
            self.action_shutdown_webui()
        elif bid == "btn-quit":
            self.exit()

    def action_handle_escape(self) -> None:
        """Escape: drop focus from an input first, otherwise quit.

        A bare `q` cannot quit while a text Input (Whisper track / lyrics /
        folder path) holds focus — it types the letter instead. Escape unfocuses
        so `q` works again, and quits outright when nothing is focused.
        """
        focused = self.focused
        if focused is not None and isinstance(focused, Input):
            self.set_focus(None)
            return
        self.exit()


def align_plain_text_for_track(track_identifier: str, lyrics_or_file_path: str) -> str:
    """Save plain lyrics text or read a lyrics file, run Whisper alignment, and store time-synced LRC."""
    from pathlib import Path
    from . import localcache, postprocess_worker, youtube, vector_index

    p = Path(lyrics_or_file_path.strip()).expanduser()
    if p.is_file():
        plain_text = p.read_text(encoding="utf-8", errors="replace")
    else:
        plain_text = lyrics_or_file_path.strip()

    if not plain_text:
        raise ValueError("No lyrics text or valid file path provided.")

    with localcache.connect() as conn:
        track_id = None
        ident = track_identifier.strip()
        if ident.isdigit():
            track_id = int(ident)
        else:
            if " - " in ident:
                a, t = ident.split(" - ", 1)
                track_id = localcache.find_track_id(a, t, conn)
            if track_id is None:
                row = conn.execute(
                    "SELECT track_id FROM tracks WHERE artist LIKE ? OR title LIKE ? LIMIT 1",
                    (f"%{ident}%", f"%{ident}%")
                ).fetchone()
                if row:
                    track_id = row["track_id"]

        if not track_id:
            raise ValueError(f"Could not find track matching '{track_identifier}'")

        track_row = conn.execute("SELECT artist, title FROM tracks WHERE track_id = ?", (track_id,)).fetchone()
        artist, title = track_row["artist"], track_row["title"]

        localcache.add_track_and_lyrics(
            artist, title, localcache.Lyrics(synced_raw="", plain=plain_text, source="user_input"), conn=conn
        )

        source_row = conn.execute(
            "SELECT url, kind FROM sources WHERE track_id = ? ORDER BY source_id LIMIT 1",
            (track_id,)
        ).fetchone()

        audio_path = None
        if source_row:
            url, kind = source_row["url"], source_row["kind"]
            if kind == "local" and Path(url).is_file():
                audio_path = Path(url)
            elif url and ("youtube" in kind or "http" in url):
                audio_path = youtube.download(url)

        if not audio_path or not Path(audio_path).is_file():
            raise ValueError(f"Could not locate or download audio for '{artist} - {title}' to align against.")

        ok = postprocess_worker._run_sync(track_id, Path(audio_path), conn)
        if not ok:
            raise RuntimeError(f"Whisper alignment produced no output for '{artist} - {title}'")

        vector_index.rebuild_from_sqlite(embed=True, include_lines=True)
        return f"Successfully aligned lyrics for #{track_id}: {artist} - {title}"


def admin_main() -> None:
    """CLI entrypoint for running the admin operations TUI."""
    app = KaraokeAdminApp()
    app.run()


if __name__ == "__main__":
    admin_main()
