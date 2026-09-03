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

from collections.abc import Mapping
from urllib.parse import quote_plus

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
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

MOOD_GLYPHS = {
    "happy": "☀\n╲╱\n╱╲",
    "sad": "☔\n░░\n▒▒",
    "angry": "🔥\n▓▓\n██",
    "tender": "♡\n/\\\n\\/",
    "neutral": "◇\n··\n··",
}

FILTER_OPTIONS = [
    ("Working songs (have lyrics)", "working"),
    ("All songs", "all"),
    ("Staging queue", "staging"),
]

# Manual mode override cycle. None == auto-detect.
MODE_CYCLE = [None, "browse", "spotify", "scan"]


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

    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self._message)
            with Horizontal(id="buttons"):
                yield Button("Whitelist", variant="success", id="yes")
                yield Button("Cancel", variant="default", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class KaraokeTui(App):
    """A player-aware Textual shell for the karaoke experience."""

    CSS = """
    Screen { layout: vertical; }
    #workspace { height: 1fr; }
    #settings { width: 30; border: round cyan; padding: 1; }
    #main { width: 1fr; padding: 0 1; }
    #visuals { width: 34; border: round magenta; padding: 1; }
    #now-playing { height: 8; border: round green; padding: 1; margin-bottom: 1; }
    #lyrics { height: 14; border: round blue; padding: 1; margin-bottom: 1; overflow-y: auto; }
    #library { height: 1fr; }
    #mood-square {
        height: 8; content-align: center middle; text-style: bold;
        border: heavy white; margin-bottom: 1;
    }
    #keybpm { height: 6; border: round green; padding: 0 1; margin-bottom: 1; }
    #ascii-visual { height: 1fr; border: round yellow; padding: 0 1; }
    """

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("s", "resync", "Resync"),
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
        self._log_level = log_level
        self._autoloaded: set[str] = set()
        self._postprocess_enqueued: set[tuple[str, str]] = set()

    # -- layout -----------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="workspace"):
            with Vertical(id="settings"):
                yield Static("Settings / config", classes="panel-title")
                yield Static("Library filter")
                yield Select(FILTER_OPTIONS, value="working", id="filter-select",
                             allow_blank=False)
                yield Static("Mode: auto", id="mode-label")
                yield Static(
                    "m mode  space play/pause\n"
                    "n next  p prev  [ -5s  ] +5s\n"
                    "enter open/whitelist\n"
                    "l log level  r refresh",
                )
                yield Static(f"log: {self._log_level}", id="log-label")
                yield Static(f"Logs\n{LOG_FILE}")
            with Vertical(id="main"):
                yield Static("Detecting player…", id="now-playing")
                yield Static("Lyrics will render here.", id="lyrics")
                yield DataTable(id="library", cursor_type="row")
            with Vertical(id="visuals"):
                yield Static(MOOD_GLYPHS["neutral"], id="mood-square")
                yield Static("key: —\nbpm: —", id="keybpm")
                yield Static("sentiment / rhythm", id="ascii-visual")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#library", DataTable)
        table.add_columns("Artist", "Title", "Src", "♪")
        self.load_songs()
        self._show_selected_song()
        self.set_interval(1.5, self._poll_detection)
        self.set_interval(0.2, self._tick_lyrics)
        self._poll_detection()

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

    def action_refresh(self) -> None:
        self.load_songs()
        self._show_selected_song()
        self._poll_detection()
        self.notify("Refreshed")

    def action_resync(self) -> None:
        self._sync_key = None
        self._poll_detection()
        self.notify("Resynced playhead")

    def action_cycle_log(self) -> None:
        order = ["off", "err", "info", "full"]
        self._log_level = order[(order.index(self._log_level) + 1) % len(order)
                                if self._log_level in order else 1]
        stream_logs(self._log_level)
        self.query_one("#log-label", Static).update(f"log: {self._log_level}")
        log.warning("log level set to %s", self._log_level)
        self.notify(f"log level: {self._log_level}")

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
        suffix = f" (pid {pid})" if pid is not None else ""
        self.notify(f"Opening {artist} - {title}{suffix}")

    def action_cycle_mode(self) -> None:
        idx = MODE_CYCLE.index(self._mode_override)
        self._mode_override = MODE_CYCLE[(idx + 1) % len(MODE_CYCLE)]
        label = self._mode_override or "auto"
        log.info("mode override -> %s", label)
        if self._mode_override == "spotify":
            started, msg = detect.launch_spotify()
            self.notify(f"Spotify mode — {msg}")
            log.info("spotify mode: %s", msg)
        else:
            self.notify(f"Mode: {label}")
        self._sync_key = None  # force a re-resolve
        self._poll_detection()

    def _control_player(self) -> str:
        return self._det.mpris_name if self._det.is_active else ""

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
        """Apply the manual mode override on top of auto-detection."""
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

        self._sync_key = key
        with localcache.connect() as conn:
            artist, title, lyrics = detect.resolve_lyrics(det, conn)
            if lyrics is None or not lyrics.has_synced:
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
                f"synced lyrics · {lyrics.source}"
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
        pos = playerctl.position(self._control_player())
        if pos is None:
            return
        self._elapsed = pos
        self._render_synced(pos)

    def _render_synced(self, elapsed: float) -> None:
        tl = self._timeline
        active = tl.active_index(elapsed)
        mood = mood_of(tl.lines[active][1]) if active >= 0 else "neutral"
        body = Text()
        _render_body(body, tl, elapsed, mood=mood)
        
        lyrics_widget = self.query_one("#lyrics", Static)
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
