"""An interactive, terminal-based browser for the karaoke song library."""
from __future__ import annotations

import subprocess
from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, DataTable

from . import localcache

class KaraokeBrowser(App):
    """A Textual app to browse the karaoke song library."""

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("enter", "select_song", "Play Song"),
    ]

    def on_mount(self) -> None:
        """Called when the app is first mounted."""
        table = self.query_one(DataTable)
        table.add_columns("Artist", "Title")
        self.load_songs()

    def compose(self) -> ComposeResult:
        """Create child widgets for the app."""
        yield Header()
        yield DataTable(cursor_type="row")
        yield Footer()

    def load_songs(self) -> None:
        """Load songs from the database and populate the table."""
        table = self.query_one(DataTable)
        with localcache.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT t.artist, t.title, s.url, s.kind
                FROM tracks t
                LEFT JOIN sources s ON t.track_id = s.track_id
                GROUP BY t.track_id
                ORDER BY t.artist, t.title
                """
            )
            rows = cur.fetchall()
            for row in rows:
                table.add_row(row["artist"], row["title"], key=row["url"])

    def action_select_song(self) -> None:
        """Called when the user presses Enter on a song."""
        table = self.query_one(DataTable)
        url = table.get_row_key(table.cursor_row)
        if not url:
            return

        # Determine how to open the URL based on its kind
        with localcache.connect() as conn:
            cur = conn.cursor()
            cur.execute("SELECT kind FROM sources WHERE url = ?", (url,))
            row = cur.fetchone()
            kind = row["kind"] if row else "youtube"

        if kind == "spotify":
            subprocess.run(["playerctl", "open", url])
        else:
            subprocess.run(["xdg-open", url])


def browse_main() -> int:
    """Run the karaoke browser TUI."""
    app = KaraokeBrowser()
    app.run()
    return 0
