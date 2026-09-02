"""Clean Textual control-surface prototype for the karaoke app.

This is intentionally separate from the older ``browse`` TUI. The first slice is
usable for design review: settings/mode controls, a library table, now-playing
metadata, lyric preview, mood square and future ASCII/vector visual panels.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from urllib.parse import quote_plus

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Button, DataTable, Footer, Header, Select, Static

from . import localcache
from .lyrics import Lyrics
from .player import LyricTimeline, render_lines, timeline_from_lyrics
from .sentiment import mood_of


@dataclass(frozen=True)
class LibraryTrack:
    """One row in the TUI library table."""

    track_id: int
    artist: str
    title: str
    source_kind: str
    url: str
    has_synced: bool
    lyric_source: str

    @property
    def label(self) -> str:
        return f"{self.artist} - {self.title}".strip(" -")


MOOD_CLASSES = {
    "happy": "mood-happy",
    "sad": "mood-sad",
    "angry": "mood-angry",
    "tender": "mood-tender",
    "neutral": "mood-neutral",
}

MOOD_GLYPHS = {
    "happy": "+++ sunshine / lift / dance +++",
    "sad": "~~~ rain / blue / distance ~~~",
    "angry": "### fire / pressure / rupture ###",
    "tender": "*** warm / close / heart ***",
    "neutral": "... listening / waiting / breath ...",
}

SAMPLE_TIMELINE = LyricTimeline(
    [
        (0.0, "previous line fades up"),
        (4.0, "current lyric line is highlighted"),
        (8.0, "word playhead moves left to right"),
        (12.0, "next line is visible"),
        (16.0, "next plus one is dim"),
    ]
)


def first_nonempty_line(text: str) -> str:
    """Return the first non-blank line from a lyric/visual seed."""
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def lyric_preview(song: dict[str, object], max_lines: int = 8) -> str:
    """Return a compact lyric preview from a legacy song row dict.

    Kept for tests and for experimenting with the earlier prototype shape; the
    clean TUI itself now uses ``LibraryTrack`` plus ``Lyrics`` objects.
    """
    text = str(song.get("synced_lyrics") or song.get("plain_lyrics") or "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines[:max_lines])


def mood_for_preview(preview: str) -> str:
    """Return the first non-neutral mood in a lyric preview."""
    for line in (preview or "").splitlines():
        mood = mood_of(line)
        if mood != "neutral":
            return mood
    return "neutral"


class KaraokeTui(App):
    """Approved clean-start karaoke TUI design."""

    CSS = """
    Screen {
        background: #10131a;
        color: #f8f9fa;
    }

    #topbar {
        height: 3;
        padding: 0 2;
        background: #6741d9;
        color: white;
        text-style: bold;
    }

    #workspace {
        height: 1fr;
        padding: 1;
    }

    .panel {
        border: round #495057;
        padding: 1 2;
        margin: 0 1 1 0;
        background: #161b22;
    }

    .panel-title {
        text-style: bold;
        color: #d0bfff;
        margin-bottom: 1;
    }

    #settings-panel {
        width: 30;
        border: round #e67700;
    }

    #center-panel {
        width: 1fr;
    }

    #visuals-panel {
        width: 36;
    }

    Select {
        margin-bottom: 1;
    }

    Button {
        margin: 0 1 1 0;
        min-width: 10;
    }

    #library-table {
        height: 11;
        margin-top: 1;
    }

    #now-playing {
        height: 6;
        border: round #087f5b;
        padding: 1 2;
        margin-bottom: 1;
        background: #0b2b26;
    }

    #lyrics {
        height: 1fr;
        border: round #1971c2;
        padding: 1 2;
        background: #081728;
    }

    #mood-square {
        height: 14;
        border: tall #868e96;
        content-align: center middle;
        text-align: center;
        text-style: bold;
        margin-bottom: 1;
    }

    .mood-happy {
        background: #2b8a3e;
        color: #ebfbee;
        border: tall #69db7c;
    }

    .mood-sad {
        background: #1864ab;
        color: #e7f5ff;
        border: tall #74c0fc;
    }

    .mood-angry {
        background: #c92a2a;
        color: #fff5f5;
        border: tall #ff8787;
    }

    .mood-tender {
        background: #a61e4d;
        color: #fff0f6;
        border: tall #faa2c1;
    }

    .mood-neutral {
        background: #0b7285;
        color: #e3fafc;
        border: tall #66d9e8;
    }

    #ascii-visuals {
        height: 1fr;
        border: round #868e96;
        padding: 1 2;
        background: #1f242d;
    }

    #status-line {
        dock: bottom;
        height: 1;
        padding: 0 2;
        background: #212529;
        color: #adb5bd;
    }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh_library", "Refresh"),
        ("enter", "open_selected", "Open"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._tracks: list[LibraryTrack] = []
        self._timeline = SAMPLE_TIMELINE
        self._active_index = 1
        self._mood = "neutral"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(
            "KARAOKE  |  Library  Live  Radio  Player  Visuals  Settings",
            id="topbar",
        )
        with Horizontal(id="workspace"):
            with Vertical(id="settings-panel", classes="panel"):
                yield Static("Settings / Config", classes="panel-title")
                yield Static("Mode")
                yield Select(
                    [
                        ("Browse library", "browse"),
                        ("Follow player", "player"),
                        ("Radio / listen / output", "radio"),
                        ("YouTube URL", "youtube"),
                    ],
                    value="player",
                    id="mode-select",
                )
                yield Static("Player target")
                yield Select(
                    [
                        ("Auto", "auto"),
                        ("Spotify", "spotify"),
                        ("VLC", "vlc"),
                        ("Firefox / browser", "firefox"),
                        ("Local file", "file"),
                    ],
                    value="auto",
                    id="player-select",
                )
                yield Static("Sync")
                yield Static("lead: 13.0s\noffset: +0.0s\nnudge: b/v, 0 reset")
                yield Static("Visual profile")
                yield Select(
                    [
                        ("Calm", "calm"),
                        ("Party", "party"),
                        ("Glitch ASCII", "glitch"),
                        ("Semantic visuals", "semantic"),
                    ],
                    value="calm",
                    id="visual-select",
                )
                yield Static("Sources\nSQLite first\nVector store optional")

            with Vertical(id="center-panel"):
                with Container(id="now-playing"):
                    yield Static("Player / now playing", classes="panel-title")
                    yield Static("Select a song or switch to a live mode.", id="now-playing-text")
                with Container(id="lyrics"):
                    yield Static("Actual scrolling lyric text", classes="panel-title")
                    yield Static("", id="lyrics-text")
                yield DataTable(cursor_type="row", id="library-table")

            with Vertical(id="visuals-panel", classes="panel"):
                yield Static("Mood / sentiment square", classes="panel-title")
                yield Static("neutral", id="mood-square")
                yield Static("Text visuals / ASCII later", classes="panel-title")
                yield Static("", id="ascii-visuals")
                with Horizontal():
                    yield Button("Play", id="play-button", variant="success")
                    yield Button("Open", id="open-button", variant="primary")
                    yield Button("Stop", id="stop-button", variant="error")
        yield Static("r refresh - enter/open launch selected source - q quit", id="status-line")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#library-table", DataTable)
        table.add_columns("*", "Artist", "Title", "Source")
        self.load_library()
        self._render_current_state()

    def load_library(self) -> None:
        """Load library rows from SQLite into the table."""
        self._tracks = load_library_tracks()
        table = self.query_one("#library-table", DataTable)
        table.clear()
        for track in self._tracks:
            table.add_row(
                "*" if track.has_synced else " ",
                track.artist,
                track.title,
                track.source_kind or "-",
            )
        self.query_one("#status-line", Static).update(
            f"{len(self._tracks)} known songs - r refresh - enter/open launch selected source - q quit"
        )

    def action_refresh_library(self) -> None:
        self.load_library()
        self.notify("Library refreshed")

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        self._select_row(event.cursor_row)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self._select_row(event.cursor_row)
        self.action_open_selected()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "open-button":
            self.action_open_selected()
        elif event.button.id == "play-button":
            self.notify("Play wiring comes next: this design slice is a safe prototype.")
        elif event.button.id == "stop-button":
            self.notify("Stop wiring comes next: no playback process is owned yet.")

    def action_open_selected(self) -> None:
        table = self.query_one("#library-table", DataTable)
        if table.cursor_row is None:
            self.notify("No song selected", severity="warning")
            return
        if not (0 <= table.cursor_row < len(self._tracks)):
            self.notify("No song selected", severity="warning")
            return
        track = self._tracks[table.cursor_row]
        url = track.url or youtube_search_url(track.artist, track.title)
        try:
            launch_source(url, track.source_kind)
        except Exception as exc:  # pragma: no cover - interactive system integration
            self.notify(f"Open failed: {exc}", severity="error")
            return
        self.notify(f"Opening {track.label}")

    def _select_row(self, row_index: int) -> None:
        if not (0 <= row_index < len(self._tracks)):
            return
        track = self._tracks[row_index]
        lyrics = load_track_lyrics(track.track_id)
        if lyrics is not None and lyrics.has_synced:
            self._timeline = timeline_from_lyrics(lyrics)
            self._active_index = max(0, min(1, len(self._timeline.lines) - 1))
        else:
            self._timeline = SAMPLE_TIMELINE
            self._active_index = 1
        if self._timeline.lines:
            self._mood = mood_of(self._timeline.lines[self._active_index][1])
        else:
            self._mood = "neutral"
        self._render_current_state(track)

    def _render_current_state(self, track: LibraryTrack | None = None) -> None:
        track = track or (self._tracks[0] if self._tracks else None)
        if track is None:
            now = "No local songs yet. Use karaoke once or index cached YouTube files."
            title = "Design preview"
        else:
            title = track.label
            source = (
                f"source: {track.source_kind or '-'} - "
                f"lyrics: {'synced' if track.has_synced else 'missing'}"
            )
            now = f"{title}\n{source}\nmode: player - cache: SQLite first"
        self.query_one("#now-playing-text", Static).update(now)

        elapsed = self._timeline.lines[self._active_index][0] if self._timeline.lines else 0.0
        lyrics = render_lines(self._timeline, self._active_index, context=3)
        self.query_one("#lyrics-text", Static).update(lyrics)

        mood_square = self.query_one("#mood-square", Static)
        for cls in MOOD_CLASSES.values():
            mood_square.remove_class(cls)
        mood_square.add_class(MOOD_CLASSES.get(self._mood, "mood-neutral"))
        mood_square.update(f"{self._mood}\n\n{MOOD_GLYPHS.get(self._mood, MOOD_GLYPHS['neutral'])}")

        self.query_one("#ascii-visuals", Static).update(
            "semantic/vector visual lane\n\n"
            f"song: {title}\n"
            f"theme seed: {self._mood}\n"
            f"line time: {elapsed:0.1f}s\n\n"
            "future: lyric meaning -> motif -> text/ASCII renderer\n"
            "offline-safe: SQLite works without vector store"
        )


def load_library_tracks(limit: int = 300) -> list[LibraryTrack]:
    """Return known tracks with one representative source and lyrics state."""
    with localcache.connect() as conn:
        rows = conn.execute(
            """
            SELECT
                t.track_id,
                t.artist,
                t.title,
                COALESCE(MIN(s.kind), '') AS source_kind,
                COALESCE(MIN(s.url), '') AS url,
                MAX(CASE
                    WHEN l.kind = 'approved'
                     AND COALESCE(l.synced_lyrics, '') != '' THEN 1
                    ELSE 0
                END) AS has_synced,
                COALESCE(MAX(l.source), '') AS lyric_source
            FROM tracks t
            LEFT JOIN sources s ON s.track_id = t.track_id
            LEFT JOIN lyrics l ON l.track_id = t.track_id AND l.kind = 'approved'
            GROUP BY t.track_id, t.artist, t.title
            ORDER BY lower(t.artist), lower(t.title)
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [
        LibraryTrack(
            track_id=int(row["track_id"]),
            artist=row["artist"] or "",
            title=row["title"] or "",
            source_kind=row["source_kind"] or "",
            url=row["url"] or "",
            has_synced=bool(row["has_synced"]),
            lyric_source=row["lyric_source"] or "",
        )
        for row in rows
    ]


def load_track_lyrics(track_id: int) -> Lyrics | None:
    """Load approved lyrics for a track id."""
    with localcache.connect() as conn:
        return localcache.get_lyrics_by_track_id(track_id, conn)


def youtube_search_url(artist: str, title: str) -> str:
    """Fallback source URL for rows without a saved source."""
    query = quote_plus(f"{artist} {title}".strip())
    return f"https://www.youtube.com/results?search_query={query}"


def launch_source(url: str, kind: str | None = None) -> None:
    """Open/play a selected source without blocking the TUI."""
    if kind == "spotify" or url.startswith("spotify:"):
        subprocess.Popen(["playerctl", "open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def tui_main() -> int:
    """Run the clean karaoke TUI."""
    KaraokeTui().run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(tui_main())
