"""An interactive, terminal-based browser for the karaoke song library."""
from __future__ import annotations
import subprocess
from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, DataTable
from textual import log as textual_log

from . import localcache
from .logger import log

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
                self._song_data.append({
                    'artist': row['artist'],
                    'title': row['title'],
                    'url': row['url'],
                    'kind': row['kind'],
                })
                table.add_row(row["artist"], row["title"])

    def action_select_song(self) -> None:
        """Called when the user presses Enter on a song."""
        table = self.query_one(DataTable)
        try:
            song = self._song_data[table.cursor_row]
        except IndexError:
            log.error(f"action_select_song: No song data at row {table.cursor_row}")
            return
            
        url, kind = song.get('url'), song.get('kind')
        log.info(f"action_select_song: Selected row {table.cursor_row}: url={url}, kind={kind}")

        if not url:
            artist = song.get('artist') or ''
            title = song.get('title') or ''
            query = f"{artist} {title}".strip().replace(" ", "+")
            if not query:
                log.warning("action_select_song: No url, artist, or title available to search.")
                return
            url = f"https://www.youtube.com/results?search_query={query}"
            kind = "youtube_search"
            log.info(f"action_select_song: Falling back to youtube search: {url}")

        try:
            if kind == "spotify":
                log.debug(f"Executing: playerctl open {url}")
                subprocess.run(["playerctl", "open", url], check=True)
            else:
                log.debug(f"Executing: xdg-open {url}")
                # xdg-open often detaches, but we don't want to block the TUI
                subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            log.info("action_select_song: Command launched successfully.")
        except Exception as e:
            log.exception(f"action_select_song: Failed to launch player for {url}")
            textual_log(f"Error launching: {e}")


def browse_main() -> int:
    """Run the karaoke browser TUI."""
    app = KaraokeBrowser()
    app.run()
    return 0

if __name__ == "__main__":
    browse_main()
