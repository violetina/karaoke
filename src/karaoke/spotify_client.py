"""Minimal standalone Spotify client.

Reuses the OAuth credentials Hermes already stored in ~/.hermes/auth.json
(client_id + refresh_token + scopes) so no separate OAuth flow is needed. Only
implements what karaoke needs: read current playback position and basic control.

Refreshes the access token via the refresh_token grant when expired. Falls back
to env HERMES_SPOTIFY_CLIENT_ID if the auth file lacks a client_id.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import requests

_AUTH_PATH = Path(os.environ.get("HERMES_AUTH_JSON",
                                 str(Path.home() / ".hermes" / "auth.json")))
_ACCOUNTS = "https://accounts.spotify.com"
_API = "https://api.spotify.com/v1"


class SpotifyAuthError(RuntimeError):
    """Raised when Hermes Spotify credentials or Spotify API calls fail."""

    pass


@dataclass
class Playback:
    """Current Spotify playback state normalized for lyric sync."""

    is_playing: bool
    progress_ms: int
    duration_ms: int
    artist: str
    title: str
    track_id: str

    @property
    def position_s(self) -> float:
        """Current Spotify playback position in seconds."""
        return self.progress_ms / 1000.0


def _load_creds() -> dict[str, Any]:
    if not _AUTH_PATH.is_file():
        raise SpotifyAuthError(f"no auth file at {_AUTH_PATH}")
    data = json.loads(_AUTH_PATH.read_text())
    sp = (data.get("providers") or {}).get("spotify")
    if not sp or not sp.get("refresh_token"):
        raise SpotifyAuthError("no spotify refresh_token in auth.json")
    return sp


class SpotifyClient:
    """Small Spotify Web API client backed by Hermes' stored OAuth refresh token."""

    def __init__(self) -> None:
        """Load credentials and cached token state from Hermes auth storage."""
        self._creds = _load_creds()
        self._access_token: Optional[str] = self._creds.get("access_token")
        # expires_at may be str or number in the file; treat unknown as expired.
        try:
            self._expires_at = float(self._creds.get("expires_at") or 0)
        except (TypeError, ValueError):
            self._expires_at = 0.0

    def _client_id(self) -> str:
        return (self._creds.get("client_id")
                or os.environ.get("HERMES_SPOTIFY_CLIENT_ID", ""))

    def _refresh(self) -> None:
        cid = self._client_id()
        if not cid:
            raise SpotifyAuthError("no client_id (auth.json or HERMES_SPOTIFY_CLIENT_ID)")
        resp = requests.post(
            f"{_ACCOUNTS}/api/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": self._creds["refresh_token"],
                "client_id": cid,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            raise SpotifyAuthError(f"token refresh failed: {resp.status_code} {resp.text[:200]}")
        tok = resp.json()
        self._access_token = tok["access_token"]
        self._expires_at = time.time() + int(tok.get("expires_in", 3600)) - 30

    def _token(self) -> str:
        if not self._access_token or time.time() >= self._expires_at:
            self._refresh()
        assert self._access_token
        return self._access_token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}"}

    def current_playback(self) -> Optional[Playback]:
        """Return the current playback state, or None if nothing is playing."""
        r = requests.get(f"{_API}/me/player", headers=self._headers(), timeout=10)
        if r.status_code == 204 or not r.text.strip():
            return None
        if r.status_code != 200:
            raise SpotifyAuthError(f"player state failed: {r.status_code} {r.text[:200]}")
        d = r.json()
        item = d.get("item") or {}
        artists = item.get("artists") or []
        return Playback(
            is_playing=bool(d.get("is_playing")),
            progress_ms=int(d.get("progress_ms") or 0),
            duration_ms=int(item.get("duration_ms") or 0),
            artist=", ".join(a.get("name", "") for a in artists),
            title=item.get("name", ""),
            track_id=item.get("id", ""),
        )

    def play(self, uris: Optional[list[str]] = None,
             device_id: Optional[str] = None) -> None:
        """Start or resume playback, optionally with a list of Spotify track URIs."""
        params = {"device_id": device_id} if device_id else {}
        body = {"uris": uris} if uris else None
        r = requests.put(f"{_API}/me/player/play", headers=self._headers(),
                         params=params, json=body, timeout=10)
        if r.status_code not in (202, 204):
            raise SpotifyAuthError(f"play failed: {r.status_code} {r.text[:200]}")

    def pause(self) -> None:
        """Pause the active Spotify device if one is available."""
        requests.put(f"{_API}/me/player/pause", headers=self._headers(), timeout=10)

    def devices(self) -> list[dict[str, Any]]:
        """Return Spotify Connect devices visible to the authenticated account."""
        r = requests.get(f"{_API}/me/player/devices", headers=self._headers(), timeout=10)
        return r.json().get("devices", []) if r.status_code == 200 else []
