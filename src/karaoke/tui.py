"""Player-aware Textual TUI for the karaoke library.

Watches the desktop over MPRIS and picks a mode automatically:

- Spotify playing -> ``spotify``: sync and control Spotify.
- Any other player (browser tab, VLC) -> ``scan``: sync to its position.
- Microphone (``R``) -> ``radio``: songrec identifies room audio, which is the
  one case MPRIS cannot cover since external audio has no player publishing
  metadata. The playhead is dead-reckoned from songrec's own offset.
- Nothing playing -> ``browse``: drive the library list.

Layout is two equal side columns around the lyrics, so they sit centred. The
left column carries cover art, a per-track read-out and worker stats; the right
carries mood, key/BPM and the sentiment/rhythm visuals. Both give way on a small
terminal — the way to get bigger lyrics is a bigger terminal FONT, which costs
cells, so the chrome has to yield. ``F`` hides it entirely.

``H`` opens the library over the lyrics on its own layer, costing them no space;
picking a song closes it. ``?`` shows a key reference generated from BINDINGS so
it cannot drift. ``A`` queues the playing track for post-processing.

Tracks with no cached lyrics are recorded as gaps for backfill, and looked up
again in the background against LRCLIB — the cache only holds what someone has
already fetched.

See ``docs/tui.md``.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import quote_plus

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (Button, DataTable, Footer, Header, Input, Label,
                             Select, Static)

from . import (detect, localcache, playerctl, recorder, sample_audio,
               staging, track_analysis, visuals)
from .browse import open_song_url
from .player_open import browser_playback, track_finished, track_idle
from .logger import LOG_FILE, log, stream_logs
from .musictheory import parse_key
from .player import (DEFAULT_LEAD_S, LyricTimeline, _render_body,
                     timeline_from_lyrics)
from .sentiment import mood_of

SongRow = dict[str, object]
SongMapping = Mapping[str, object]

# How long the player may hold nothing before the queue gives up on it and
# moves on. Long enough that a slow watch-URL load is never mistaken for a
# stall — the watcher itself only ticks every 2s.
IDLE_STALL_S = 10.0

# Mood squares. The art rows are 2 cells wide, but the weather glyphs are not
# all the same width: "☔" and "🔥" are genuinely 2 cells while "☀ ♡ ◇" are 1, so
# without padding those two squares render visibly lopsided under
# `content-align: center`. pad_cells fixes that exactly.
#
# It cannot help East-Asian *ambiguous* glyphs, whose width is terminal- and
# font-dependent rather than defined — see visuals.sentiment_bars.
MOOD_GLYPHS = {
    mood: f"{visuals.pad_cells(glyph, 2)}\n{art}"
    for mood, glyph, art in (
        ("happy", "☀", "╲╱\n╱╲"),
        ("sad", "☔", "░░\n▒▒"),
        ("angry", "🔥", "▓▓\n██"),
        ("tender", "♡", "/\\\n\\/"),
        ("neutral", "◇", "··\n··"),
    )
}

FILTER_OPTIONS = [
    ("Working songs (have lyrics)", "working"),
    ("All songs", "all"),
    ("Staging queue", "staging"),
    ("Spotify tracks", "spotify"),
]

# Manual mode override cycle. None == auto-detect.
MODE_CYCLE = [None, "browse", "scan"]

# Browser MPRIS position can run slightly AHEAD of audible output (output
# buffering/latency); a positive offset (seconds) is subtracted from the reported
# position to pull the highlight back into sync. Default is 0 (no correction) for
# both modes; tune per song with , / . and save with S, or set the env defaults
# KARAOKE_SYNC_OFFSET / KARAOKE_SYNC_OFFSET_SPOTIFY.
def _default_sync_offset(mode: str = "scan") -> float:
    if mode == "spotify":
        env, fallback = "KARAOKE_SYNC_OFFSET_SPOTIFY", 0.0
    else:
        env, fallback = "KARAOKE_SYNC_OFFSET", 0.0
    try:
        return float(os.environ.get(env, str(fallback)))
    except ValueError:
        return fallback


SYNC_OFFSET_STEP = 0.1

# Below these the chrome gives way to the lyrics. The way to make lyrics bigger
# in a terminal is a bigger terminal font, which costs cells — and measuring the
# real layout at 80x24 showed the fixed panels taking 72% of the screen, so they
# have to yield rather than squeeze the lyrics into a corner.
NARROW_COLS = 110
SHORT_ROWS = 32

# Mic (radio) mode. songrec needs a few seconds of audio; re-identifying every
# ~30s corrects drift and catches track changes without hammering the service.
MIC_REIDENTIFY_S = 30.0
MIC_LISTEN_TIMEOUT = 20


def _radio_cli_running() -> bool:
    """True if `karaoke --radio` already holds the mic.

    Two recognisers on one input just take turns failing, so the TUI declines
    rather than fighting the CLI for it.
    """
    try:
        out = subprocess.run(["pgrep", "-af", "karaoke"], capture_output=True,
                             text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any((" -r" in line or "--radio" in line) and "tui" not in line
               for line in out.splitlines())


def lyric_display_state(lyrics) -> str:
    """What can be shown for a track: "synced", "unsynced" or "none".

    Three states, not two. Treating it as "synced or nothing" is what made the
    TUI announce "No synced lyrics — added to the backfill queue" for a track
    whose words were sitting in the database unsynced, and whose words the
    player was showing on screen at the same moment.
    """
    if lyrics is None:
        return "none"
    if lyrics.has_synced:
        return "synced"
    if (lyrics.plain or "").strip():
        return "unsynced"
    return "none"


def genre_line(row) -> str:
    """The stored genre, with the honesty the label needs.

    A bare label overstates it. "pop" won 39 of 120 tracks and "punk rock" 29,
    far beyond what the library holds, because both sit close to a great deal
    of music -- so a thin win on one of those is not the same claim as a clear
    win on something specific. The runner-up is shown when the margin is
    narrow, because it is often the better answer: "heavy metal, ~punk rock"
    describes sludge better than either alone.
    """
    if row is None:
        return ""
    from .genre import BROAD_LABELS, MIN_MARGIN

    label = row["genre"]
    score = row["score"] or 0.0
    runner = row["runner_up"] or ""
    runner_score = row["runner_up_score"] or 0.0
    close = runner and (score - runner_score) < MIN_MARGIN
    if close:
        return f"{label} ~{runner}"
    if label in BROAD_LABELS:
        # Marked rather than hidden: it is a real answer, just a weak one.
        return f"{label}?"
    return label


def track_info(*, source: str = "", duration: float | None = None,
               offset: float = 0.0, pending: "list[str] | None" = None,
               lyric_lines: int = 0, error: str = "", genre: str = "") -> str:
    """Compact per-track read-out for under the cover art.

    A label column wide enough for the longest label, ASCII throughout, so the
    values line up whatever the terminal does with symbol glyphs.

    An error replaces the block rather than joining it: when something is
    wrong, that is the thing worth reading. Absent values are simply left out,
    so a track with nothing known renders nothing at all.
    """
    if error:
        return f"! {error}"

    rows: list[tuple[str, str]] = []
    if genre:
        rows.append(("genre", genre))
    if source:
        # Say plainly when the words are Whisper's guess rather than a real
        # lyric. Nothing else on screen distinguishes them, and the failure is
        # not always obvious from the text -- though sometimes it is:
        # Neubauten's "Installation N 1" is stored as German and Polish
        # fragments fused without spaces.
        from .librarysearch import is_transcribed

        rows.append(("source",
                     f"{source}  (guessed)" if is_transcribed(source) else source))
    if lyric_lines:
        rows.append(("lines", str(lyric_lines)))
    if duration:
        rows.append(("length", f"{int(duration) // 60}:{int(duration) % 60:02d}"))
    rows.append(("offset", f"{offset:+.1f}s"))
    if pending is not None:
        rows.append(("postproc", ", ".join(pending) if pending else "done"))

    if not rows:
        return ""
    width = max(len(label) for label, _ in rows) + 2
    return "\n".join(f"{label:<{width}s}{value}" for label, value in rows)


def short_source(name: str, width: int = 26) -> str:
    """Shorten a PipeWire source name while keeping both informative ends.

    These run long — "bluez_output.1C_5E_82_70_03_6F.1.monitor" — and the two
    ends are what identify it: the device kind at the front and ".monitor" (as
    opposed to a microphone) at the back. A plain truncation would drop exactly
    the part that says whether the right thing is being recorded.
    """
    name = (name or "").strip()
    if len(name) <= width:
        return name
    tail = ".monitor" if name.endswith(".monitor") else name[-8:]
    head = name[:max(1, width - len(tail) - 1)]
    return f"{head}…{tail}"


def record_panel(*, recording_id: int, elapsed_s: float = 0.0,
                 marks_ok: int = 0, marks_total: int = 0,
                 size_bytes: int = 0, source: str = "",
                 blink: bool = True) -> str:
    """The recording indicator for the sidebar.

    ASCII labels in a fixed column, like track_info, so nothing shifts as the
    numbers change. The dot alternates on each refresh: a still display gives
    no sign that capture is actually alive, and this is the one panel where
    that distinction matters.
    """
    dot = "●" if blink else "○"
    mins, secs = divmod(int(max(0.0, elapsed_s)), 60)
    hours, mins = divmod(mins, 60)
    clock = (f"{hours}:{mins:02d}:{secs:02d}" if hours
             else f"{mins:02d}:{secs:02d}")
    rows = [
        ("marks", f"{marks_ok}/{marks_total}"),
        ("size", f"{size_bytes / 1e6:.0f} MB"),
    ]
    if source:
        rows.append(("src", short_source(source)))
    width = max(len(label) for label, _ in rows) + 2
    body = "\n".join(f"{label:<{width}s}{value}" for label, value in rows)
    return f"{dot} REC {recording_id}  {clock}\n{body}"


def binding_rows(bindings, *, key_display=None) -> list[tuple[str, str]]:
    """Return (key, description) pairs for the help screen, from BINDINGS.

    Generated rather than hand-written so the help can never drift from the
    real key map — the previous cheat-sheet was a hard-coded Static that had
    already fallen out of step with BINDINGS.
    """
    from textual.keys import format_key

    rows: list[tuple[str, str]] = []
    for binding in Binding.make_bindings(bindings):
        if not binding.show or not binding.description:
            continue
        if key_display is not None:
            key = key_display(binding)
        else:
            key = binding.key_display or format_key(binding.key)
        rows.append((key, binding.description))
    return rows


def help_table(rows: list[tuple[str, str]]):
    """Render (key, description) pairs as a two-column Rich grid."""
    from rich.table import Table

    table = Table.grid(padding=(0, 2))
    table.add_column(justify="right", style="bold cyan", no_wrap=True)
    table.add_column()
    for key, description in rows:
        table.add_row(key, description)
    return table


HELP_NOTES = (
    "",
    "H opens the library over the lyrics; picking a song closes it again.",
    "↑/↓ move the highlight, enter opens (or whitelists in the staging list).",
)


class HelpScreen(ModalScreen[None]):
    """Keyboard reference, rendered from the app's own BINDINGS."""

    CSS = """
    HelpScreen { align: center middle; }
    #help-dialog {
        width: 64; height: auto; max-height: 90%;
        border: thick $accent; padding: 1 2; background: $surface;
        border-title-align: center;
    }
    """

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("question_mark", "dismiss", "Close"),
        ("q", "dismiss", "Close"),
    ]

    def __init__(self, rows: list[tuple[str, str]]) -> None:
        super().__init__()
        self._rows = rows

    def compose(self) -> ComposeResult:
        with Vertical(id="help-dialog") as dialog:
            dialog.border_title = "Keys"
            dialog.border_subtitle = "esc / ? to close"
            yield Static(help_table(self._rows))
            yield Static("\n".join((*HELP_NOTES, f"logs: {LOG_FILE}")))


def stats_panels(lib, summary, status=None) -> "list[tuple[str, list[tuple[str, str]]]]":
    """(section, rows) pairs for the stats screen.

    Pure: takes already-gathered data and returns labelled strings, so the
    layout is testable without a database, a broker or an event loop.
    """
    from .librarystats import bar

    def pct(n: int, d: int) -> str:
        return f"{100 * n / d:.0f}%" if d else "—"

    library = [
        ("tracks", str(lib.tracks)),
        ("karaoke-ready", f"{lib.synced}  {bar(lib.synced, lib.tracks)} "
                          f"{pct(lib.synced, lib.tracks)}"),
        ("plain only", str(lib.plain_only)),
        ("no lyrics", str(lib.tracks - lib.with_lyrics)),
        ("sources", str(lib.sources)),
        ("staged", str(lib.staged)),
    ]

    pipeline = [
        ("analysed", f"{lib.analysed}  {bar(lib.analysed, lib.tracks)} "
                     f"{pct(lib.analysed, lib.tracks)}"),
        ("backlog", str(lib.unanalysed)),
        ("word timings", str(lib.word_timed)),
    ]
    if status is not None:
        pipeline.append(("workers", f"{status.workers} up"))
        pipeline.append(("queue", str(status.queued)))

    listening = [
        ("plays", str(summary.plays)),
        ("discoveries", str(summary.discoveries)),
        ("cache hits", f"{summary.cache_hits}/"
                       f"{summary.cache_hits + summary.cache_misses}  "
                       f"{pct(summary.cache_hits, summary.cache_hits + summary.cache_misses)}"),
        ("artists", str(summary.distinct_artists)),
    ]

    total_lyrics = sum(n for _, n in lib.lyric_sources) or 1
    # Caption source names run to 35 characters and would push the bars out of
    # line; the distinguishing part is the front.
    sources = [(name[:16], f"{n:>4}  {bar(n, total_lyrics)}")
               for name, n in lib.lyric_sources[:6]]

    total_keys = sum(n for _, n in lib.keys) or 1
    keys = [(name, f"{n:>4}  {bar(n, total_keys)}") for name, n in lib.keys[:6]]

    total_bpm = sum(n for _, n in lib.tempo_bands) or 1
    tempo = [(name, f"{n:>4}  {bar(n, total_bpm)}") for name, n in lib.tempo_bands]

    panels = [
        ("library", library),
        ("pipeline", pipeline),
        ("listening", listening),
        ("lyric sources", sources),
        ("gaps", [(s, str(n)) for s, n in lib.gaps]),
        ("keys", keys),
        ("tempo", tempo),
    ]
    return [(title, rows) for title, rows in panels if rows]


class StatsScreen(ModalScreen[None]):
    """Library, pipeline and listening statistics."""

    CSS = """
    StatsScreen { align: center middle; }
    #stats-dialog {
        width: 92; height: auto; max-height: 90%;
        border: thick $accent; padding: 1 2; background: $surface;
        border-title-align: center; overflow-y: auto;
    }
    """

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("T", "dismiss", "Close"),
        ("q", "dismiss", "Close"),
    ]

    def __init__(self, panels) -> None:
        super().__init__()
        self._panels = panels

    def compose(self) -> ComposeResult:
        from rich.table import Table

        grid = Table.grid(padding=(0, 3))
        grid.add_column()
        grid.add_column()
        cells = []
        for title, rows in self._panels:
            inner = Table.grid(padding=(0, 2))
            inner.add_column(justify="left", style="cyan", no_wrap=True)
            inner.add_column(justify="left")
            inner.add_row(f"[bold]{title}[/bold]", "")
            for label, value in rows:
                inner.add_row(label, value)
            cells.append(inner)
        # Two columns, so seven short panels fit without scrolling.
        for i in range(0, len(cells), 2):
            grid.add_row(cells[i], cells[i + 1] if i + 1 < len(cells) else "")

        with Vertical(id="stats-dialog") as dialog:
            dialog.border_title = "Stats"
            dialog.border_subtitle = "esc to close"
            yield Static(grid)


def _playback_endpoint_problem(url: str, *, timeout: float = 3.0):
    """Why a recorded track cannot be served, in words, or None if it can.

    The control API is a long-lived process, so it serves whatever code it was
    started with. When it predates a route it answers 404 and the browser shows
    "Not Found" -- indistinguishable, from the outside, from the recording
    itself being missing. Naming the difference is the whole point of this.
    """
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            if response.status == 200:
                return None
            return f"Playback endpoint answered {response.status}"
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return ("Control API does not know this route — it is probably "
                    "running older code. Restart: python -m karaoke.ctrl_api")
        return f"Playback endpoint answered {exc.code}"
    except urllib.error.URLError as exc:
        return (f"Control API is not reachable ({exc.reason}). "
                "Start it: python -m karaoke.ctrl_api")
    except Exception as exc:                    # pragma: no cover - defensive
        return f"Could not reach the control API: {exc}"


class RecordingBrowseScreen(ModalScreen[None]):
    """Browse what a recording captured, and play a track back from it.

    Playback goes to the browser window that is already open, because that
    window publishes MPRIS and the rest of the app already reads position from
    there. A recorded track therefore syncs its lyrics through exactly the same
    path a stream does.

    Two levels: sessions, then the tracks inside one. Tracks that cannot be
    played -- pruned audio, or a stretch that turned out to be silence -- are
    listed and marked rather than hidden, because a gap in a session is
    evidence about the session.
    """

    CSS = """
    RecordingBrowseScreen { align: center middle; }
    #rec-dialog {
        width: 100; height: auto; max-height: 90%;
        border: thick $accent; padding: 1 2; background: $surface;
        border-title-align: center;
    }
    #rec-table { height: auto; max-height: 24; }
    #rec-hint { color: $text-muted; height: 1; }
    """

    BINDINGS = [
        ("escape", "back", "Back"),
        ("B", "dismiss", "Close"),
        ("q", "dismiss", "Close"),
    ]

    def __init__(self, recordings: list[dict]) -> None:
        super().__init__()
        self._recordings = recordings
        self._recording_id: Optional[int] = None
        self._rows: list[dict] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="rec-dialog") as dialog:
            dialog.border_title = "Recordings"
            dialog.border_subtitle = "enter to open · esc to go back"
            yield DataTable(id="rec-table", cursor_type="row")
            yield Static("", id="rec-hint")

    def on_mount(self) -> None:
        self._show_sessions()

    # -- the two levels ---------------------------------------------------

    def _table(self) -> DataTable:
        return self.query_one("#rec-table", DataTable)

    def _reset(self, *columns: str) -> DataTable:
        table = self._table()
        table.clear(columns=True)
        table.add_columns(*columns)
        return table

    def _show_sessions(self) -> None:
        self._recording_id = None
        table = self._reset("When", "Status", "Tracks", "Audio")
        for rec in self._recordings:
            when = time.strftime("%d %b %H:%M", time.localtime(rec["started_at"]))
            size = rec.get("audio_bytes") or 0
            table.add_row(
                when, rec.get("status", ""), str(rec.get("identified", 0)),
                f"{size / 1_000_000_000:.1f} GB" if size else "gone",
                key=str(rec["recording_id"]))
        self.query_one("#rec-hint", Static).update(
            f"{len(self._recordings)} session(s)")

    def _show_tracks(self, recording_id: int) -> None:
        from . import recording_audio

        self._recording_id = recording_id
        self._rows = recording_audio.browse_rows(recording_id)
        table = self._reset("At", "Artist", "Title", "Length", "")
        for row in self._rows:
            at = time.strftime("%H:%M:%S", time.localtime(row["start_wall"]))
            # Why a row cannot be played matters: "gone" is a retention
            # decision, "silent" is what the capture actually contains.
            if not row["playable"]:
                note = "audio gone"
            elif row["silent"]:
                note = "silent"
            elif not row["confident"]:
                note = "boundary ?"
            else:
                note = ""
            table.add_row(at, row["artist"][:24], row["title"][:34],
                          f"{row['duration_s'] / 60:.1f}m", note,
                          key=str(row["index"]))
        playable = sum(1 for r in self._rows if r["playable"] and not r["silent"])
        self.query_one("#rec-hint", Static).update(
            f"recording {recording_id}: {len(self._rows)} track(s), "
            f"{playable} playable")

    # -- interaction ------------------------------------------------------

    def action_back(self) -> None:
        """esc backs out one level, then closes."""
        if self._recording_id is None:
            self.dismiss(None)
        else:
            self._show_sessions()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value
        if key is None:
            return
        if self._recording_id is None:
            self._show_tracks(int(key))
        else:
            self._play(int(key))

    def _play(self, index: int) -> None:
        from . import recording_audio
        from .player_open import open_song_url

        row = next((r for r in self._rows if r["index"] == index), None)
        if row is None:
            return
        if not row["playable"]:
            self.app.notify("That track's audio has been pruned",
                            severity="warning")
            return
        if self._recording_id is None:
            return

        url = recording_audio.track_url(self._recording_id, index)

        # Checked before navigating. Sending the browser to a URL that answers
        # 404 replaces what is playing with an error page, and the message it
        # shows ("Not Found") says nothing about the cause -- which the first
        # time was a control API still running the code it was started with,
        # two days before these routes existed.
        problem = _playback_endpoint_problem(url)
        if problem:
            self.app.notify(problem, severity="error", timeout=10)
            return

        try:
            open_song_url(url, "recording")
        except Exception:
            log.debug("could not open recorded track", exc_info=True)
            self.app.notify("Could not reach the playback window",
                            severity="error")
            return
        self.app.notify(f"Playing {row['title']} from recording "
                        f"{self._recording_id}")
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    """A tiny yes/no modal used for the staging whitelist confirmation."""

    CSS = """
    ConfirmScreen { align: center middle; }
    #dialog {
        width: 60; height: auto; border: thick $accent; padding: 1 2;
        background: $surface;
    }
    #buttons { height: auto; margin-top: 1; align-horizontal: center; }
    Button { margin: 0 1; }
    """

    # Without this the dialog can only be dismissed by clicking a button.
    # _prompt_save_offset raises it unprompted on a track change, so being
    # unable to escape it was a real trap.
    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(self, message: str, *, yes_label: str = "Whitelist",
                 no_label: str = "Cancel") -> None:
        super().__init__()
        self._message = message
        self._yes_label = yes_label
        self._no_label = no_label

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._message)
            with Horizontal(id="buttons"):
                yield Button(self._yes_label, variant="success", id="yes")
                yield Button(self._no_label, variant="default", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_cancel(self) -> None:
        # dismiss(False), not dismiss(): both call sites test the result for
        # truthiness, so escape must mean an explicit "no".
        self.dismiss(False)


class KaraokeTui(App):
    """A player-aware Textual shell for the karaoke experience."""

    # Textual's default ("*") focuses the first focusable widget at mount,
    # which with the browse overlay hidden is the Select *inside it* — every
    # keypress would then be swallowed by an invisible dropdown. Focus is
    # granted explicitly when the overlay opens instead.
    AUTO_FOCUS = None

    CSS = """
    Screen { layout: vertical; layers: base overlay; }
    #workspace { height: 1fr; }
    #sidebar { width: 34; padding: 0 1; }
    #main { width: 1fr; padding: 0 1; }
    #visuals { width: 34; border: round magenta; padding: 1; }
    /* Tall enough for the figlet title banner (5 rows + artist + status). */
    #now-playing { height: 11; border: round green; padding: 0 1; margin-bottom: 1; }
    /* overflow-y is hidden, not auto: a scrollbar appearing mid-song steals a
       column and shears the block glyphs, and #lyrics is a non-focusable
       Static so the scrollbar is unreachable by keyboard anyway. */
    #lyrics { height: 1fr; border: round blue; padding: 1; overflow-y: hidden; }
    #statusbar { height: 1; }
    #mode-label { width: 1fr; }
    #worker-load { width: auto; text-align: right; }
    #mood-square {
        height: 8; content-align: center middle; text-style: bold;
        border: heavy white; margin-bottom: 1;
    }
    #search-input { height: 3; margin-bottom: 1; border: round $accent; }
    /* Hidden until there is a list, so the lyrics keep the full pane. */
    #queue { display: none; height: 10; border: round cyan; margin-top: 1; }
    #queue.-on { display: block; }
    #keybpm { height: 6; border: round green; padding: 0 1; margin-bottom: 1; }
    #ascii-visual { height: 1fr; border: round yellow; padding: 0 1; }
    /* Recording indicator. Hidden entirely when idle rather than shown empty,
       so the sidebar layout is unchanged until it matters. */
    #record-panel { display: none; height: auto; border: round red;
                    padding: 0 1; margin-top: 1; color: $error; }
    #record-panel.-on { display: block; }
    #worker-panel { height: auto; border: round cyan; padding: 0 1;
                    margin-top: 1; }
    /* Takes the rest of the column so the reserved space is visibly held. */
    /* 1fr: soaks up the slack, so the panels under it sit at the bottom. */
    #beat-art { height: 1fr; border: round $surface-lighten-2; padding: 0 1; }
    /* auto height, so the art above takes whatever is left */
    #track-info { height: auto; padding: 0 1; margin-top: 1;
                  color: $text-muted; }

    /* Browse overlay. On its own layer so showing it never resizes #workspace.
       The offset centres it by arithmetic (Screen is layout: vertical, so
       `align` is unavailable) — if width/height change, offset must too. */
    #browse-overlay {
        layer: overlay; display: none;
        width: 80%; height: 80%; offset: 10% 10%;
        border: round cyan; border-title-align: center;
        background: $surface; padding: 1 2;
    }
    #browse-overlay.-visible { display: block; }

    /* Responsive chrome. A bigger terminal font means fewer cells, so the
       fixed-size panels crowd out the lyrics exactly when they can least
       afford it: at 80x24 the visuals column alone is 42% of the width and
       lyrics got 28% of the screen. These classes are applied on resize. */
    Screen.-narrow #visuals { display: none; }
    Screen.-narrow #sidebar { display: none; }
    Screen.-short #now-playing { height: 3; padding: 0 1; margin-bottom: 0; }
    Screen.-short Header { display: none; }

    /* Focus mode: nothing but the lyrics, at any size. */
    Screen.-focus #visuals { display: none; }
    Screen.-focus #sidebar { display: none; }
    Screen.-focus #now-playing { display: none; }
    Screen.-focus #statusbar { display: none; }
    Screen.-focus Header { display: none; }
    #browse-head { height: 3; }
    #browse-head > Static { width: 8; content-align: left middle; }
    #filter-select { width: 34; }
    #library { height: 1fr; }
    #log-label, #log-path { color: $text-muted; height: 1; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("H", "toggle_browse", "Browse"),
        ("A", "approve_postprocess", "Post-process"),
        ("k", "sample_key", "Sample key/BPM"),
        ("slash", "focus_search", "Search"),
        ("greater_than_sign", "queue_next", "Queue next"),
        ("o", "toggle_play_once", "Play-once"),
        ("O", "toggle_record", "Record"),
        ("R", "toggle_mic", "Mic/radio"),
        ("F", "toggle_focus", "Focus"),
        ("B", "browse_recordings", "Recordings"),
        ("T", "stats", "Stats"),
        ("question_mark", "help", "Keys"),
        # NOT priority: a priority app binding shadows every modal's own
        # escape, which stopped Stats and Help from closing. The
        # ordinary binding already fires from inside the search Input --
        # Input does not bind escape -- so the action, not the binding,
        # was what needed fixing.
        Binding("escape", "hide_browse", "Leave search / close browse",
                show=False),
        ("r", "refresh", "Refresh"),
        ("s", "resync", "Resync"),
        ("comma", "sync_earlier", "Lyrics -0.1s"),
        ("full_stop", "sync_later", "Lyrics +0.1s"),
        ("S", "save_offset", "Save offset"),
        ("m", "cycle_mode", "Mode"),
        ("enter", "select", "Open/Whitelist"),
        ("space", "play_pause", "Play/Pause"),
        ("n", "next_track", "Next"),
        ("p", "previous_track", "Prev"),
        ("left_square_bracket", "seek_back", "Seek -5s"),
        ("right_square_bracket", "seek_fwd", "Seek +5s"),
        ("l", "cycle_log", "Log level"),
    ]

    def __init__(self, *, log_level: str = "err") -> None:
        super().__init__()
        self._song_data: list[SongRow] = []
        self._filter = "working"
        self._mode_override: str | None = None
        self._det = detect.Detection(mode="browse")
        self._timeline = LyricTimeline([])
        self._sync_key: tuple[str, str] | None = None
        self._sync_mood = "neutral"
        self._gap_logged: set[tuple[str, str]] = set()
        self._elapsed = 0.0
        self._sync_offset = _default_sync_offset()
        self._current_track_id: int | None = None
        self._offset_dirty = False
        self._log_level = log_level
        self._autoloaded: set[str] = set()
        self._postprocess_enqueued: set[tuple[str, str]] = set()
        self._cpu_sample: tuple | None = None
        self._current_song: tuple[str, str, str] | None = None
        self._lyrics_fetched: set[tuple[str, str]] = set()
        self._mic_ref = None                       # last songrec identification
        self._offset_mode = ""     # clock the current offset was tuned against
        self._clock_from_mic = False   # mic named the song, a player times it
        # Set once Spotify is unusable (bad auth or an exhausted quota) so a
        # single failure cannot become a lookup storm across a listening session.
        self._spotify_off = False
        self._sampling = False     # a capture is running (real time)
        self._recording_id: int | None = None
        self._record_tick = 0
        self._record_marks: tuple[int, int] | None = None
        self._track_duration: float | None = None  # wraps the radio playhead
        self._mic_stop: threading.Event | None = None
        self._last_error = ""      # surfaced in the track-info read-out
        self._mood_art = None      # rendered picture for the current mood
        self._mood_source = ""     # 'cover' or 'generated'
        self._mood_shown = ""      # mood the picture was rendered for
        self._queue: list = []     # search results, in play order
        self._queue_at = -1        # index currently playing
        self._play_once = True     # queue decides the next track
        self._last_finished_url = ""
        self._idle_since = 0.0     # when the player last held nothing

    # -- layout -----------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="workspace"):
            # Left column balances the right one so the lyrics sit centred
            # rather than pushed against the screen edge.
            with Vertical(id="sidebar"):
                # Art first (it takes the slack), then per-track facts, then
                # the global worker read-out pinned at the bottom.
                yield Static("", id="beat-art")
                # Search sits under the art, where the eye already is when
                # looking for "what else is like this".
                # The placeholder carries the way out. There is nothing else
                # focusable on this screen, so a reader who does not know
                # about escape has no affordance to discover.
                yield Input(placeholder="search  (/ · esc to leave)",
                            id="search-input")
                yield Static("", id="track-info")
                yield Static("workers  —", id="worker-panel")
                # Empty and hidden until recording, so it costs no space.
                yield Static("", id="record-panel")
            with Vertical(id="main"):
                yield Static("Detecting player…", id="now-playing")
                yield Static("Lyrics will render here.", id="lyrics")
                # The result list lives under the lyrics rather than in the
                # overlay: it is what plays next, so it belongs where the
                # playing track is, not behind a panel you have to open.
                yield DataTable(id="queue", cursor_type="row")
                with Horizontal(id="statusbar"):
                    yield Static("Mode: auto", id="mode-label")
                    yield Static("worker-load: —", id="worker-load")
            with Vertical(id="visuals"):
                yield Static(MOOD_GLYPHS["neutral"], id="mood-square")
                yield Static("key: —\nbpm: —", id="keybpm")
                yield Static("sentiment / rhythm", id="ascii-visual")
        # Floats on its own layer above #workspace, so revealing it costs the
        # lyrics no space and does not reflow them.
        with Container(id="browse-overlay") as overlay:
            overlay.border_title = "Library"
            overlay.border_subtitle = "H close · ? keys"
            with Horizontal(id="browse-head"):
                yield Static("Filter")
                yield Select(FILTER_OPTIONS, value="working", id="filter-select",
                             allow_blank=False)
            yield DataTable(id="library", cursor_type="row")
            yield Static(f"log: {self._log_level}", id="log-label")
            yield Static(f"logs: {LOG_FILE}", id="log-path")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#library", DataTable)
        table.add_columns("Artist", "Title", "Src", "♪")
        self.load_songs()
        self._show_selected_song()
        self.set_interval(1.5, self._poll_detection)
        self.set_interval(0.2, self._tick_lyrics)
        self.set_interval(3.0, self._refresh_worker_load)
        self.set_interval(1.0, self._refresh_record_status)
        # 2s: fast enough to catch the end before the site starts
        # its own next track, slow enough not to spam CDP.
        self.set_interval(2.0, self._watch_queue)
        self.apply_size_classes(self.size.width, self.size.height)
        # A row still marked 'recording' with nothing running is a crash, not a
        # live capture. Close it out so the listing does not lie and its audio
        # becomes analysable.
        try:
            recorder.reconcile_stale()
            self._warn_unanalysed_recordings()
            # Audio is kept after analysis now, so something has to bound it.
            from . import recording_worker
            for note in recording_worker.prune_recordings():
                log.info("recording retention:%s", note)
        except Exception:
            log.debug("reconciling stale recordings failed", exc_info=True)
        self._poll_detection()
        self._refresh_worker_load()

    # -- library ----------------------------------------------------------
    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "filter-select":
            self._filter = str(event.value)
            self.load_songs()
            self._show_selected_song()

    def load_songs(self) -> None:
        table = self.query_one("#library", DataTable)
        table.clear()
        self._song_data.clear()
        with localcache.connect() as conn:
            if self._filter == "staging":
                self._load_staging(conn)
            elif self._filter == "spotify":
                self._load_spotify(conn)
            else:
                self._load_tracks(conn, only_working=self._filter == "working")
        for song in self._song_data:
            table.add_row(
                str(song.get("artist") or ""),
                str(song.get("title") or ""),
                str(song.get("kind") or "—"),
                "♪" if song.get("synced_lyrics") else (
                    "·" if song.get("plain_lyrics") else " "),
            )

    def _load_spotify(self, conn) -> None:
        """Tracks that have a Spotify source, joined to that source.

        _load_tracks deliberately ranks spotify *below* every browser-openable
        kind so Enter opens the browser, which leaves Spotify-only tracks
        unreachable from the library. This lists them against their Spotify
        source instead, so the row's url/kind are the Spotify ones and
        _open_selected needs no special case — open_song_url already rewrites
        them to the web player and navigates the existing Chrome window.
        """
        cur = conn.cursor()
        cur.execute(
            """
            SELECT t.track_id, t.artist, t.title,
                   s.url AS url, s.kind AS kind,
                   COALESCE(l.source, '') AS lyric_source,
                   COALESCE(l.synced_lyrics, '') AS synced_lyrics,
                   COALESCE(l.plain_lyrics, '') AS plain_lyrics
            FROM tracks t
            JOIN sources s
              ON s.track_id = t.track_id AND s.kind = 'spotify'
            LEFT JOIN lyrics l
              ON t.track_id = l.track_id AND l.kind = 'approved'
            GROUP BY t.track_id
            ORDER BY t.artist, t.title
            """
        )
        for row in cur.fetchall():
            self._song_data.append({
                "track_id": row["track_id"],
                "artist": row["artist"],
                "title": row["title"],
                "url": row["url"],
                "kind": row["kind"],
                "lyric_source": row["lyric_source"],
                "synced_lyrics": row["synced_lyrics"],
                "plain_lyrics": row["plain_lyrics"],
            })

    def _load_tracks(self, conn, *, only_working: bool) -> None:
        cur = conn.cursor()
        # Prefer a browser-openable source (youtube/http) over spotify so Enter
        # opens in the browser. Deterministic per track (see browse.py).
        cur.execute(
            """
            SELECT t.track_id, t.artist, t.title,
                   COALESCE(s.url, '') AS url,
                   COALESCE(s.kind, '') AS kind,
                   COALESCE(l.source, '') AS lyric_source,
                   COALESCE(l.synced_lyrics, '') AS synced_lyrics,
                   COALESCE(l.plain_lyrics, '') AS plain_lyrics
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
            LEFT JOIN lyrics l
              ON t.track_id = l.track_id AND l.kind = 'approved'
            GROUP BY t.track_id
            ORDER BY t.artist, t.title
            """
        )
        for row in cur.fetchall():
            has_lyrics = bool(row["synced_lyrics"] or row["plain_lyrics"])
            if only_working and not has_lyrics:
                continue
            self._song_data.append({
                "track_id": row["track_id"],
                "artist": row["artist"],
                "title": row["title"],
                "url": row["url"],
                "kind": row["kind"],
                "lyric_source": row["lyric_source"],
                "synced_lyrics": row["synced_lyrics"],
                "plain_lyrics": row["plain_lyrics"],
            })

    def _load_staging(self, conn) -> None:
        for item in staging.list_staged(status="all", limit=200, conn=conn):
            self._song_data.append({
                "staged_id": item.id,
                "status": item.status,
                "artist": item.artist,
                "title": item.title,
                "url": item.source_url,
                "kind": f"{item.source_kind}/{item.status}",
                "lyric_source": item.source_kind,
                "synced_lyrics": item.synced_lyrics,
                "plain_lyrics": item.plain_lyrics,
            })

    # -- selection / previews --------------------------------------------
    def on_data_table_row_highlighted(self, _e: DataTable.RowHighlighted) -> None:
        self._show_selected_song()

    def on_data_table_row_selected(self, _e: DataTable.RowSelected) -> None:
        self.action_select()

    # -- browse overlay ---------------------------------------------------
    _BROWSE_OPEN = "-visible"

    def _browse_open(self) -> bool:
        return self.query_one("#browse-overlay").has_class(self._BROWSE_OPEN)

    def _show_browse(self) -> None:
        # add_class BEFORE focus: a hidden widget silently refuses focus, so
        # reversing these two lines leaves the table unfocused and the arrow
        # keys dead.
        self.query_one("#browse-overlay").add_class(self._BROWSE_OPEN)
        self.query_one("#library", DataTable).focus()

    def _hide_browse(self) -> None:
        self.query_one("#browse-overlay").remove_class(self._BROWSE_OPEN)
        # Back to the screen. There is no sensible focusable target in #main,
        # and inventing one purely to hold focus would be worse.
        self.set_focus(None)

    def action_toggle_browse(self) -> None:
        self._hide_browse() if self._browse_open() else self._show_browse()

    def search_has_focus(self) -> bool:
        """Whether typing currently goes into the search box.

        ``self.focused`` needs a mounted screen stack, which an app object
        constructed for a unit test does not have. Answering False there is
        right as well as convenient: nothing is focused when nothing is
        running.
        """
        try:
            focused = self.focused
        except Exception:
            return False
        return focused is not None and getattr(focused, "id", "") == "search-input"

    def action_hide_browse(self) -> None:
        """escape: leave whatever has taken over the keyboard.

        The search box first. Nothing else on this screen is focusable, so a
        box with focus had no Tab target and no way out -- every key went into
        it, including the ones bound to actions. Escape is the way out of a
        text field everywhere else, so it is the way out here.
        """
        if self.search_has_focus():
            self.set_focus(None)
            return
        if self._browse_open():
            self._hide_browse()

    def title_banner(self, artist: str, title: str, width: int,
                     height: int = 99) -> str:
        """The song title in figlet block type, or plain text if it will not fit.

        This is where block type earns its keep: a header is one short string
        with space around it, unlike a lyric line that has to stay legible and
        in rhythm with its neighbours.

        Falls back to plain text whenever the banner would not fit — a narrow
        panel, a long title, or a compacted header on a short terminal.
        """
        from . import bigtext

        plain = f"♪ {artist} - {title}"
        if width < bigtext.MIN_WIDTH or height < 6:
            return plain
        rendered = bigtext.render(title, width, max_rows=1)
        if rendered is None or len(rendered[0].rows) + 1 > height:
            return plain
        return "\n".join((*rendered[0].rows, f"  {artist}"))

    # -- responsive chrome ------------------------------------------------
    def apply_size_classes(self, width: int, height: int) -> None:
        """Shrink the surrounding panels when there are few cells to spend.

        The way to get bigger lyrics in a terminal is a bigger terminal font,
        which costs cells — so the fixed-size chrome has to give ground rather
        than squeeze the lyrics into a corner.
        """
        self.screen.set_class(width < NARROW_COLS, "-narrow")
        self.screen.set_class(height < SHORT_ROWS, "-short")

    def on_resize(self, event) -> None:
        self.apply_size_classes(event.size.width, event.size.height)

    def action_toggle_focus(self) -> None:
        """`F`: lyrics only, hiding every other panel."""
        on = not self.screen.has_class("-focus")
        self.screen.set_class(on, "-focus")
        self.notify("Focus mode" if on else "Focus mode off")

    # -- mic / radio mode -------------------------------------------------
    def mic_elapsed(self, now: float | None = None) -> float | None:
        """Playhead position from the last mic identification, or None.

        songrec reports where in the track it heard us, plus the monotonic
        instant it finished listening. Everything since is dead reckoning off
        that anchor — there is no MPRIS position to read in radio mode.

        DEFAULT_LEAD_S is added for the same reason `karaoke -r` adds it:
        songrec listens for ~10s before returning, so the offset it reports is
        already stale by roughly that much and the highlight would sit a couple
        of lines behind the audio. Both modes use the same figure so they stay
        in step.
        """
        ref = self._mic_ref
        if ref is None or ref.offset is None or ref.offset_mono is None:
            return None
        now = time.monotonic() if now is None else now
        pos = max(0.0, ref.offset + (now - ref.offset_mono) + DEFAULT_LEAD_S)

        # Bound the reckoning to the track. Nothing here observes the audio, so
        # a song left on repeat keeps counting upward past its own end: the
        # lyrics run out, the footer counts down to a track change that never
        # comes, and the next re-identification is the only thing that rescues
        # it. Wrapping is right for repeat and self-correcting otherwise, since
        # a genuine track change re-anchors within MIC_REIDENTIFY_S either way.
        #
        # Guarded on a sane duration: a bad or missing value must not fold a
        # correct playhead back to zero.
        dur = self._track_duration
        if dur and dur > 30.0 and pos >= dur:
            pos %= dur
        return pos

    def _mic_running(self) -> bool:
        """Whether the mic worker is currently listening."""
        return self._mic_stop is not None and not self._mic_stop.is_set()

    def _mic_loop(self) -> None:
        """Identify room audio on a loop until the mic is switched off.

        Each result either re-anchors the current song (correcting drift) or
        swaps in a new one. A failed identification is ignored rather than
        clearing the song: songrec returns nothing over speech and quiet
        passages, and dropping the lyrics every time would be worse than
        holding the last known track.
        """
        from .identify import identify_live

        while not self._mic_stop.is_set():
            try:
                ref = identify_live(mic=True, timeout=MIC_LISTEN_TIMEOUT)
            except Exception as exc:
                log.debug("mic identify failed: %s", exc)
                ref = None
            if ref is not None and ref.title:
                changed = (self._mic_ref is None
                           or (ref.artist, ref.title)
                           != (self._mic_ref.artist, self._mic_ref.title))
                self._mic_ref = ref
                if changed:
                    # Force _poll_detection to re-resolve instead of
                    # short-circuiting on an unchanged key.
                    self._sync_key = None
                    log.info("mic: %s - %s", ref.artist, ref.title)
            self._mic_stop.wait(MIC_REIDENTIFY_S)

    def action_toggle_mic(self) -> None:
        """`R`: follow room audio via the microphone (songrec)."""
        import shutil

        if self._mic_stop is not None and not self._mic_stop.is_set():
            self._mic_stop.set()
            self._mic_ref = None
            self._sync_key = None
            self.notify("Mic off")
            return

        if not shutil.which("songrec"):
            self.notify("songrec is not installed", severity="error")
            return
        if _radio_cli_running():
            # Two recognisers on one mic just take turns failing.
            self.notify("karaoke -r is already using the mic", severity="warning")
            return

        self._mic_stop = threading.Event()
        self._mic_ref = None
        self.run_worker(self._mic_loop, exclusive=False, thread=True)
        self.notify("Mic on — listening…")

    # -- post-processing --------------------------------------------------
    def approve_postprocess(self, artist: str, title: str,
                            url: str = "") -> tuple[bool, str]:
        """Queue the given track for post-processing. Returns (ok, message).

        Pure enough to test: does the DB lookup and the publish, returns what
        happened, and leaves the notifying to the caller.

        Requires lyrics to exist. Post-processing derives word timings from the
        approved lyrics and key/BPM from the audio; with no lyrics there is
        nothing to upgrade, and the track is already queued as a lyric gap for
        the backfill to pick up instead.
        """
        from .postprocess_queue import needs_postprocessing, publish_postprocess_task

        if not (artist or title):
            return False, "Nothing playing to approve"
        with localcache.connect() as conn:
            track_id = localcache.find_track_id(artist, title, conn)
            if track_id is None:
                return False, f"{artist} - {title} is not in the library yet"
            cached = localcache.get_lyrics_by_track_id(track_id, conn)
            if cached is None or not (cached.synced_raw or cached.plain):
                return False, f"No lyrics for {title} yet — queued as a gap"
            pending = needs_postprocessing(track_id, conn)
        if not pending:
            return False, f"{title} is already fully processed"
        if not publish_postprocess_task(artist, title, url):
            return False, "Broker unreachable — nothing queued"
        return True, f"Queued {title}: {', '.join(pending)}"

    # -- search and the play queue ----------------------------------------

    def action_focus_search(self) -> None:
        """`/`: jump to the search box."""
        self.query_one("#search-input", Input).focus()

    def on_input_submitted(self, event) -> None:
        """Enter in the search box: build a list and start playing it."""
        if event.input.id != "search-input":
            return
        # Focus has to leave the box, or every subsequent keypress is typed
        # into it instead of reaching the bindings -- and it has to leave on
        # the empty query too. Returning early there was a way to stay stuck:
        # nothing else is focusable, so Tab had nowhere to go and Enter on an
        # empty box did not release either.
        self.set_focus(None)
        query = (event.value or "").strip()
        if not query:
            self._set_queue([])
            return
        self.run_worker(lambda q=query: self._background_search(q),
                        exclusive=False, thread=True)

    def _background_search(self, query: str) -> None:
        """Search off the UI thread; scoring reads every track's lyrics."""
        from . import librarysearch

        try:
            with localcache.connect() as conn:
                hits = librarysearch.search(query, conn)
                rows = [{"track_id": h.track_id, "artist": h.artist,
                         "title": h.title, "score": h.score,
                         "fields": h.fields,
                         "url": librarysearch.playable_url(h.track_id, conn) or ""}
                        for h in hits]
        except Exception as exc:
            log.debug("search failed", exc_info=True)
            self.call_from_thread(self.notify, f"Search failed: {exc}",
                                  severity="error")
            return
        self.call_from_thread(self._set_queue, rows, query)

    def _set_queue(self, rows: list, query: str = "") -> None:
        """Show the result list and start it playing."""
        self._queue = rows
        self._queue_at = -1
        table = self.query_one("#queue", DataTable)
        if not rows:
            table.set_class(False, "-on")
            table.clear()
            if query:
                self.notify(f"No matches for {query!r}", severity="warning")
            return
        table.set_class(True, "-on")
        self._render_queue()
        self.notify(f"{len(rows)} match(es) for {query!r}" if query
                    else f"{len(rows)} queued")
        self.play_queue_index(0)

    def _render_queue(self) -> None:
        """Draw the list, marking what is playing."""
        table = self.query_one("#queue", DataTable)
        table.clear()
        if not table.columns:
            table.add_columns(" ", "Artist", "Title", "")
        for i, row in enumerate(self._queue):
            playing = i == self._queue_at
            # A row with no source cannot be played; say so rather than
            # skipping silently when it is reached.
            note = "" if row.get("url") else "no source"
            table.add_row(">" if playing else str(i + 1),
                          row["artist"][:18], row["title"][:28], note)
        table.border_title = (f"queue  {self._queue_at + 1}/{len(self._queue)}"
                              f"{'  (play-once)' if self._play_once else ''}")

    def on_data_table_row_selected(self, event) -> None:
        """Click or Enter on a queue row plays it."""
        if event.data_table.id != "queue":
            return
        self.play_queue_index(event.cursor_row)

    def action_toggle_play_once(self) -> None:
        """`o`: play the queue through once, or let the player carry on.

        With it on, the TUI takes playback back at the end of every track and
        moves to the next entry. That is what stops YouTube Music's own radio
        from taking over -- opening a watch URL makes it attach an RD… station,
        so the "next" song is its suggestion rather than the list you built.
        """
        self._play_once = not self._play_once
        self._render_queue()
        self.notify("Play-once: queue decides what plays next" if self._play_once
                    else "Play-once off: the player picks its own next track")

    def _watch_queue(self) -> None:
        """Advance the queue when the browser finishes a track.

        MPRIS cannot answer this: when the site moves to its own suggestion the
        metadata simply changes, which looks the same as the user picking
        something. The page's video element reports that it ended.
        """
        if not (self._play_once and self._queue and self._queue_at >= 0):
            return
        state = browser_playback()
        if not track_finished(state):
            # The window can also come to rest holding nothing at all: the
            # site navigated off the watch URL, or the load never took. That
            # never reads as finished -- no duration, and `ended` will never
            # be set -- so without this the queue waits for an end that cannot
            # arrive. A fresh watch URL looks the same for a moment, so let it
            # hold before treating it as the end of the track.
            if not track_idle(state):
                self._idle_since = 0.0
                return
            now = time.monotonic()
            if not self._idle_since:
                self._idle_since = now
                return
            if now - self._idle_since < IDLE_STALL_S:
                return
            log.info("player idle for %.0fs; advancing the queue", IDLE_STALL_S)
        self._idle_since = 0.0
        # Only act once per track: after advancing, the new track is short of
        # its end again, but a stalled read could otherwise fire repeatedly.
        url = (state or {}).get("url", "")
        if url and url == self._last_finished_url:
            return
        self._last_finished_url = url
        self.action_queue_next()

    def play_queue_index(self, index: int) -> bool:
        """Open the queue entry at ``index``. Returns whether it played."""
        if not (0 <= index < len(self._queue)):
            return False
        row = self._queue[index]
        url = row.get("url") or ""
        if not url:
            self.notify(f"No source for {row['artist']} - {row['title']}",
                        severity="warning")
            self._queue_at = index
            self._render_queue()
            return False
        try:
            open_song_url(url, "youtube" if "youtu" in url else "")
        except Exception as exc:
            log.exception("queue play failed")
            self.notify(f"Play failed: {exc}", severity="error")
            return False
        self._queue_at = index
        self._render_queue()
        self.notify(f"Playing {row['artist']} - {row['title']}")
        return True

    def action_queue_next(self) -> None:
        """Advance to the next playable entry in the queue."""
        if not self._queue:
            return
        index = self._queue_at + 1
        while index < len(self._queue):
            if self.play_queue_index(index):
                return
            index += 1
        self.notify("End of queue")

    def action_sample_key(self) -> None:
        """`k`: detect key/BPM by recording what is playing.

        For Spotify there is no file to analyse, so those tracks can never be
        post-processed the normal way. Recording the sink monitor gives a clean
        digital copy of the audio and closes that gap.

        Capture is real time and takes the better part of a minute, so it runs
        in a worker and is guarded against being started twice.
        """
        det = self._det
        if not (det.artist and det.title):
            self.notify("Nothing playing to sample", severity="warning")
            return
        if self._sampling:
            self.notify("Already sampling", severity="warning")
            return
        self._sampling = True
        seconds = sample_audio.DEFAULT_SECONDS
        self.notify(f"Sampling {seconds:.0f}s of {det.title}…")
        self.run_worker(
            lambda a=det.artist, t=det.title: self._background_sample(a, t),
            exclusive=False, thread=True,
        )

    def _background_sample(self, artist: str, title: str) -> None:
        """Record and analyse the playing audio, in a worker thread."""
        try:
            result = sample_audio.sample_and_analyse(artist, title)
        except sample_audio.CaptureError as exc:
            log.warning("sample failed: %s", exc)
            self.call_from_thread(self.notify, f"Sample failed: {exc}",
                                  severity="error")
            return
        except sample_audio.AnalysisUnavailable as exc:
            # The recording worked; this interpreter just cannot analyse it.
            # Say so plainly rather than reporting an unknown key.
            log.warning("sample not analysed: %s", exc)
            self.call_from_thread(self.notify, str(exc), severity="warning")
            return
        except Exception as exc:
            log.exception("sample analysis failed")
            self.call_from_thread(self.notify, f"Analysis failed: {exc}",
                                  severity="error")
            return
        finally:
            self._sampling = False

        key = result.key.name if result.key else "unknown"
        bpm = f"{result.bpm:.0f}" if result.bpm else "?"
        self.call_from_thread(self.notify, f"{artist} - {title}: {key}, {bpm} BPM")
        # The analysis panel reads from the DB, so refresh it now the row exists.
        self.call_from_thread(self._refresh_track_info, None)

    def on_unmount(self) -> None:
        """Stop any capture when the app exits.

        Without this the ffmpeg child outlives the TUI: it keeps writing audio
        with nothing supervising it, its `recordings` row stays 'recording'
        forever, and the next TUI -- seeing no session of its own -- starts a
        second recorder on the same source. Observed in the wild: a capture
        orphaned for 1h51m alongside a live one.
        """
        try:
            recorder.stop_all()
        except Exception:
            log.debug("stopping recordings on exit failed", exc_info=True)

    def action_toggle_record(self) -> None:
        """`O`: record the output continuously, marking what plays on it.

        Unlike `k`, which samples one track in real time, this runs unattended:
        songrec is asked what is playing every so often and each answer is
        stored as a marker, so the session can be cut back into tracks and
        analysed afterwards.
        """
        if self._recording_id is not None:
            recorded, total = recorder.mark_count(self._recording_id)
            stopped_id = self._recording_id
            recorder.stop(stopped_id)
            self.notify(f"Recording {stopped_id} stopped "
                        f"({recorded}/{total} tracks identified)")
            self._recording_id = None
            self._record_marks = None
            self._refresh_record_status()
            # Recording and analysing were separate steps with nothing joining
            # them, so finished sessions simply accumulated -- four of them,
            # nearly a gigabyte, before anyone noticed. Stopping now starts the
            # analysis.
            if recorded:
                self._analyse_recording(stopped_id)
            return
        try:
            session = recorder.start()
        except recorder.RecorderError as exc:
            self.notify(f"Cannot record: {exc}", severity="error")
            return
        except Exception as exc:
            log.exception("failed to start recording")
            self.notify(f"Record failed: {exc}", severity="error")
            return
        self._recording_id = session.recording_id
        self.notify(f"Recording {session.recording_id} to {session.directory.name}")
        self._refresh_record_status()

    def _analyse_recording(self, recording_id: int) -> None:
        """Decompile a finished recording, in a worker thread.

        Minutes of work for an evening's audio, so it cannot run on the UI
        thread; and it is deliberately *not* run on app exit, where starting a
        long job during shutdown would be worse than leaving it. A session
        closed that way is caught by the reminder at mount instead.
        """
        def _work() -> None:
            from . import recording_worker
            try:
                lines = recording_worker.analyse(recording_id)
            except Exception as exc:
                log.exception("recording analysis failed")
                self.call_from_thread(self.notify,
                                      f"Analysis failed: {exc}", severity="error")
                return
            done = sum(1 for line in lines if line.strip().startswith("ok"))
            self.call_from_thread(
                self.notify,
                f"Recording {recording_id}: {done} track(s) analysed")

        self.notify(f"Analysing recording {recording_id}…")
        try:
            self.run_worker(_work, exclusive=False, thread=True)
        except Exception:
            log.debug("analysis dispatch failed", exc_info=True)

    def _warn_unanalysed_recordings(self) -> None:
        """Point out sessions that finished without being analysed.

        Quitting mid-recording, or a crash, closes the row without analysing
        it. Without this they are invisible: the audio sits on disk and the
        tracks never gain their key or BPM.
        """
        try:
            with localcache.connect() as conn:
                rows = conn.execute(
                    "SELECT count(*) n FROM recordings WHERE status = 'complete'"
                ).fetchone()
        except Exception:
            return
        pending = int(rows["n"]) if rows else 0
        if pending:
            self.notify(f"{pending} recording(s) awaiting analysis "
                        f"(karaoke-recording --analyse)", severity="warning")

    def _refresh_record_status(self) -> None:
        """Keep the recording indicator current; also catches a died recorder."""
        panel = self.query_one("#record-panel", Static)
        if self._recording_id is None:
            panel.set_class(False, "-on")
            panel.update("")
            return
        if not recorder.is_running(self._recording_id):
            # The capture died on its own (ffmpeg exited, or a cap was hit).
            self.notify(f"Recording {self._recording_id} ended", severity="warning")
            self._recording_id = None
            panel.set_class(False, "-on")
            panel.update("")
            return

        self._record_tick += 1
        # The clock and the blink want a fast refresh; the mark count is a
        # database round trip and does not, so it is sampled every fifth tick.
        if self._record_tick % 5 == 1 or self._record_marks is None:
            self._record_marks = recorder.mark_count(self._recording_id)
        directory = recorder.session_directory(self._recording_id)
        panel.set_class(True, "-on")
        panel.update(record_panel(
            recording_id=self._recording_id,
            elapsed_s=recorder.elapsed(self._recording_id) or 0.0,
            marks_ok=self._record_marks[0], marks_total=self._record_marks[1],
            size_bytes=recorder.directory_size(directory) if directory else 0,
            source=recorder.session_source(self._recording_id) or "",
            blink=self._record_tick % 2 == 1,
        ))

    def action_approve_postprocess(self) -> None:
        """`A`: post-process whatever is playing, on demand.

        The automatic enqueue in _poll_detection is silent and fires once per
        session, so it cannot be retried if the broker was down at the time.
        This is the deliberate version: it reports what happened and clears the
        session guard so the track can be queued again.
        """
        if not self._current_song:
            self.notify("Nothing playing to approve", severity="warning")
            return
        artist, title, url = self._current_song
        self._postprocess_enqueued.discard((artist.lower(), title.lower()))
        ok, message = self.approve_postprocess(artist, title, url)
        self.notify(message, severity="information" if ok else "warning")

    def action_browse_recordings(self) -> None:
        """`B`: browse recorded sessions and play a track out of one."""
        from . import recording_worker

        try:
            with localcache.connect() as conn:
                rows = conn.execute(
                    "SELECT recording_id, started_at, status, dir,"
                    "       (SELECT COALESCE(sum(m.ok), 0) FROM recording_marks m"
                    "         WHERE m.recording_id = r.recording_id) AS identified"
                    " FROM recordings r"
                    " WHERE status != 'discarded'"
                    " ORDER BY recording_id DESC LIMIT 40").fetchall()
                recordings = [
                    {"recording_id": r["recording_id"],
                     "started_at": r["started_at"],
                     "status": r["status"],
                     "identified": r["identified"],
                     "audio_bytes": recording_worker.audio_bytes(r)}
                    for r in rows]
        except Exception as exc:
            self.notify(f"Recordings unavailable: {exc}", severity="error")
            return
        if not recordings:
            self.notify("No recordings yet")
            return
        self.push_screen(RecordingBrowseScreen(recordings))

    def action_stats(self) -> None:
        """`T`: library, pipeline and listening statistics."""
        from . import librarystats

        try:
            with localcache.connect() as conn:
                lib = librarystats.collect(conn)
            summary = localcache.summarize()
        except Exception as exc:
            self.notify(f"Stats unavailable: {exc}", severity="error")
            return
        status = None
        try:
            from .postprocess_status import get_status
            status = get_status(sample_cpu=False)
        except Exception:
            pass          # broker down is not a reason to hide the rest
        self.push_screen(StatsScreen(stats_panels(lib, summary, status)))

    def action_help(self) -> None:
        # get_key_display is the app's own formatter, so the help screen and
        # the Footer always agree on how a key is written.
        self.push_screen(HelpScreen(
            binding_rows(self.BINDINGS, key_display=self.get_key_display)))

    def action_refresh(self) -> None:
        self.load_songs()
        self._show_selected_song()
        self._poll_detection()
        self.notify("Refreshed")

    def action_resync(self) -> None:
        self._sync_key = None
        self._poll_detection()
        self.notify("Resynced playhead")

    def action_sync_earlier(self) -> None:
        # Show lyrics earlier: increase the offset we subtract from position.
        self._sync_offset = round(self._sync_offset + SYNC_OFFSET_STEP, 2)
        self._offset_dirty = True
        self._nudge_sync()

    def action_sync_later(self) -> None:
        # Show lyrics later: decrease the offset (can go negative to delay).
        self._sync_offset = round(self._sync_offset - SYNC_OFFSET_STEP, 2)
        self._offset_dirty = True
        self._nudge_sync()

    def _nudge_sync(self) -> None:
        # Record which clock this tuning was made against. Radio dead-reckons
        # the playhead and adds DEFAULT_LEAD_S; MPRIS modes do not. Capturing it
        # here rather than at save time is what makes it correct: by the time
        # the save prompt fires on a track change, self._det already describes
        # the *next* track.
        self._offset_mode = self._det.mode
        hint = " · press S to save" if self._current_track_id is not None else ""
        self.notify(f"Lyric sync offset: {self._sync_offset:+.1f}s{hint}")
        if self._det.is_active and self._timeline.lines:
            self._tick_lyrics()

    def action_save_offset(self) -> None:
        if self._current_track_id is None:
            self.notify("No track to save offset for", severity="warning")
            return
        with localcache.connect() as conn:
            localcache.set_sync_offset(self._current_track_id, self._sync_offset,
                                       conn, mode=self._offset_mode or self._det.mode)
        self._offset_dirty = False
        self.notify(f"Saved offset {self._sync_offset:+.1f}s for this track")
        log.info("saved sync offset %.2f for track %s",
                 self._sync_offset, self._current_track_id)

    def _prompt_save_offset(self, track_id: int, offset: float) -> None:
        """Ask whether to persist an unsaved offset for a track that's ending."""
        def _on_confirm(save: bool | None) -> None:
            if save:
                with localcache.connect() as conn:
                    localcache.set_sync_offset(track_id, offset, conn,
                                               mode=self._offset_mode)
                self.notify(f"Saved offset {offset:+.1f}s")
                log.info("saved sync offset %.2f for track %s (on change)",
                         offset, track_id)

        self.push_screen(
            ConfirmScreen(
                f"Save lyric sync offset {offset:+.1f}s for this track?",
                yes_label="Save", no_label="Discard",
            ),
            _on_confirm,
        )

    def action_cycle_log(self) -> None:
        order = ["off", "err", "info", "full"]
        self._log_level = order[(order.index(self._log_level) + 1) % len(order)
                                if self._log_level in order else 1]
        stream_logs(self._log_level)
        self.query_one("#log-label", Static).update(f"log: {self._log_level}")
        log.warning("log level set to %s", self._log_level)
        self.notify(f"log level: {self._log_level}")

    def _refresh_worker_load(self) -> None:
        """Update the post-processing worker-load read-out (in a thread).

        Uses a NON-blocking CPU delta between successive ticks (no sleep) so the
        timer keeps firing indefinitely, and hits the RabbitMQ management API off
        the UI thread. Best-effort — a failure updates the line but never stops
        future refreshes.
        """
        prev = self._cpu_sample

        def _work() -> None:
            line = "worker-load: (unavailable)"
            panel = "workers  (unavailable)"
            try:
                from . import postprocess_status as ps
                st = ps.get_status(prev_cpu_sample=prev)
                # Persist the fresh sample for the next tick's delta.
                self._cpu_sample = st.cpu_sample
                line = ps.worker_load_line(st)
                panel = ps.worker_panel(st)
            except Exception:
                log.debug("worker-load refresh failed", exc_info=True)
            for selector, text in (("#worker-load", line),
                                   ("#worker-panel", panel)):
                try:
                    self.call_from_thread(
                        self.query_one(selector, Static).update, text
                    )
                except Exception:
                    pass

        try:
            self.run_worker(_work, exclusive=False, thread=True)
        except Exception:
            log.debug("worker-load dispatch failed", exc_info=True)

    def _selected_song(self) -> SongRow | None:
        table = self.query_one("#library", DataTable)
        if not self._song_data:
            return None
        if 0 <= table.cursor_row < len(self._song_data):
            return self._song_data[table.cursor_row]
        return None

    def _show_selected_song(self) -> None:
        # When a player is syncing, the lyrics panel is owned by the clock.
        if self._det.is_active and self._timeline.lines:
            return
        song = self._selected_song()
        lyrics = self.query_one("#lyrics", Static)
        lyrics.border_subtitle = None
        if song is None:
            lyrics.update("No songs match this filter yet.")
            self._update_mood("neutral")
            self._update_keybpm(None)
            self.query_one("#ascii-visual", Static).update("sentiment / rhythm")
            return
        preview = lyric_preview(song)
        lyrics.update(preview or "No lyrics cached for this track yet.")
        self._render_visuals(song, preview, self._elapsed)

    # -- opening / whitelist / controls ----------------------------------
    def action_select(self) -> None:
        if self._filter == "staging":
            self._whitelist_selected()
        else:
            self._open_selected()

    def _whitelist_selected(self) -> None:
        song = self._selected_song()
        if song is None or "staged_id" not in song:
            self.notify("No staged item selected", severity="warning")
            return
        if song.get("status") == "approved":
            self.notify("Already whitelisted", severity="information")
            return
        artist = str(song.get("artist") or "")
        title = str(song.get("title") or "")
        staged_id = int(song["staged_id"])  # type: ignore[arg-type]

        def on_confirm(ok: bool | None) -> None:
            if not ok:
                return
            try:
                staging.whitelist_staged(staged_id)
            except Exception as exc:
                log.exception("whitelist failed for #%s", staged_id)
                self.notify(f"Whitelist failed: {exc}", severity="error")
                return
            log.info("whitelisted staged #%s: %s - %s", staged_id, artist, title)
            self.notify(f"Whitelisted {artist} - {title}")
            self.load_songs()

        self.push_screen(
            ConfirmScreen(f"Whitelist (approve) lyrics for\n{artist} - {title}?"),
            on_confirm,
        )

    def _open_selected(self) -> None:
        song = self._selected_song()
        if song is None:
            self.notify("No selected song", severity="warning")
            return
        url = str(song.get("url") or "")
        kind = str(song.get("kind") or "")
        artist = str(song.get("artist") or "")
        title = str(song.get("title") or "")
        if not url:
            query = quote_plus(f"{artist} {title}".strip())
            if not query:
                self.notify("No URL or search terms for this row", severity="warning")
                return
            url = f"https://www.youtube.com/results?search_query={query}"
            kind = "youtube_search"
        try:
            pid = open_song_url(url, kind)
        except Exception as exc:
            log.exception("KaraokeTui failed to open %s", url)
            self.notify(f"Open failed; see {LOG_FILE}", severity="error")
            self.query_one("#now-playing", Static).update(f"Open failed: {exc}")
            return
        # Close the overlay only once the song actually opened. On the failure
        # path above we deliberately stay open: the reason is written into
        # #now-playing, which sits *behind* the overlay, and the user most
        # likely wants to pick a different row.
        self._hide_browse()
        suffix = f" (pid {pid})" if pid is not None else ""
        self.notify(f"Opening {artist} - {title}{suffix}")

    def action_cycle_mode(self) -> None:
        idx = MODE_CYCLE.index(self._mode_override)
        self._mode_override = MODE_CYCLE[(idx + 1) % len(MODE_CYCLE)]
        label = self._mode_override or "auto"
        log.info("mode override -> %s", label)
        self.notify(f"Mode: {label}")
        self._sync_key = None  # force a re-resolve
        self._poll_detection()

    def _control_player(self) -> str:
        return self._det.mpris_name if self._det.is_active else ""

    def _resolve_track_id(self, det, artist, title, conn) -> int | None:
        """Best-effort canonical track id for the current detection.

        Prefers a source-URL / video-ID match (reliable for browser tabs), then
        the resolved or raw artist/title. Returns None on a miss.
        """
        if getattr(det, "url", ""):
            found = localcache.find_track_by_url(det.url, conn)
            if found:
                return found[0]
        for a, t in ((artist, title), (det.artist, det.title)):
            if a and t:
                tid = localcache.find_track_id(a, t, conn)
                if tid is not None:
                    return tid
        return None

    def action_play_pause(self) -> None:
        if self._det.is_active:
            ok = playerctl.play_pause(self._control_player())
            if not ok:
                self.notify("control failed", severity="warning")

    def action_next_track(self) -> None:
        if self._det.is_active:
            playerctl.next_track(self._control_player())

    def action_previous_track(self) -> None:
        if self._det.is_active:
            playerctl.previous_track(self._control_player())

    def action_seek_back(self) -> None:
        if self._det.is_active:
            playerctl.seek(-5, self._control_player())

    def action_seek_fwd(self) -> None:
        if self._det.is_active:
            playerctl.seek(5, self._control_player())

    # -- detection + live sync -------------------------------------------
    def _effective_detection(self) -> detect.Detection:
        """Apply the manual mode override on top of auto-detection.

        A live mic identification decides *which song* is playing: it hears the
        room, and MPRIS may be reporting a stale track from some other player.

        It does not decide the *clock*. When a player is playing the song the
        mic just named — the Spotify app, typically — its MPRIS position is
        exact, while radio dead-reckons from songrec's offset plus
        DEFAULT_LEAD_S and drifts until the next re-anchor. In that case hand
        back the player's own detection so the accurate position is used. The
        mic still chose the song; only the clock changed.
        """
        if self._mic_ref is not None and self._mic_ref.title:
            # Pass the mic's track so that when several players are playing,
            # the one actually making the sound in the room is chosen.
            det = detect.detect_active(self._mic_ref.artist, self._mic_ref.title)
            if det.is_active and detect.same_track(
                    det.artist, det.title,
                    self._mic_ref.artist, self._mic_ref.title):
                self._clock_from_mic = True
                return det
            self._clock_from_mic = False
            return detect.Detection(
                mode="radio", player="songrec",
                artist=self._mic_ref.artist, title=self._mic_ref.title,
            )
        self._clock_from_mic = False
        det = detect.detect_active()
        if self._mode_override is None:
            return det
        if self._mode_override == "browse":
            return detect.Detection(mode="browse")
        # spotify / scan forced: keep detection only if it matches, else browse
        if det.is_active and det.mode == self._mode_override:
            return det
        if det.is_active:
            # a player is active but not the forced kind; still sync it
            return det
        return detect.Detection(mode="browse")

    def _poll_detection(self) -> None:
        det = self._effective_detection()
        self._det = det
        mode_label = self.query_one("#mode-label", Static)
        now = self.query_one("#now-playing", Static)
        override = f" (forced {self._mode_override})" if self._mode_override else " (auto)"
        # The mic identified the song but a player is supplying the position.
        # Say so, or it looks like the mic quietly switched itself off.
        if self._clock_from_mic:
            override = " (mic)"
        if not det.is_active:
            mode_label.update(f"Mode: browse{override}")
            self._timeline = LyricTimeline([])
            self._sync_key = None
            now.update(
                "Nothing playing.\nPick a song and press Enter to open it "
                "in the browser, or start Spotify / a YouTube tab."
            )
            self._show_selected_song()
            return

        key = (det.artist.lower(), det.title.lower())
        mode_label.update(f"Mode: {det.mode}{override} · {det.player or 'player'}")
        if key == self._sync_key:
            return

        # Track changed: if the previous track has unsaved offset edits, ask to
        # save them before we load the new track's offset.
        if self._offset_dirty and self._current_track_id is not None:
            self._prompt_save_offset(self._current_track_id, self._sync_offset)
            self._offset_dirty = False

        self._sync_key = key
        with localcache.connect() as conn:
            artist, title, lyrics = detect.resolve_lyrics(det, conn)
            # Resolve the canonical track id and load any saved per-track offset,
            # falling back to the session default when none has been saved.
            self._current_track_id = self._resolve_track_id(det, artist, title, conn)
            saved = (
                localcache.get_sync_offset(self._current_track_id, conn, det.mode)
                if self._current_track_id is not None else None
            )
            self._sync_offset = saved if saved is not None else _default_sync_offset(det.mode)
            self._offset_dirty = False
            self._offset_mode = det.mode
            # Needed by mic_elapsed to wrap the dead-reckoned playhead on repeat.
            self._track_duration = self._load_duration(self._current_track_id, conn)
            # Radio identifies songs MPRIS never sees, so this is where a
            # Spotify link is worth resolving — once per track, and only while
            # the mic is actually running, so browsing costs no API quota.
            if (self._mic_running() and not self._spotify_off
                    and localcache.spotify_lookup_due(self._current_track_id, conn)):
                self.run_worker(
                    lambda tid=self._current_track_id, a=artist, t=title:
                        self._background_fetch_spotify(tid, a, t),
                    exclusive=False, thread=True,
                )
            if lyrics is None or not lyrics.has_synced:
                # The cache is only what has already been fetched. Radio mode
                # calls get_synced, which hits LRCLIB on a miss and caches the
                # result; the TUI used to read the cache and stop, so a track
                # nobody had fetched yet showed as having no lyrics forever.
                if key not in self._lyrics_fetched:
                    self._lyrics_fetched.add(key)
                    self.run_worker(
                        lambda a=det.artist, t=det.title: self._background_fetch_lyrics(a, t),
                        exclusive=False,
                        thread=True,
                    )
                if key not in self._gap_logged:
                    detect.record_gap(det, conn)
                    self._gap_logged.add(key)
                    log.info("no lyrics; queued gap: %s - %s", det.artist, det.title)
                
                # Auto-autoload captions in background if URL is YouTube/YT Music
                if det.url and (detect.is_youtube_url(det.url) or "music.youtube.com" in det.url.lower()):
                    from .localcache import extract_youtube_id
                    vid = extract_youtube_id(det.url)
                    if vid and vid not in self._autoloaded:
                        self._autoloaded.add(vid)
                        self.run_worker(
                            lambda u=det.url, v=vid: self._background_autoload_captions(u, v),
                            exclusive=False,
                            thread=True,
                        )
        display_artist = artist or det.artist
        display_title = title or det.title
        current_song = {"artist": display_artist, "title": display_title}
        # Remembered so `A` can post-process whatever is playing without
        # re-resolving it.
        self._current_song = (display_artist, display_title, det.url or "")
        self._refresh_cover_art(det.url or "")
        if lyrics is not None and (lyrics.synced_raw or lyrics.plain):
            self._last_error = ""      # resolved fine; drop any stale error
        self._refresh_track_info(lyrics)
        keybpm_line = self._format_keybpm_line(current_song)
        # Enqueue background post-processing (key/BPM analysis, word-timing upgrade)
        # if this track is missing derived assets. Best-effort; no-op if the
        # RabbitMQ broker is unreachable.
        pp_key = (display_artist.lower(), display_title.lower())
        if pp_key not in self._postprocess_enqueued:
            self._postprocess_enqueued.add(pp_key)
            try:
                from .postprocess_queue import enqueue_if_needed
                self.run_worker(
                    lambda a=display_artist, t=display_title, u=det.url or "":
                        enqueue_if_needed(a, t, u),
                    exclusive=False,
                    thread=True,
                )
            except Exception:
                log.debug("postprocess enqueue dispatch failed", exc_info=True)
        state = lyric_display_state(lyrics)
        if state == "synced":
            self._timeline = timeline_from_lyrics(lyrics)
            size = now.content_size
            banner = self.title_banner(display_artist, display_title,
                                       size.width or 0, (size.height or 0) - 1)
            now.update(
                f"{banner}\n"
                f"{det.mode} · {det.player or '—'} · {keybpm_line} · "
                f"{lyrics.source} · offset {self._sync_offset:+.1f}s (, / .)"
            )
        elif state == "unsynced":
            # Words without timings. Previously this fell through to "no
            # lyrics", which was wrong in a way that hid real text: the player
            # showed the song's words while the TUI claimed to have none and
            # queued it for backfill it did not need. There is nothing to
            # highlight, so they are shown plainly and labelled as unsynced.
            self._timeline = LyricTimeline([])
            lyrics_widget = self.query_one("#lyrics", Static)
            lyrics_widget.border_subtitle = f"unsynced · {lyrics.source}"
            lyrics_widget.update(lyrics.plain.strip())
            now.update(
                f"♪ {display_artist} - {display_title}\n"
                f"mode: {det.mode}  player: {det.player or '—'}\n"
                f"{keybpm_line}\n"
                f"{lyrics.source} · words only, no timings"
            )
        else:
            self._timeline = LyricTimeline([])
            lyrics_widget = self.query_one("#lyrics", Static)
            lyrics_widget.border_subtitle = None
            lyrics_widget.update(
                f"No lyrics for {display_artist} - {display_title}.\n"
                "Added to the staging/backfill queue — run karaoke-stage or "
                "karaoke-backfill, then whitelist it in the Staging view."
            )
            now.update(
                f"♪ {display_artist} - {display_title}\n"
                f"mode: {det.mode}  player: {det.player or '—'}\n"
                f"{keybpm_line}\n"
                "no lyrics — queued for staging"
            )

    def _tick_lyrics(self) -> None:
        if not self._det.is_active:
            return
        # Radio mode has no MPRIS player to ask; the playhead is dead-reckoned
        # from where songrec last heard us.
        pos = self.mic_elapsed() if self._det.mode == "radio" else \
            playerctl.position(self._control_player())
        if pos is None:
            return
        # Pull the highlight back by the sync offset: browser MPRIS position runs
        # ahead of audible output, so lyrics would otherwise lead the sound.
        elapsed = max(0.0, pos - self._sync_offset)
        self._elapsed = elapsed
        if self._timeline.lines:
            self._render_synced(elapsed)      # also refreshes the visuals
            return
        # No synced lyrics for this track -- common in Spotify mode -- but the
        # beat animation only needs BPM and elapsed time. Gating it on the
        # timeline froze the rhythm bar and cartwheel while the BPM read-out
        # beside them kept updating, which just looked broken.
        self._render_visuals(self._current_song_row(), "", elapsed)

    def _render_synced(self, elapsed: float) -> None:
        tl = self._timeline
        active = tl.active_index(elapsed)
        mood = mood_of(tl.lines[active][1]) if active >= 0 else "neutral"
        lyrics_widget = self.query_one("#lyrics", Static)
        # Fill the panel. _render_body defaults to 8 lines total, which left
        # most of a full-height pane empty. Weighted towards what is coming up,
        # and the active line is kept off the very top and bottom edges.
        rows = max(0, lyrics_widget.content_size.height)
        before, after = (3, 5) if rows < 10 else (rows // 3, rows - rows // 3)
        body = Text()
        _render_body(body, tl, elapsed, mood=mood, before=before, after=after)
        lyrics_widget.update(body)
        
        nxt = tl.next_time(elapsed)
        lyrics_widget.border_subtitle = f"next in {nxt - elapsed:0.1f}s" if nxt else "(end)"
        if mood != self._sync_mood:
            self._sync_mood = mood
            self._update_mood(mood)
        # Refresh the sentiment/rhythm read-out against the playing track.
        preview = "\n".join(t for _, t in tl.lines)
        self._render_visuals(self._current_song_row(), preview, elapsed)

    def _current_song_row(self) -> SongMapping:
        """Best-effort song row for the currently-syncing track (for visuals)."""
        return {"artist": self._det.artist, "title": self._det.title}

    # -- visuals ----------------------------------------------------------
    def _update_mood(self, mood: str) -> None:
        """Show the mood, as a picture when one has been rendered for it.

        The mood *word* stays above the image. The picture carries the feeling,
        but it should not be the only thing naming it -- the word is what makes
        a wrong match obviously wrong rather than merely odd, and it is the only
        thing that still works on a terminal without colour.
        """
        panel = self.query_one("#mood-square", Static)
        if mood != self._mood_shown or self._mood_art is None:
            # A new mood needs a new picture; rendering happens off the UI
            # thread, so fall back to the glyph block until it arrives.
            if mood != self._mood_shown:
                self._mood_shown = mood
                self._refresh_mood_art(mood)
        if self._mood_art is None:
            panel.update(
                f"{mood.upper()}\n\n{MOOD_GLYPHS.get(mood, MOOD_GLYPHS['neutral'])}"
            )
            return
        label = Text(f"{mood.upper()}  ", style="bold")
        label.append(self._mood_source, style="dim")
        panel.update(Text("\n").join([label, self._mood_art]))

    def _refresh_mood_art(self, mood: str) -> None:
        """Render a picture for this mood in a worker thread.

        Scoring the cover pool costs a handful of ffmpeg calls, so this runs on
        a mood change only -- never on the 0.2s lyric tick, which is what reads
        the result.
        """
        def _work() -> None:
            from . import coverart, moodframe
            try:
                panel = self.query_one("#mood-square", Static)
                cols = max(0, panel.content_size.width)
                rows = max(0, panel.content_size.height - 1)   # the mood word
                if cols < 4 or rows < 2:
                    return
                analysis = self._lookup_analysis(self._current_song_row())
                pixels, source = moodframe.image_for(mood, analysis, cols, rows)
                if not pixels:
                    return
                self._mood_art = coverart.to_text(pixels)
                self._mood_source = source
                self.call_from_thread(self._update_mood, mood)
            except Exception:
                log.debug("mood art refresh failed", exc_info=True)

        try:
            self.run_worker(_work, exclusive=False, thread=True)
        except Exception:
            log.debug("mood art dispatch failed", exc_info=True)

    def _update_keybpm(self, song: SongMapping | None) -> None:
        panel = self.query_one("#keybpm", Static)
        if song is None:
            panel.update("key: —\nbpm: —")
            return
        analysis = self._lookup_analysis(song)
        if analysis is None:
            panel.update(
                "key: not analysed\nbpm: —\n"
                "(make install-audio, then\n karaoke-analyze)"
            )
            return
        key = analysis.resolved_key or analysis.detected_key
        key_line = key.name if key else "unknown"
        extras = []
        if key:
            extras.append(f"camelot {key.camelot}")
        if analysis.key_relation in ("relative", "parallel", "conflict"):
            extras.append(analysis.key_relation)
        bpm_line = f"{analysis.bpm:.0f} bpm · {visuals.tempo_word(analysis.bpm)}" \
            if analysis.bpm else "bpm —"
        conf = f"conf {analysis.key_confidence:.0%} {analysis.key_agreement}" \
            if analysis.key_confidence else ""
        panel.update(
            f"key: {key_line}"
            + (f"  ({', '.join(extras)})" if extras else "")
            + f"\n{bpm_line}\n{conf}"
        )

    def _lookup_analysis(self, song: SongMapping):
        raw_id = song.get("track_id")
        try:
            with localcache.connect() as conn:
                if raw_id is None:
                    resolved = localcache.find_track_id(
                        str(song.get("artist") or ""),
                        str(song.get("title") or ""),
                        conn,
                    )
                else:
                    resolved = int(str(raw_id))
                if resolved is None:
                    return None
                return track_analysis.get_analysis(int(resolved), conn)
        except Exception:
            return None

    def _format_keybpm_line(self, song: SongMapping | None) -> str:
        """Compact key/BPM line for the now-playing panel."""
        analysis = self._lookup_analysis(song) if song else None
        if analysis is None:
            return "key: not analysed  bpm: —"
        key = analysis.resolved_key or analysis.detected_key
        key_text = key.name if key else "unknown"
        bpm_text = f"{analysis.bpm:.0f}" if analysis.bpm else "—"
        return f"key: {key_text}  bpm: {bpm_text}"

    def _render_visuals(self, song: SongMapping | None, preview: str,
                        elapsed: float) -> None:
        profile = visuals.analyze_sentiment(preview or "")
        if not (self._det.is_active and self._timeline.lines):
            self._update_mood(profile.dominant)
        self._update_keybpm(song)
        analysis = self._lookup_analysis(song) if song else None
        bpm = analysis.bpm if analysis else None
        arc = visuals.sentiment_arc(profile)
        bars = visuals.sentiment_bars(profile)
        rhythm = visuals.rhythm_bar(bpm, elapsed)
        cartwheel = visuals.cartwheel_frame(bpm, elapsed)
        self.query_one("#ascii-visual", Static).update(
            f"sentiment arc\n{arc}\n\n{bars}\n\nrhythm\n{rhythm}\n\n{cartwheel}"
        )

    def cover_source(self, url: str = "") -> "os.PathLike | None":
        """Where to get artwork for the current track.

        Prefers the player's own MPRIS art, which browsers have already written
        to a local file — no download, and it is the cover the user is looking
        at. Falls back to the cached YouTube media, whose first frame stands in
        when there is no cover.
        """
        from . import coverart

        # resolve_art, not art_path_from_url: the Spotify app publishes a
        # remote https artUrl where Chromium publishes a local file, so
        # local-only resolution left Spotify with no thumbnail at all.
        path = coverart.resolve_art(playerctl.art_url(self._control_player()) or "")
        if path is not None:
            return path
        vid = localcache.extract_youtube_id(url or "")
        if not vid:
            return None
        from .config import settings
        for ext in (".webm", ".m4a", ".mp4", ".mkv", ".opus", ".ogg"):
            candidate = Path(settings.youtube_dir) / f"{vid}{ext}"
            if candidate.is_file():
                return candidate
        return None

    @staticmethod
    def _load_duration(track_id: "int | None", conn) -> "float | None":
        """Stored track length in seconds, or None when unknown."""
        if track_id is None:
            return None
        row = conn.execute("SELECT duration FROM tracks WHERE track_id = ?",
                           (track_id,)).fetchone()
        return float(row["duration"]) if row and row["duration"] else None

    def _refresh_track_info(self, lyrics) -> None:
        """Fill the read-out under the cover art. Best-effort and never fatal."""
        try:
            pending = None
            duration = None
            genre = ""
            if self._current_track_id is not None:
                from .postprocess_queue import needs_postprocessing
                with localcache.connect() as conn:
                    pending = needs_postprocessing(self._current_track_id, conn)
                    duration = self._load_duration(self._current_track_id, conn)
                    genre = genre_line(
                        localcache.genre_for(self._current_track_id, conn))
            text = track_info(
                source=(lyrics.source if lyrics else ""),
                duration=duration,
                offset=self._sync_offset,
                pending=pending,
                lyric_lines=len(self._timeline.lines),
                error=self._last_error,
                genre=genre,
            )
        except Exception as exc:
            log.debug("track info refresh failed", exc_info=True)
            text = track_info(error=str(exc)[:40])
        try:
            self.query_one("#track-info", Static).update(text)
        except Exception:
            pass

    def _refresh_cover_art(self, url: str = "") -> None:
        """Render cover art into the left column, in a worker thread.

        ffmpeg is a subprocess, so this stays off the UI thread and only runs
        on a track change rather than every poll.
        """
        def _work() -> None:
            from . import coverart
            try:
                panel = self.query_one("#beat-art", Static)
                cols = max(0, panel.content_size.width)
                rows = max(0, panel.content_size.height)
                source = self.cover_source(url)
                art = None
                if source is not None and cols > 3 and rows > 1:
                    art = coverart.render(source, cols,
                                          min(rows, cols // coverart.CELL_ASPECT))
                if art is not None:
                    # Keep it while the source still resolves. Spotify's image
                    # URLs expire and YouTube thumbnails change with
                    # re-uploads, so this is the only moment the art is
                    # reliably obtainable.
                    self._remember_cover(source, url)
                elif cols > 3 and rows > 1:
                    # The source is gone or unreadable; a kept copy is what
                    # makes that recoverable rather than simply blank.
                    art = self._stored_cover(cols, rows)
                self.call_from_thread(panel.update, art if art is not None else "")
            except Exception as exc:
                log.debug("cover art refresh failed", exc_info=True)
                self._last_error = f"cover art: {exc}"[:60]

        try:
            self.run_worker(_work, exclusive=False, thread=True)
        except Exception:
            log.debug("cover art dispatch failed", exc_info=True)

    def _remember_cover(self, source, url: str) -> None:
        """Keep the art that just rendered, so it outlives its URL.

        Uses the track id the poll loop already resolved. An earlier version
        resolved it again in a helper named ``_current_track_id`` -- which is
        the name of the attribute holding it, so the attribute shadowed the
        method and calling it raised ``'NoneType' object is not callable``.
        """
        from . import cover_store

        track_id = self._current_track_id
        if track_id is None or source is None:
            return
        try:
            with localcache.connect() as conn:
                cover_store.ensure_table(conn)
                if cover_store.grid_for_track(track_id, conn) is not None:
                    return          # already kept; sampling again buys nothing
                cover_store.capture(track_id, source, conn, source_url=url)
        except Exception:
            log.debug("could not store cover art", exc_info=True)

    def _stored_cover(self, cols: int, rows: int):
        """Art from the database, for when the source no longer resolves."""
        from . import cover_store, coverart

        track_id = self._current_track_id
        if track_id is None:
            return None
        try:
            with localcache.connect() as conn:
                return cover_store.render_for_track(
                    track_id, cols, min(rows, max(1, cols // coverart.CELL_ASPECT)),
                    conn)
        except Exception:
            log.debug("could not read stored cover art", exc_info=True)
            return None

    def _background_fetch_spotify(self, track_id: int, artist: str,
                                  title: str) -> None:
        """Resolve a Spotify URI for a track and store it, in a worker thread.

        Runs at most once per track: a hit is cached as a ``spotify`` source and
        a miss in ``spotify_lookups``, and ``spotify_lookup_due`` consults both.
        Search is the rate-limited endpoint and this project has already lost a
        day of API access to calling it in a loop, so the guards matter more
        than the feature.
        """
        if self._spotify_off or not (artist and title):
            return
        from .spotify_client import (SpotifyAuthError, SpotifyClient,
                                     SpotifyRateLimited)
        try:
            uri = SpotifyClient().search_track(artist, title)
        except SpotifyRateLimited as exc:
            # Not a miss. Recording it would cache a false negative that
            # spotify_lookup_due would then honour permanently.
            self._spotify_off = True
            log.warning("spotify rate limited; lookups off for this session: %s", exc)
            self.call_from_thread(
                self.notify,
                f"Spotify rate limited; retry in {exc.retry_after // 60}min",
                severity="warning")
            return
        except SpotifyAuthError as exc:
            self._spotify_off = True
            log.warning("spotify auth failed; lookups off: %s (see: make auth-status)", exc)
            return
        except Exception:
            log.debug("spotify lookup failed for %s - %s", artist, title,
                      exc_info=True)
            return

        try:
            with localcache.connect() as conn:
                if uri:
                    localcache.add_track_source(artist, title, url=uri,
                                                kind="spotify", conn=conn)
                localcache.record_spotify_lookup(track_id, uri, conn)
        except Exception:
            log.debug("storing spotify lookup failed", exc_info=True)
            return
        if uri:
            log.info("spotify source stored for %s - %s", artist, title)

    def _background_fetch_lyrics(self, artist: str, title: str) -> None:
        """Fetch lyrics from LRCLIB for a cache miss, in a worker thread.

        The detection path is cache-only by design (it runs on a 1.5s timer and
        must not block on the network). This is the missing other half: on a
        miss, go and get them once, cache them, and clear the sync key so the
        next poll picks the track up with lyrics attached.
        """
        if not (artist and title):
            return
        try:
            from .lyrics import clean_title, fetch_lrclib

            found = None
            for candidate in {title, clean_title(title)}:
                ly = fetch_lrclib(artist, candidate)
                if ly.synced_raw or ly.plain:
                    found = ly
                    break
            if found is None:
                # LRCLIB does not have everything. The player may already be
                # showing the words in its own lyrics tab -- that panel is the
                # only source that knows about a track nobody has indexed.
                found = self._panel_lyrics(artist, title)
            if found is None:
                return
            with localcache.connect() as conn:
                localcache.add_track_and_lyrics(artist, title, found, conn=conn)
                # Words without timings cannot drive a session. Whisper can
                # supply the rhythm they lack -- but only from the whole track,
                # so this goes to the worker rather than being done here.
                if not found.synced_raw and found.plain:
                    self._enqueue_sync(artist, title, conn)
            log.info("fetched lyrics for %s - %s (%s)", artist, title, found.source)
            # Force the next poll to re-resolve rather than short-circuit on an
            # unchanged key.
            self._sync_key = None
        except Exception as exc:
            log.debug("background lyric fetch failed", exc_info=True)
            self._last_error = f"lyric fetch: {exc}"[:60]

    def _enqueue_sync(self, artist: str, title: str, conn) -> None:
        """Ask the worker to give plain lyrics a rhythm.

        Only when there is audio to align against: the lyrics panel answers for
        plenty of tracks whose audio cannot be fetched, and queueing those would
        fail on every retry the way Spotify-only analysis used to.
        """
        from .postprocess_queue import (enqueue_if_needed,
                                        has_downloadable_source)

        track_id = localcache.find_track_id(artist, title, conn)
        if track_id is None or not has_downloadable_source(track_id, conn):
            log.debug("no fetchable audio to sync %s - %s against", artist, title)
            return
        row = conn.execute(
            "SELECT url FROM sources WHERE track_id = ? AND url LIKE '%youtu%'"
            " LIMIT 1", (track_id,)).fetchone()
        if enqueue_if_needed(artist, title, (row["url"] if row else "") or "", conn):
            log.info("queued %s - %s for lyric alignment", artist, title)
            self.call_from_thread(
                self.notify, f"Queued {title} for lyric timing")

    def _panel_lyrics(self, artist: str, title: str):
        """Lyrics from the player's own lyrics tab, as a Lyrics object.

        Unsynced by nature -- LyricFind supplies words, not timings -- so these
        are stored as plain text and become a candidate for alignment against a
        transcription, which is what turns them into something singable.
        """
        from . import ytmusic_lyrics
        from .lyrics import Lyrics

        try:
            kind, panel = ytmusic_lyrics.capture_for_playing(artist, title)
        except Exception:
            log.debug("lyrics panel read failed", exc_info=True)
            return None
        if panel is None:
            return None
        if kind == "biography":
            self._store_panel_note(artist, title, panel)
            return None
        log.info("lyrics panel supplied %d lines for %s - %s (%s)",
                 len(panel.lines), artist, title, panel.attribution)
        return Lyrics(plain="\n".join(panel.lines),
                      source=ytmusic_lyrics.lyrics_source(panel))

    def _store_panel_note(self, artist: str, title: str, panel) -> None:
        """Keep the artist biography the panel showed instead of lyrics.

        This text used to be discarded once it had been recognised as not being
        lyrics, which cost a band history for every track that had one. It is
        not singable, so it never touches the lyrics table -- but it is exactly
        what a semantic search for a half-remembered band should find.
        """
        from . import localcache

        try:
            conn = localcache.connect()
            track_id = localcache.find_track_id(artist, title, conn)
            if track_id is None:
                log.debug("no track row for %r - %r; biography not stored",
                          artist, title)
                return
            if localcache.record_note(track_id, "biography", panel.text,
                                      "ytmusic_panel", conn):
                log.info("stored biography note (%d chars) for %s - %s",
                         len(panel.text), artist, title)
        except Exception:
            log.debug("could not store panel note", exc_info=True)

    def _background_autoload_captions(self, url: str, vid: str) -> None:
        """Fetch, stage, and auto-approve YouTube captions in a worker thread.

        Runs via run_worker(thread=True); all UI mutations are marshalled back
        onto the Textual event loop with call_from_thread.
        """
        self.call_from_thread(
            self.notify, f"Autoloading captions for {vid}…", severity="information"
        )
        try:
            from .stage_sources import stage_youtube_captions
            from . import staging

            result = stage_youtube_captions(url)
            # Only auto-approve when captions carry real timing (synced/enhanced).
            if result and caption_is_synced(result.source_kind) and result.lines > 0:
                staging.whitelist_staged(result.staged_id)
                self.call_from_thread(
                    self.notify,
                    f"Auto-loaded synced captions for {result.artist} - {result.title}",
                    severity="information",
                )
                log.info("Autoloaded and approved captions for %s (staged_id=%s)",
                         url, result.staged_id)
                # Re-poll so the newly approved lyrics load into the timeline now.
                self._sync_key = None
                self.call_from_thread(self._poll_detection)
            else:
                log.warning("Autoload: staged captions not synced for %s (kind=%s)",
                            url, result.source_kind if result else "none")
                self.call_from_thread(
                    self.notify,
                    "Autoload: only unsynced captions found; left in staging queue.",
                    severity="warning",
                )
        except Exception:
            log.exception("Background autoload failed for %s", url)
            self.call_from_thread(
                self.notify,
                "Autoload: no usable captions found on YouTube.",
                severity="warning",
            )


# -- pure helpers (unit-tested) ------------------------------------------
def caption_is_synced(source_kind: str) -> bool:
    """True when a staged caption source_kind carries real timing.

    Caption source kinds look like ``youtube_caption_manual_en-US_enhanced``
    or ``..._synced`` (timed) vs ``..._plain`` (no timing). Only timed captions
    are worth auto-approving into the live timeline.
    """
    return "_synced" in source_kind or "_enhanced" in source_kind


def first_nonempty_line(text: str) -> str:
    """Return the first non-empty line from a block of text."""
    for line in text.splitlines():
        clean = line.strip()
        if clean:
            return clean
    return ""


def lyric_preview(song: SongMapping, *, max_lines: int = 12) -> str:
    """Build a compact lyric preview from synced or plain cached lyrics."""
    synced = str(song.get("synced_lyrics") or "")
    plain = str(song.get("plain_lyrics") or "")
    source = synced or plain
    lines = [line.strip() for line in source.splitlines() if line.strip()]
    if not lines:
        return ""
    return "\n".join(lines[:max_lines])


def mood_for_preview(preview: str) -> str:
    """Pick the first non-neutral mood in a lyric preview."""
    for line in preview.splitlines():
        mood = mood_of(line)
        if mood != "neutral":
            return mood
    return "neutral"


def tui_main() -> int:
    """Run the player-aware karaoke TUI."""
    import os
    KaraokeTui(log_level=os.environ.get("KARAOKE_LOG", "err")).run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(tui_main())
