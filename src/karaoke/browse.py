"""An interactive, terminal-based browser for the karaoke song library."""
from __future__ import annotations
import subprocess
from urllib.parse import quote_plus
from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, DataTable
from textual import log as textual_log

from . import localcache
from .logger import LOG_FILE, OPEN_STDERR_LOG, OPEN_STDOUT_LOG, log
# Re-exported for backwards compatibility; the implementation now lives in
# player_open so non-TUI callers need not import Textual.
from .player_open import open_song_url

__all__ = ["open_song_url", "KaraokeBrowser", "browse_main"]


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
            # Prefer a browser-openable source (youtube/http) over spotify so
            # Enter opens the song in the browser rather than depending on the
            # Spotify desktop app. Selection is deterministic per track.
            cur.execute(
                """
                SELECT t.artist, t.title, s.url, s.kind
                FROM tracks t
                LEFT JOIN sources s ON s.source_id = (
                    SELECT s2.source_id FROM sources s2
                    WHERE s2.track_id = t.track_id
                    ORDER BY
                        CASE
                            WHEN s2.kind = 'youtube' THEN 0
                            WHEN s2.url LIKE 'http%' THEN 1
                            WHEN s2.kind = 'spotify' THEN 2
                            ELSE 3
                        END,
                        s2.source_id
                    LIMIT 1
                )
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
        artist = song.get('artist') or ''
        title = song.get('title') or ''
        log.info(
            "action_select_song: row=%s artist=%r title=%r url=%r kind=%r log=%s",
            table.cursor_row,
            artist,
            title,
            url,
            kind,
            LOG_FILE,
        )

        if not url:
            query = quote_plus(f"{artist} {title}".strip())
            if not query:
                log.warning("action_select_song: No url, artist, or title available to search.")
                self.notify("No URL or search terms for this row", severity="warning")
                return
            url = f"https://www.youtube.com/results?search_query={query}"
            kind = "youtube_search"
            log.info("action_select_song: Falling back to youtube search: %s", url)

        try:
            pid = open_song_url(url, kind)
            if pid is None:
                self.notify(f"Opened {artist} - {title}")
            else:
                self.notify(f"Opening {artist} - {title} (pid {pid})")
            log.info("action_select_song: launch requested successfully pid=%s", pid)
        except Exception as e:
            log.exception("action_select_song: Failed to launch player for %s", url)
            self.notify(f"Error launching URL; see {LOG_FILE}", severity="error")
            textual_log(f"Error launching: {e}")


def browse_main() -> int:
    """Run the karaoke browser TUI."""
    app = KaraokeBrowser()
    app.run()
    return 0

if __name__ == "__main__":
    browse_main()
