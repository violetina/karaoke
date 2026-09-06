"""Backend Operations & Admin TUI for Karaoke Platform.

Provides live worker pool management (karaoke-postprocess@1..6.service),
error log diagnostics, and file ingestion / folder scan triggers.
"""
from __future__ import annotations

import os
import sys
import subprocess
from typing import Any
from pathlib import Path

from textual.app import App, ComposeResult
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

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh_all", "Refresh"),
        ("plus", "scale_up", "Worker +1"),
        ("minus", "scale_down", "Worker -1"),
        ("R", "restart_workers", "Restart Workers"),
        ("s", "scan_folder", "Scan Folder"),
        ("e", "fetch_errors", "Error Logs"),
    ]

    def __init__(self):
        super().__init__()
        self.api = ApiClient()
        self._target_workers = 1

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="admin-workspace"):
            # Top: Worker Pool Status & Scaling Controls
            with Container(id="worker-container"):
                yield Static("[bold cyan]Post-Processing Workers (systemd karaoke-postprocess@1..6.service)[/bold cyan]", id="worker-title")
                with Horizontal(id="worker-controls"):
                    yield Button("Scale Up (+1)", id="btn-scale-up", variant="success")
                    yield Button("Scale Down (-1)", id="btn-scale-down", variant="warning")
                    yield Button("Restart Workers", id="btn-restart-workers", variant="error")
                yield DataTable(id="worker-table", cursor_type="row")
                yield Static("Worker Status: Loading...", id="worker-summary")

            # Middle: File Upload / Ingestion Panel
            with Container(id="ingest-container"):
                yield Static("[bold cyan]File Ingestion & Folder Scan[/bold cyan]")
                with Horizontal():
                    yield Input(placeholder="Path to folder or audio file (e.g. ~/Music)...", id="ingest-input")
                    yield Button("Scan Folder", id="btn-scan", variant="primary")

            # Bottom: Live Error Log Diagnostics
            with Container(id="log-container"):
                yield Static("[bold red]Recent Error Logs & Diagnostics[/bold red]")
                yield OptionList(id="error-list")

        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#worker-table", DataTable)
        table.add_columns("Worker Unit", "State", "Worker ID")
        self.refresh_all()
        self.set_interval(3.0, self.refresh_workers)

    def refresh_all(self) -> None:
        self.refresh_workers()
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
        for u in units:
            uname = u.get("unit", "")
            is_act = bool(u.get("active"))
            st = "[bold green]ACTIVE[/bold green]" if is_act else "[dim]inactive[/dim]"
            if is_act:
                active_count += 1
            table.add_row(uname, st, str(u.get("worker_id", "")))

        self._target_workers = active_count or 1
        cpu = res.get("worker_cpu") or 0.0
        ram = res.get("worker_memory_mb") or 0.0
        q_len = res.get("queue_depth") or 0
        summary = f"[bold green]Active Workers:[/bold green] {active_count}/6 | [bold yellow]RAM:[/bold yellow] {ram:.1f} MB | [bold cyan]Queue Depth:[/bold cyan] {q_len} jobs"
        self.query_one("#worker-summary", Static).update(summary)

    def _direct_worker_status(self) -> dict[str, Any]:
        """Direct systemd fallback when ctrl_api HTTP is unreachable."""
        units = []
        active_count = 0
        for i in range(1, 7):
            uname = f"karaoke-postprocess@{i}.service"
            try:
                r = subprocess.run(["systemctl", "--user", "is-active", uname], capture_output=True, text=True, timeout=2)
                act = r.stdout.strip() == "active"
            except Exception:
                act = False
            if act:
                active_count += 1
            units.append({"unit": uname, "worker_id": i, "active": act})
        return {
            "status": "ok",
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

    def action_scale_up(self) -> None:
        self._target_workers = min(6, self._target_workers + 1)
        self._scale_workers(self._target_workers)

    def action_scale_down(self) -> None:
        self._target_workers = max(0, self._target_workers - 1)
        self._scale_workers(self._target_workers)

    def _scale_workers(self, target: int) -> None:
        res = self.api._http_post(self.api.ctrl_url, "/api/workers/scale", {"target": target})
        if res and res.get("status") == "ok":
            self.notify(f"Worker pool target set to {target}")
            try:
                self.refresh_workers()
            except Exception:
                pass
        else:
            self.notify("Failed to update worker scaling", severity="error")

    def action_restart_workers(self) -> None:
        self._scale_workers(self._target_workers or 1)
        self.notify("Restarted postprocess worker pool")

    def action_scan_folder(self) -> None:
        inp = self.query_one("#ingest-input", Input)
        folder = inp.value.strip() or "~/Music"
        res = self.api._http_post(self.api.ctrl_url, "/api/scan/folder", {"dir": folder})
        if res and res.get("status") in ("accepted", "ok"):
            self.notify(f"Triggered folder scan for {folder}")
        else:
            self.notify(f"Folder scan failed for {folder}", severity="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn-scale-up":
            self.action_scale_up()
        elif bid == "btn-scale-down":
            self.action_scale_down()
        elif bid == "btn-restart-workers":
            self.action_restart_workers()
        elif bid == "btn-scan":
            self.action_scan_folder()


def admin_main() -> None:
    """CLI entrypoint for running the admin operations TUI."""
    app = KaraokeAdminApp()
    app.run()


if __name__ == "__main__":
    admin_main()
