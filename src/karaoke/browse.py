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

    def __init__(self):
        super().__init__()
        self._song_data = []

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
                self._song_data.append({'url': row['url'], 'kind': row['kind']})
                table.add_row(row["artist"], row["title"])

    def action_select_song(self) -> None:
        """Called when the user presses Enter on a song."""
        table = self.query_one(DataTable)
        song = self._song_data[table.cursor_row]
        
        url, kind = song.get('url'), song.get('kind')

        if not url:
            return

        if kind == "spotify":
            subprocess.run(["playerctl", "open", url])
        else:
            subprocess.run(["xdg-open", url])


def browse_main() -> int:
    """Run the karaoke browser TUI."""
    app = KaraokeBrowser()
    app.run()
    return 0

if __name__ == "__main__":
    browse_main()
