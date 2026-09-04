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


class SpotifyRateLimited(RuntimeError):
    """Raised on HTTP 429 so callers stop instead of mistaking it for a miss."""

    def __init__(self, retry_after: int = 0) -> None:
        self.retry_after = retry_after
        hours = retry_after / 3600.0
        super().__init__(
            f"Spotify rate limit hit; retry after {retry_after}s (~{hours:.1f}h)"
        )


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

    # --- playlist building ------------------------------------------------

    def current_user_id(self) -> str:
        """Return the authenticated user's Spotify id."""
        r = requests.get(f"{_API}/me", headers=self._headers(), timeout=10)
        if r.status_code != 200:
            raise SpotifyAuthError(f"me failed: {r.status_code} {r.text[:200]}")
        return r.json().get("id", "")

    def _search_items(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Run one Spotify track search and return the raw items.

        Raises :class:`SpotifyRateLimited` on HTTP 429. Swallowing that would be
        actively misleading — an exhausted quota is indistinguishable from "no
        such track", and a caller would record hundreds of real songs as missing.
        """
        r = requests.get(
            f"{_API}/search", headers=self._headers(),
            params={"q": query, "type": "track", "limit": limit}, timeout=10,
        )
        if r.status_code == 429:
            raise SpotifyRateLimited(int(r.headers.get("Retry-After") or 0))
        if r.status_code != 200:
            return []
        return ((r.json().get("tracks") or {}).get("items") or [])

    def search_track(self, artist: str, title: str) -> Optional[str]:
        """Return the best-matching Spotify track URI for artist/title, or None.

        Tries the strict field-filtered query first, then progressively looser
        ones, because library metadata carries noise Spotify does not index:
        "feat. X" inside the title, a full credited-artist list where Spotify
        has only the primary, and rows with artist and title swapped.

        Every loose result is checked with :func:`track_matches` before being
        accepted, so relaxing the query cannot silently return a different song.
        """
        from .spotify_playlist import primary_artist, search_title, track_matches

        a, t = artist.strip(), title.strip()
        pa, st = primary_artist(a), search_title(t)

        # 1. Strict: exact-ish filters. Trust these without extra checking.
        for cand_a, cand_t in {(a, t), (pa, st)}:
            if not (cand_a and cand_t):
                continue
            items = self._search_items(f'track:"{cand_t}" artist:"{cand_a}"', limit=1)
            if items:
                return items[0].get("uri")

        # 2. Loose free text, including the swapped reading — verified.
        for q_artist, q_title in ((pa, st), (st, pa)):
            if not (q_artist and q_title):
                continue
            for item in self._search_items(f"{q_artist} {q_title}", limit=5):
                names = [x.get("name", "") for x in (item.get("artists") or [])]
                if track_matches(q_artist, q_title, names, item.get("name", "")):
                    return item.get("uri")
        return None

    def find_playlist(self, name: str) -> Optional[str]:
        """Return the id of the user's playlist with this exact name, if any.

        Lets a rebuild target the existing playlist instead of creating a
        duplicate every run.
        """
        url: Optional[str] = f"{_API}/me/playlists?limit=50"
        while url:
            r = requests.get(url, headers=self._headers(), timeout=10)
            if r.status_code != 200:
                return None
            data = r.json()
            for pl in data.get("items") or []:
                if (pl.get("name") or "") == name:
                    return pl.get("id")
            url = data.get("next")
        return None

    def playlist_track_uris(self, playlist_id: str) -> list[str]:
        """Return every track URI already in a playlist (paged)."""
        uris: list[str] = []
        url: Optional[str] = f"{_API}/playlists/{playlist_id}/tracks?limit=100"
        while url:
            r = requests.get(url, headers=self._headers(), timeout=15)
            if r.status_code != 200:
                break
            data = r.json()
            for item in data.get("items") or []:
                uri = ((item.get("track") or {}).get("uri") or "")
                if uri:
                    uris.append(uri)
            url = data.get("next")
        return uris

    def create_playlist(self, name: str, *, description: str = "",
                        public: bool = False) -> str:
        """Create a playlist for the current user and return its id."""
        user_id = self.current_user_id()
        r = requests.post(
            f"{_API}/users/{user_id}/playlists", headers=self._headers(),
            json={"name": name, "description": description, "public": public},
            timeout=15,
        )
        if r.status_code not in (200, 201):
            raise SpotifyAuthError(
                f"playlist create failed: {r.status_code} {r.text[:200]}")
        return r.json().get("id", "")

    def add_playlist_tracks(self, playlist_id: str, uris: list[str]) -> int:
        """Append track URIs to a playlist. Returns how many were added.

        Spotify caps each request at 100 URIs, so this batches.
        """
        added = 0
        for start in range(0, len(uris), 100):
            batch = uris[start:start + 100]
            r = requests.post(
                f"{_API}/playlists/{playlist_id}/tracks", headers=self._headers(),
                json={"uris": batch}, timeout=15,
            )
            if r.status_code not in (200, 201):
                raise SpotifyAuthError(
                    f"playlist add failed: {r.status_code} {r.text[:200]}")
            added += len(batch)
        return added
