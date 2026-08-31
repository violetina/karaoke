"""Karaoke lyric lookup + synced terminal renderer.

Timing logic (`active_index`, `LyricTimeline`) is pure and unit-tested without
audio. Rendering uses Rich; playback clock is driven by time.monotonic() from a
user start keypress (MVP). True audio-position sync is a later upgrade.
"""
from __future__ import annotations

import time
import select
import subprocess
from dataclasses import dataclass, field
from typing import Any, Optional

from .lyrics import Lyrics, fetch_lrclib, parse_lrc
from .identify import SongRef

# Active-line background per detected mood (see sentiment.mood_of). Word highlight
# stays magenta on top; neutral keeps the original blue.
_MOOD_BG = {
    "happy": "on green",
    "sad": "on blue",
    "angry": "on red",
    "tender": "on deep_pink4",
    "neutral": "on blue",
}
# Panel border colour per mood — also the colour used for the beat "flash".
_MOOD_BORDER = {
    "happy": "green",
    "sad": "blue",
    "angry": "red",
    "tender": "magenta",
    "neutral": "cyan",
}


@dataclass
class LyricTimeline:
    """Ordered (time_seconds, text) lines with active-line lookup."""

    lines: list[tuple[float, str]] = field(default_factory=list)

    @property
    def times(self) -> list[float]:
        """Return only the lyric timestamps in display order."""
        return [t for t, _ in self.lines]

    def active_index(self, elapsed: float) -> int:
        """Index of the line active at `elapsed` seconds.

        Returns -1 before the first line's timestamp (intro), otherwise the
        index of the latest line whose timestamp is <= elapsed.
        """
        idx = -1
        for i, (t, _) in enumerate(self.lines):
            if t <= elapsed:
                idx = i
            else:
                break
        return idx

    def next_time(self, elapsed: float) -> Optional[float]:
        """Timestamp of the next upcoming line, or None if past the end."""
        for t, _ in self.lines:
            if t > elapsed:
                return t
        return None

    def active_fraction(self, elapsed: float, tail: float = 4.0) -> float:
        """Progress (0..1) through the currently-active line.

        Used to interpolate a word-level highlight: LRCLIB gives only per-LINE
        timestamps, so we spread the line's on-screen duration across its words.
        Returns 0.0 in the intro (no active line). For the last line (no next
        timestamp) assumes a `tail`-second duration.
        """
        a = self.active_index(elapsed)
        if a < 0:
            return 0.0
        start = self.lines[a][0]
        end = self.lines[a + 1][0] if a + 1 < len(self.lines) else start + tail
        if end <= start:
            return 0.0
        return max(0.0, min(1.0, (elapsed - start) / (end - start)))


def timeline_from_lyrics(ly: Lyrics) -> LyricTimeline:
    """Build a renderable timeline from parsed or raw LRC lyrics."""
    if ly.lines:
        return LyricTimeline(list(ly.lines))
    if ly.synced_raw:
        return LyricTimeline(parse_lrc(ly.synced_raw))
    return LyricTimeline([])


def get_synced(
    ref: SongRef,
    *,
    use_cache: bool = True,
    transcribe: bool = False,
    force_transcribe: bool = False,
    lyrics_file: Optional[str] = None,
    stats_mode: Optional[str] = None,
) -> Lyrics:
    """Return synced lyrics, checking caches before going online."""
    from . import localcache

    artist, title, album, duration = ref.artist, ref.title, ref.album, ref.duration
    audio_path = ref.path


    def _log(event: str, ly: Optional[Lyrics] = None) -> None:
        if not stats_mode:
            return
        localcache.log_event(
            stats_mode, event, artist=artist, title=title,
            source=(ly.source if ly else ""),
            has_synced=bool(ly and ly.has_synced),
        )

    # Force path: Whisper only, no cache read, no LRCLIB.
    if force_transcribe and audio_path:
        from .whisper_sync import transcribe_to_lrc
        
        plain_lyrics = None
        if lyrics_file:
            with open(lyrics_file) as f:
                plain_lyrics = f.read()

        lrc = transcribe_to_lrc(audio_path, text=plain_lyrics)
        ly = Lyrics(
            plain="\n".join(t for _, t in parse_lrc(lrc)),
            synced_raw=lrc, source="whisper", lines=parse_lrc(lrc),
        ) if lrc.strip() else Lyrics()
        if ly.synced_raw or ly.plain:
            try:
                with localcache.connect() as conn:
                    localcache.add_track_and_lyrics(artist, title, ly, album=album, duration=duration, conn=conn)
            except Exception:
                pass
        return ly

    # 1. Local SQLite cache first
    if use_cache:
        with localcache.connect() as conn:
            track_id = localcache.find_track_id(artist, title, conn)
            if track_id:
                cached = localcache.get_lyrics_by_track_id(track_id, conn)
                if cached is not None and (cached.synced_raw or cached.plain):
                    _log("cache_hit", cached)
                    return cached

    if use_cache:
        _log("cache_miss")

    ly = fetch_lrclib(artist, title, album, duration)

    # Whisper fallback
    if not ly.has_synced and transcribe and audio_path:
        try:
            from .whisper_sync import transcribe_to_lrc

            lrc = transcribe_to_lrc(audio_path)
            if lrc.strip():
                ly = Lyrics(
                    plain="\n".join(t for _, t in parse_lrc(lrc)),
                    synced_raw=lrc,
                    source="whisper",
                    lines=parse_lrc(lrc),
                )
        except Exception:
            pass

    if not (ly.synced_raw or ly.plain):
        _log("no_lyrics", ly)
        if stats_mode in ("radio", "player"):
            try:
                with localcache.connect() as conn:
                    localcache.log_lyric_gap(artist, title, conn)
            except Exception:
                pass
    
    # Write-through to local cache
    if use_cache and (ly.synced_raw or ly.plain):
        try:
            with localcache.connect() as conn:
                localcache.add_track_and_lyrics(artist, title, ly, album=album, duration=duration, url=ref.url, conn=conn)
        except Exception:
            pass
            
    return ly


def line_nudge_delta(times: list[float], elapsed: float, direction: int) -> float:
    """Clock delta to move the highlighted line by one, in seconds.

    `direction > 0` advances to the NEXT line (lyrics catch up to audio running
    ahead of the display); `direction < 0` steps back one line. Returns the
    amount to ADD to the lyric clock (the accumulated nudge). Returns 0.0 when
    already at an edge (past the last line going forward, or in the intro going
    back). A small epsilon lands the clock just inside the target line so the
    active-line lookup is unambiguous.
    """
    if not times:
        return 0.0
    # Active index: latest i with times[i] <= elapsed, else -1 (intro).
    a = -1
    for i, t in enumerate(times):
        if t <= elapsed:
            a = i
        else:
            break
    if direction > 0:
        j = a + 1
        if j >= len(times):
            return 0.0  # already at/after the last line
        return (times[j] + 0.05) - elapsed
    # backward
    if a <= -1:
        return 0.0  # already in the intro
    j = a - 1
    if j < 0:
        return (times[0] - 0.5) - elapsed  # step back into the intro
    return (times[j] + 0.05) - elapsed


class _KeyReader:
    """Non-blocking single-key reader (cbreak) for live nudge controls.

    cbreak (not raw) keeps ISIG on, so Ctrl-C still raises KeyboardInterrupt.
    Falls back to a no-op when stdin isn't a TTY (piped/tests). Reads one byte
    at a time via select so it never blocks the render loop.
    """

    def __init__(self) -> None:
        self._fd = None
        self._old = None

    def __enter__(self):
        import sys
        if not sys.stdin.isatty():
            return self
        import termios
        import tty
        self._fd = sys.stdin.fileno()
        self._old = termios.tcgetattr(self._fd)
        tty.setcbreak(self._fd)
        return self

    def get(self) -> Optional[str]:
        if self._fd is None:
            return None
        import select
        import sys
        r, _, _ = select.select([sys.stdin], [], [], 0)
        if r:
            return sys.stdin.read(1)
        return None

    def __exit__(self, *exc) -> None:
        if self._fd is not None and self._old is not None:
            import termios
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)


_NUDGE_HINT = "[v]-line [b]+line [0]reset [q]quit"

# Live recognition (songrec) listens ~10s before returning a position, biasing
# the reported offset low so the highlight starts a couple of lines behind the
# audio. Pre-bias the lyric clock forward by this many seconds on mic/radio
# locks so the start is close without needing to tap `b`. Overridable via --lead.
DEFAULT_LEAD_S = 13.0


def active_word_index(text: str, frac: float) -> int:
    """Index of the word to highlight given progress `frac` (0..1) through a line.

    LRCLIB timestamps are per-line only, so we spread the line's duration evenly
    across its whitespace-split words and pick the one under the playhead.
    Returns -1 for an empty/blank line. Clamps to the last word at frac>=1.
    """
    words = text.split()
    if not words:
        return -1
    f = max(0.0, min(0.999999, frac))
    return min(len(words) - 1, int(f * len(words)))


def _append_lyric_line(body, line: str, *, kind: str, frac: float = 0.0,
                       mood: str = "neutral") -> None:
    """Append one lyric line to a Rich Text body.

    kind: 'active' (current line — highlight the current word in purple over a
    mood-tinted background), 'past' (dim) or 'future' (grey). `frac` drives the
    word highlight; `mood` (from sentiment.mood_of) picks the active-line
    background. Imports Rich lazily so pure/tested code needn't depend on it.
    """
    if kind == "past":
        body.append("  " + line + "\n", style="dim")
        return
    if kind == "future":
        body.append("  " + line + "\n", style="grey70")
        return
    # active line: word-level purple highlight over a mood-tinted background
    bg = _MOOD_BG.get(mood, "on blue")
    base = f"bold white {bg}"
    wi = active_word_index(line, frac)
    body.append("♪ ", style=base)
    if wi < 0:
        body.append(line + "\n", style=base)
        return
    words = line.split()
    for j, w in enumerate(words):
        if j == wi:
            body.append(w, style="bold white on magenta")
        else:
            body.append(w, style=base)
        body.append(" " if j < len(words) - 1 else "\n", style=base)


def _render_body(body, tl: "LyricTimeline", elapsed: float,
                 *, before: int = 3, after: int = 5, mood: str = "neutral") -> None:
    """Fill a Rich Text `body` with the window around the active line.

    `mood` tints the active line's background (from sentiment.mood_of on the
    active line's text); pass "neutral" to keep the original blue.
    """
    active = tl.active_index(elapsed)
    frac = tl.active_fraction(elapsed)
    lo = max(0, active - before)
    hi = min(len(tl.lines), active + after)
    for i in range(lo, hi):
        line = tl.lines[i][1]
        if i == active:
            _append_lyric_line(body, line, kind="active", frac=frac, mood=mood)
        elif i < active:
            _append_lyric_line(body, line, kind="past")
        else:
            _append_lyric_line(body, line, kind="future")


def _active_mood(tl: "LyricTimeline", elapsed: float) -> str:
    """Mood of the currently-active lyric line (neutral in the intro)."""
    from .sentiment import mood_of
    a = tl.active_index(elapsed)
    if a < 0:
        return "neutral"
    return mood_of(tl.lines[a][1])


def _build_frame(tl: "LyricTimeline", elapsed: float, header: str, *,
                 beat_times=None, footer_extra: str = ""):
    """Assemble the mood-tinted, beat-flashed Rich Panel for one render tick.

    Shared by every player. `beat_times` (sorted list) drives a real on-beat
    border flash in --file mode; when None we fall back to a per-line pulse so
    audio-less modes (Spotify/live) still blink. The border colour follows the
    active line's mood; on a flash tick it brightens.
    """
    from rich.align import Align
    from rich.panel import Panel
    from rich.text import Text
    from .beats import beat_on, line_pulse

    mood = _active_mood(tl, elapsed)
    body = Text()
    _render_body(body, tl, elapsed, mood=mood)

    if beat_times:
        flash = beat_on(beat_times, elapsed)
    else:
        a = tl.active_index(elapsed)
        line_start = tl.lines[a][0] if a >= 0 else None
        flash = line_pulse(line_start, elapsed)

    color = _MOOD_BORDER.get(mood, "cyan")
    border_style = f"bold {color}" if flash else color

    nxt = tl.next_time(elapsed)
    foot = f"{mood}"
    foot += f"  ·  next in {nxt - elapsed:0.1f}s" if nxt else "  ·  (end)"
    if footer_extra:
        foot = f"{footer_extra}  ·  {foot}"
    return Panel(Align.left(body), title=header, subtitle=foot,
                 border_style=border_style)


def render_lines(tl: LyricTimeline, active: int, context: int = 3) -> str:
    """Plain-text window around the active line (used by tests + fallback)."""
    if not tl.lines:
        return "(no synced lyrics)"
    lo = max(0, active - context)
    hi = min(len(tl.lines), active + context + 1)
    out = []
    for i in range(lo, hi):
        prefix = ">> " if i == active else "   "
        out.append(f"{prefix}{tl.lines[i][1]}")
    return "\n".join(out)


def play(tl: LyricTimeline, *, title: str = "", artist: str = "",
         offset: float = 0.0,
         beat_times: Optional[list[float]] = None) -> None:  # pragma: no cover - interactive
    """Render synced lyrics with a Rich Live view, clock from keypress.

    `beat_times` (from beats.detect_beats on a local file) enables a real on-beat
    border flash; without it the border pulses once per lyric line.
    """
    from rich.console import Console
    from rich.live import Live
    from rich.panel import Panel

    console = Console()
    if not tl.lines:
        console.print("[yellow]No synced lyrics available for this track.[/]")
        return

    header = f"{artist} - {title}".strip(" -")
    console.print(f"[bold cyan]{header}[/]")
    console.input("[dim]Press Enter when the music starts...[/] ")
    start = time.monotonic()

    def frame() -> Panel:
        elapsed = time.monotonic() - start + offset
        return _build_frame(tl, elapsed, header, beat_times=beat_times)

    try:
        with Live(frame(), console=console, refresh_per_second=10, screen=False) as live:
            while True:
                elapsed = time.monotonic() - start + offset
                live.update(frame())
                if tl.next_time(elapsed) is None and \
                        elapsed > tl.times[-1] + 4:
                    break
                time.sleep(0.1)
    except KeyboardInterrupt:
        console.print("\n[dim]stopped[/]")


def play_offset_synced(
    tl: LyricTimeline,
    *,
    title: str = "",
    artist: str = "",
    offset: float,
    offset_mono: float,
    extra_latency: float = 0.0,
) -> None:  # pragma: no cover - interactive/live
    """Render synced lyrics anchored to a known track position (mic/room sync).

    `offset` is the position-in-track (seconds) reported by songrec at monotonic
    instant `offset_mono`. We advance a local clock from that anchor, so the
    highlighted line follows the live audio heard through the mic. `extra_latency`
    nudges for recognizer/audio delay (add a bit if lyrics run early).
    """
    from rich.align import Align
    from rich.console import Console
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text

    console = Console()
    if not tl.lines:
        console.print("[yellow]No synced lyrics available for this track.[/]")
        return

    header = f"{artist} - {title}".strip(" -")
    nudge = [float(extra_latency)]  # live per-line adjustment (v/b keys)

    def elapsed_now() -> float:
        return offset + (time.monotonic() - offset_mono) + nudge[0]

    def frame() -> Panel:
        from .beats import line_pulse
        e = elapsed_now()
        mood = _active_mood(tl, e)
        body = Text()
        _render_body(body, tl, e, mood=mood)
        nxt = tl.next_time(e)
        foot = f"{mood}  ·  {e:0.1f}s"
        foot += f"  ·  next in {nxt - e:0.1f}s" if nxt else "  ·  (end)"
        if abs(nudge[0]) > 1e-6:
            foot += f"  ·  nudge {nudge[0]:+.1f}s"
        foot += "  ·  " + _NUDGE_HINT
        a = tl.active_index(e)
        flash = line_pulse(tl.lines[a][0] if a >= 0 else None, e)
        color = _MOOD_BORDER.get(mood, "cyan")
        return Panel(Align.left(body), title=header, subtitle=foot,
                     border_style=f"bold {color}" if flash else color)

    console.print(f"[bold cyan]{header}[/]  [dim](synced to live audio — v/b nudge, q to stop)[/]")
    try:
        with _KeyReader() as keys, \
                Live(frame(), console=console, refresh_per_second=10, screen=False) as live:
            while True:
                k = keys.get()
                if k:
                    if k in ("q", "\x1b"):
                        break
                    elif k == "v":  # v = step lyrics one line BACK
                        nudge[0] += line_nudge_delta(tl.times, elapsed_now(), -1)
                    elif k == "b":  # b = step lyrics one line FORWARD (catch up)
                        nudge[0] += line_nudge_delta(tl.times, elapsed_now(), +1)
                    elif k == "0":
                        nudge[0] = float(extra_latency)
                e = elapsed_now()
                live.update(frame())
                if tl.next_time(e) is None and e > tl.times[-1] + 4:
                    break
                time.sleep(0.05)
    except KeyboardInterrupt:
        console.print("\n[dim]stopped[/]")


def play_radio_synced(
    *,
    mic: bool = True,
    reidentify_interval: float = 30.0,
    extra_latency: float = 0.0,
    listen_timeout: int = 30,
) -> None:  # pragma: no cover - interactive/live
    """Continuously follow live audio (radio/room): identify, sync, re-lock.

    Runs a background thread that re-identifies every `reidentify_interval`s.
    Each result either re-anchors the current song (drift correction) or, if the
    song changed, swaps in new lyrics. Between/over speech it keeps the last song
    and shows a listening hint. Ctrl-C to stop.
    """
    import threading

    from rich.align import Align
    from rich.console import Console
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text

    from .identify import identify_live
    from .player import get_synced, timeline_from_lyrics  # self-import safe

    console = Console()

    # Shared state guarded by a lock; updated by the background identifier.
    state = {
        "key": None,        # (artist, title) currently displayed
        "artist": "",
        "title": "",
        "tl": LyricTimeline([]),
        "offset": 0.0,
        "offset_mono": time.monotonic(),
        "status": "listening…",
        "has_lyrics": False,
    }
    lock = threading.Lock()
    stop = threading.Event()
    nudge = [float(extra_latency)]  # live per-line adjustment (v/b keys)

    def apply(ref) -> None:
        """Fold a fresh identification into shared state."""
        if ref is None or not ref.title or ref.offset is None:
            with lock:
                state["status"] = "listening… (no match — speech/ad/quiet?)"
            return
        key = (ref.artist.lower(), ref.title.lower())
        with lock:
            same = key == state["key"]
        if same:
            # Drift correction: re-anchor the clock, keep the timeline.
            with lock:
                state["offset"] = ref.offset
                state["offset_mono"] = ref.offset_mono
                state["status"] = "in sync"
            from . import localcache
            localcache.log_event("radio", "relock", artist=ref.artist, title=ref.title)
            return
        # New song -> fetch lyrics (slow) OUTSIDE the lock.
        from . import localcache
        localcache.log_event(
            "radio", "discover", artist=ref.artist, title=ref.title, source="songrec"
        )
        ly = get_synced(ref.artist, ref.title, stats_mode="radio")
        tl = timeline_from_lyrics(ly)
        localcache.log_event(
            "radio", "play", artist=ref.artist, title=ref.title,
            source=ly.source, has_synced=bool(tl.lines),
        )
        with lock:
            state["key"] = key
            state["artist"] = ref.artist
            state["title"] = ref.title
            state["tl"] = tl
            state["offset"] = ref.offset
            state["offset_mono"] = ref.offset_mono
            state["has_lyrics"] = bool(tl.lines)
            state["status"] = "in sync" if tl.lines else "no synced lyrics for this track"

    def identifier() -> None:
        # First pass immediately, then every interval.
        while not stop.is_set():
            try:
                ref = identify_live(mic=mic, timeout=listen_timeout)
                apply(ref)
            except Exception as e:  # keep the loop alive on transient errors
                with lock:
                    state["status"] = f"identify error: {e}"
            stop.wait(reidentify_interval)

    def elapsed_now() -> float:
        return state["offset"] + (time.monotonic() - state["offset_mono"]) + nudge[0]

    def frame() -> Panel:
        from .beats import line_pulse
        with lock:
            tl = state["tl"]
            artist, title = state["artist"], state["title"]
            status = state["status"]
            has = state["has_lyrics"]
        header = f"{artist} - {title}".strip(" -") or "listening for music…"
        body = Text()
        mood = "neutral"
        flash = False
        if has and tl.lines:
            e = elapsed_now()
            mood = _active_mood(tl, e)
            _render_body(body, tl, e, mood=mood)
            a = tl.active_index(e)
            flash = line_pulse(tl.lines[a][0] if a >= 0 else None, e)
        else:
            body.append("\n  ♪ …\n\n", style="dim")
        n = nudge[0]
        foot = f"{mood}  ·  {status}"
        if abs(n) > 1e-6:
            foot += f"  ·  nudge {n:+.1f}s"
        foot += "  ·  " + _NUDGE_HINT
        color = _MOOD_BORDER.get(mood, "cyan")
        return Panel(Align.left(body), title=header, subtitle=foot,
                     border_style=f"bold {color}" if flash else color)

    console.print("[bold cyan]Radio karaoke[/]  [dim](listening — v/b nudge, q or Ctrl-C to stop)[/]")
    th = threading.Thread(target=identifier, daemon=True)
    th.start()
    try:
        with _KeyReader() as keys, \
                Live(frame(), console=console, refresh_per_second=10, screen=False) as live:
            while not stop.is_set():
                k = keys.get()
                if k:
                    if k in ("q", "\x1b"):
                        break
                    elif k == "v":  # v = step lyrics one line BACK
                        with lock:
                            times = state["tl"].times
                        nudge[0] += line_nudge_delta(times, elapsed_now(), -1)
                    elif k == "b":  # b = step lyrics one line FORWARD (catch up)
                        with lock:
                            times = state["tl"].times
                        nudge[0] += line_nudge_delta(times, elapsed_now(), +1)
                    elif k == "0":  # reset nudge to the CLI-provided baseline
                        nudge[0] = float(extra_latency)
                live.update(frame())
                time.sleep(0.05)
    except KeyboardInterrupt:
        console.print("\n[dim]stopped[/]")
    finally:
        stop.set()


def play_spotify_loop(
    *,
    offset: float = 0.0,
    poll_interval: float = 1.0,
    use_cache: bool = True,
) -> None:  # pragma: no cover - interactive/live
    """Continuously follow Spotify playback, one track after another.

    For each track: fetch synced lyrics and sync to the live position. If a track
    has no synced lyrics, print a note and WAIT (polling) for the next song
    instead of exiting. Auto-advances when the track changes. Runs until playback
    stops entirely or Ctrl-C.
    """
    from rich.console import Console

    from .spotify_client import SpotifyClient

    console = Console()
    sp = SpotifyClient()
    handled_id: Optional[str] = None   # track we've already resolved (played or skipped)
    warned_idle = False

    console.print("[dim]Following Spotify — Ctrl-C to stop.[/]")
    try:
        while True:
            try:
                pb = sp.current_playback()
            except Exception as e:  # noqa: BLE001 - keep the loop alive on transient errors
                console.print(f"[red]Spotify error:[/] {e}")
                time.sleep(poll_interval)
                continue

            if pb is None or not pb.title:
                if not warned_idle:
                    console.print("[dim]Nothing playing on Spotify. Waiting for a track…[/]")
                    warned_idle = True
                handled_id = None
                time.sleep(poll_interval)
                continue
            warned_idle = False

            if pb.track_id == handled_id:
                # Already resolved this track (it had no lyrics) — keep waiting.
                time.sleep(poll_interval)
                continue

            handled_id = pb.track_id
            header = f"{pb.artist} - {pb.title}".strip(" -")
            console.print(f"[bold cyan]{header}[/]  [dim](fetching lyrics…)[/]")

            ly = get_synced(pb.artist, pb.title,
                            duration=pb.duration_ms / 1000.0, use_cache=use_cache,
                            stats_mode="spotify")
            tl = timeline_from_lyrics(ly)
            from . import localcache
            localcache.log_event(
                "spotify", "play", artist=pb.artist, title=pb.title,
                source=ly.source, has_synced=bool(tl.lines),
            )
            if not tl.lines:
                console.print(
                    f"[yellow]No synced lyrics for {header} "
                    f"(source={ly.source}). Waiting for the next track…[/]")
                continue

            # Sync this track until it changes or playback stops, then loop.
            _sync_one_spotify_track(sp, tl, header, pb, offset, poll_interval, console)
    except KeyboardInterrupt:
        console.print("\n[dim]stopped[/]")


def _sync_one_spotify_track(
    sp: Any,
    tl: LyricTimeline,
    header: str,
    pb0: Any,
    offset: float,
    poll_interval: float,
    console: Any,
) -> None:  # pragma: no cover - interactive/live
    """Render `tl` locked to the live Spotify position for the current track.

    Returns when the track changes or playback stops (so the caller advances).
    KeyboardInterrupt propagates to the caller to exit the whole loop.
    """
    from rich.align import Align
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text

    track_id = pb0.track_id
    pos0 = pb0.position_s
    mono0 = time.monotonic()
    playing = pb0.is_playing
    last_poll = time.monotonic()

    def elapsed_now() -> float:
        base = pos0 + offset
        # Only advance the local clock while actually playing (paused holds).
        return base + (time.monotonic() - mono0) if playing else base

    def frame() -> Panel:
        e = elapsed_now()
        return _build_frame(tl, e, header, footer_extra=f"{e:0.1f}s")

    with Live(frame(), console=console, refresh_per_second=10, screen=False) as live:
        while True:
            now = time.monotonic()
            if now - last_poll >= poll_interval:
                last_poll = now
                pb = sp.current_playback()
                if pb is None or not pb.title:
                    console.print("\n[dim]playback stopped[/]")
                    return
                if pb.track_id != track_id:
                    return  # track changed — caller resolves the new one
                pos0 = pb.position_s
                mono0 = time.monotonic()
                playing = pb.is_playing
            live.update(frame())
            time.sleep(0.1)
