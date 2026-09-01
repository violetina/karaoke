"""An interactive, terminal-based browser for the karaoke song library."""
from __future__ import annotations
import subprocess
from urllib.parse import quote_plus
from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, DataTable
from textual import log as textual_log

from . import localcache
from .logger import LOG_FILE, OPEN_STDERR_LOG, OPEN_STDOUT_LOG, log


def open_song_url(url: str, kind: str | None) -> int | None:
    """Open a song URL and return the spawned process id when applicable.

    YouTube/browser URLs are opened asynchronously so the TUI remains responsive.
    stdout/stderr are captured to log files so xdg-open failures are debuggable.
    """
    if kind == "spotify":
        log.debug("Executing: playerctl open %s", url)
        completed = subprocess.run(
            ["playerctl", "open", url],
            check=True,
            capture_output=True,
            text=True,
        )
        if completed.stdout:
            log.debug("playerctl stdout: %s", completed.stdout.strip())
        if completed.stderr:
            log.debug("playerctl stderr: %s", completed.stderr.strip())
        return None

    OPEN_STDOUT_LOG.parent.mkdir(parents=True, exist_ok=True)
    stdout = OPEN_STDOUT_LOG.open("ab")
    stderr = OPEN_STDERR_LOG.open("ab")
    try:
        log.debug("Executing: xdg-open %s", url)
        proc = subprocess.Popen(["xdg-open", url], stdout=stdout, stderr=stderr)
        log.info(
            "xdg-open spawned pid=%s stdout=%s stderr=%s",
            proc.pid,
            OPEN_STDOUT_LOG,
            OPEN_STDERR_LOG,
        )
        return proc.pid
    finally:
        stdout.close()
        stderr.close()


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
