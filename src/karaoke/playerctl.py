"""MPRIS/playerctl helpers for desktop media metadata.

This module keeps playerctl subprocess details and metadata cleanup out of the
CLI. MPRIS is the common Linux desktop media-player API used by Cosmic/GNOME/KDE
players, browsers, VLC and Spotify.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Optional

from .identify import SongRef, parse_query


@dataclass(frozen=True)
class PlayerMetadata:
    """Metadata returned by playerctl/MPRIS for the active player."""

    artist: str = ""
    title: str = ""
    album: str = ""
    url: str = ""
    player: str = ""


def _clean_piece(text: str) -> str:
    """Trim whitespace and common quote wrappers from player metadata."""
    return (text or "").strip().strip('"\u201c\u201d\u2018\u2019')


def normalize_player_track(artist: str, title: str, album: str = "", url: str = "") -> SongRef:
    """Normalize noisy MPRIS metadata into a SongRef.

    Some players (notably VLC for files with imperfect tags) expose artist in
    both artist and title, e.g. artist="Tom Waits" and title='Tom Waits - "Watch
    Her Disappear"'. Strip that duplicated artist before doing lyric lookup.
    """
    a = _clean_piece(artist)
    t = _clean_piece(title)
    if a and t.casefold().startswith((a + " - ").casefold()):
        t = _clean_piece(t[len(a) + 3:])
    elif a and t.casefold().startswith((a + " – ").casefold()):
        t = _clean_piece(t[len(a) + 3:])
    elif a and t.casefold().startswith((a + " — ").casefold()):
        t = _clean_piece(t[len(a) + 3:])
    elif not a and " - " in t:
        parsed = parse_query(t)
        a, t = parsed.artist, parsed.title
    return SongRef(artist=a, title=t, album=_clean_piece(album), url=url, source="player")


def _metadata_format() -> str:
    # Unit Separator between fields; unlikely in song metadata and easy to split.
    return "{{artist}}\x1f{{title}}\x1f{{album}}\x1f{{xesam:url}}\x1f{{playerName}}"


def current_metadata() -> Optional[PlayerMetadata]:
    """Return active MPRIS metadata via playerctl, or None if unavailable."""
    try:
        proc = subprocess.run(
            ["playerctl", "metadata", "--format", _metadata_format()],
            capture_output=True, text=True, timeout=5, check=True,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    parts = proc.stdout.rstrip("\n").split("\x1f")
    if len(parts) < 2:
        return None
    parts += [""] * (5 - len(parts))
    meta = PlayerMetadata(
        artist=_clean_piece(parts[0]),
        title=_clean_piece(parts[1]),
        album=_clean_piece(parts[2]),
        url=_clean_piece(parts[3]),
        player=_clean_piece(parts[4]),
    )
    if not meta.artist and not meta.title:
        return None
    return meta


def current_songref() -> Optional[SongRef]:
    """Resolve active desktop-player metadata to a SongRef."""
    meta = current_metadata()
    if meta is None:
        return None
    ref = normalize_player_track(meta.artist, meta.title, meta.album, meta.url)
    if not ref.title:
        return None
    return ref


def _run(cmd: list[str], *, timeout: float = 5.0) -> Optional[str]:
    """Run a playerctl command, returning stdout or None on failure."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=True
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    return proc.stdout.strip()


def _base_cmd(player: str = "") -> list[str]:
    cmd = ["playerctl"]
    if player:
        cmd += ["--player", player]
    return cmd


def position(player: str = "") -> Optional[float]:
    """Current playback position in seconds for the (targeted) player."""
    out = _run(_base_cmd(player) + ["position"])
    if out is None:
        return None
    try:
        return float(out)
    except ValueError:
        return None


def status(player: str = "") -> Optional[str]:
    """Playback status string (Playing/Paused/Stopped) or None."""
    return _run(_base_cmd(player) + ["status"])


def play_pause(player: str = "") -> bool:
    """Toggle play/pause on the (targeted) player. Returns success."""
    return _run(_base_cmd(player) + ["play-pause"]) is not None


def next_track(player: str = "") -> bool:
    """Skip to the next track. Returns success."""
    return _run(_base_cmd(player) + ["next"]) is not None


def previous_track(player: str = "") -> bool:
    """Skip to the previous track. Returns success."""
    return _run(_base_cmd(player) + ["previous"]) is not None


def seek(offset_s: float, player: str = "") -> bool:
    """Seek by a relative offset (seconds; may be negative). Returns success."""
    arg = f"{offset_s:+g}" if offset_s < 0 else f"{offset_s:g}+"
    return _run(_base_cmd(player) + ["position", arg]) is not None
