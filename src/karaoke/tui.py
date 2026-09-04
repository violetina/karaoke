"""Player-aware Textual TUI for the karaoke library.

Default behavior is automatic: the app watches the desktop via MPRIS/playerctl
and picks a mode.

- Spotify playing  -> ``spotify`` mode: sync + control Spotify.
- Browser/desktop player playing a YouTube / YT Music tab, VLC, etc.
  -> ``scan`` mode: sync lyrics to the player's position and control it.
- Nothing playing  -> ``browse`` mode: drive the library list; Enter opens the
  source in the browser (the old "youtube" behavior).

Press ``m`` to cycle a manual mode override (auto -> browse -> spotify -> scan).
Forcing ``spotify`` launches the Spotify desktop app if it isn't already running.

Tracks with no cached lyrics are recorded as gaps (for backfill / staging) and,
by default, hidden from the library so only working songs are listed. A filter
selector switches between working songs, all songs, and the staging queue; in the
staging view Enter asks to whitelist (approve) a candidate into the working list.

The right-hand visual space shows detected musical key, BPM/tempo and a live
sentiment + rhythm read-out for the current lyrics.
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Mapping
from urllib.parse import quote_plus

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Label, Select, Static

from . import detect, localcache, playerctl, staging, track_analysis, visuals
from .browse import open_song_url
from .logger import LOG_FILE, log, stream_logs
from .musictheory import parse_key
from .player import LyricTimeline, _render_body, timeline_from_lyrics
from .sentiment import mood_of

SongRow = dict[str, object]
SongMapping = Mapping[str, object]

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
    #main { width: 1fr; padding: 0 1; }
    #visuals { width: 34; border: round magenta; padding: 1; }
    #now-playing { height: 8; border: round green; padding: 1; margin-bottom: 1; }
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
    #keybpm { height: 6; border: round green; padding: 0 1; margin-bottom: 1; }
    #ascii-visual { height: 1fr; border: round yellow; padding: 0 1; }
    #worker-panel {
        height: auto; border: round cyan; padding: 0 1; margin-top: 1;
    }

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
    Screen.-short #now-playing { height: 3; padding: 0 1; margin-bottom: 0; }
    Screen.-short Header { display: none; }

    /* Focus mode: nothing but the lyrics, at any size. */
    Screen.-focus #visuals { display: none; }
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
        ("R", "toggle_mic", "Mic/radio"),
        ("F", "toggle_focus", "Focus"),
        ("question_mark", "help", "Keys"),
        Binding("escape", "hide_browse", "Close browse", show=False),
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
        self._mic_stop: threading.Event | None = None

    # -- layout -----------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="workspace"):
            with Vertical(id="main"):
                yield Static("Detecting player…", id="now-playing")
                yield Static("Lyrics will render here.", id="lyrics")
                with Horizontal(id="statusbar"):
                    yield Static("Mode: auto", id="mode-label")
                    yield Static("worker-load: —", id="worker-load")
            with Vertical(id="visuals"):
                yield Static(MOOD_GLYPHS["neutral"], id="mood-square")
                yield Static("key: —\nbpm: —", id="keybpm")
                yield Static("sentiment / rhythm", id="ascii-visual")
                yield Static("workers  —", id="worker-panel")
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
        self.apply_size_classes(self.size.width, self.size.height)
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

    def action_hide_browse(self) -> None:
        """escape: close the overlay if open, otherwise do nothing."""
        if self._browse_open():
            self._hide_browse()

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
        """
        ref = self._mic_ref
        if ref is None or ref.offset is None or ref.offset_mono is None:
            return None
        now = time.monotonic() if now is None else now
        return max(0.0, ref.offset + (now - ref.offset_mono))

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
        hint = " · press S to save" if self._current_track_id is not None else ""
        self.notify(f"Lyric sync offset: {self._sync_offset:+.1f}s{hint}")
        if self._det.is_active and self._timeline.lines:
            self._tick_lyrics()

    def action_save_offset(self) -> None:
        if self._current_track_id is None:
            self.notify("No track to save offset for", severity="warning")
            return
        with localcache.connect() as conn:
            localcache.set_sync_offset(self._current_track_id, self._sync_offset, conn)
        self._offset_dirty = False
        self.notify(f"Saved offset {self._sync_offset:+.1f}s for this track")
        log.info("saved sync offset %.2f for track %s",
                 self._sync_offset, self._current_track_id)

    def _prompt_save_offset(self, track_id: int, offset: float) -> None:
        """Ask whether to persist an unsaved offset for a track that's ending."""
        def _on_confirm(save: bool | None) -> None:
            if save:
                with localcache.connect() as conn:
                    localcache.set_sync_offset(track_id, offset, conn)
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

        A live mic identification wins outright: it hears what is actually in
        the room, which is the whole reason to turn it on. MPRIS may be
        reporting a different (or stale) track from some other player.
        """
        if self._mic_ref is not None and self._mic_ref.title:
            return detect.Detection(
                mode="radio", player="songrec",
                artist=self._mic_ref.artist, title=self._mic_ref.title,
            )
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
                localcache.get_sync_offset(self._current_track_id, conn)
                if self._current_track_id is not None else None
            )
            self._sync_offset = saved if saved is not None else _default_sync_offset(det.mode)
            self._offset_dirty = False
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
        if lyrics is not None and lyrics.has_synced:
            self._timeline = timeline_from_lyrics(lyrics)
            now.update(
                f"♪ {display_artist} - {display_title}\n"
                f"mode: {det.mode}  player: {det.player or '—'}\n"
                f"{keybpm_line}\n"
                f"synced lyrics · {lyrics.source} · offset {self._sync_offset:+.1f}s (, / .)"
            )
        else:
            self._timeline = LyricTimeline([])
            lyrics_widget = self.query_one("#lyrics", Static)
            lyrics_widget.border_subtitle = None
            lyrics_widget.update(
                f"No synced lyrics for {display_artist} - {display_title}.\n"
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
        if not (self._det.is_active and self._timeline.lines):
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
        self._render_synced(elapsed)

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
        self.query_one("#mood-square", Static).update(
            f"{mood.upper()}\n\n{MOOD_GLYPHS.get(mood, MOOD_GLYPHS['neutral'])}"
        )

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
                return
            with localcache.connect() as conn:
                localcache.add_track_and_lyrics(artist, title, found, conn=conn)
            log.info("fetched lyrics for %s - %s (%s)", artist, title, found.source)
            # Force the next poll to re-resolve rather than short-circuit on an
            # unchanged key.
            self._sync_key = None
        except Exception:
            log.debug("background lyric fetch failed", exc_info=True)

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
