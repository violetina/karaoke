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
        from .api_client import ApiClient
        self.api = ApiClient()
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
        """Load songs using ApiClient (HTTP when server up, SQLite fallback)."""
        table = self.query_one(DataTable)
        tracks = self.api.list_tracks(limit=5000)
        if tracks:
            for t in tracks:
                self._song_data.append({
                    'artist': t.get('artist', ''),
                    'title': t.get('title', ''),
                    'url': t.get('url', ''),
                    'kind': t.get('kind', ''),
                })
                table.add_row(t.get('artist', ''), t.get('title', ''))
            return

        # Direct DB fallback if API returns empty
        with localcache.connect() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT t.artist, t.title, s.url, s.kind
                FROM tracks t
                LEFT JOIN sources s ON s.source_id = (
                    SELECT s2.source_id FROM sources s2
                    WHERE s2.track_id = t.track_id
                    ORDER BY
                        CASE
                            WHEN s2.kind = 'youtube_music' THEN 0
                            WHEN s2.kind = 'youtube' THEN 1
                            WHEN s2.url LIKE 'http%' THEN 2
                            WHEN s2.kind = 'spotify' THEN 3
                            ELSE 4
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

        res = self.api.play(url=url, kind=kind, artist=artist, title=title)
        if res.get("status") in ("launched", "ok"):
            self.notify(f"Opening {artist} - {title}")
        else:
            self.notify(f"Play failed for {artist} - {title}", severity="warning")
            log.warning("action_select_song: Failed to launch player for %s: %s", url, res.get("detail"))
            self.notify(f"Error launching URL; see {LOG_FILE}", severity="error")


def browse_main() -> int:
    """Run the karaoke browser TUI."""
    app = KaraokeBrowser()
    app.run()
    return 0

if __name__ == "__main__":
    browse_main()
