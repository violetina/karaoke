"""Report whether playback auth is healthy, without exposing any secrets.

Two independent things have to be signed in for playback to work well, and both
fail *silently*:

- The Spotify Web API, via the OAuth credentials Hermes stores in
  ``~/.hermes/auth.json``. A revoked refresh token only shows up as features
  quietly not working.
- The kiosk Chrome profile, which carries the Spotify and YouTube Premium web
  sessions. It runs with ``--app=``, so it has no address bar and no profile
  menu — there is nowhere for it to tell you it has been signed out.

Prints validity only. Never token values.

Run: ``make auth-status``
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

OK, WARN, BAD = "ok  ", "warn", "FAIL"

CDP_URL = "http://localhost:9222/json"
KIOSK_PROFILE = Path(os.environ.get(
    "KIOSK_PROFILE", str(Path.home() / ".config" / "google-chrome-kiosk")))


def _line(status: str, label: str, detail: str) -> None:
    print(f"  [{status}] {label:<22s} {detail}")


def check_hermes_file() -> bool:
    """The credential file exists and carries a Spotify refresh token."""
    from karaoke.spotify_client import _AUTH_PATH

    if not _AUTH_PATH.is_file():
        _line(BAD, "hermes auth.json", f"missing at {_AUTH_PATH}")
        return False
    try:
        data = json.loads(_AUTH_PATH.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        _line(BAD, "hermes auth.json", f"unreadable: {exc}")
        return False

    sp = (data.get("providers") or {}).get("spotify") or {}
    if not sp.get("refresh_token"):
        _line(BAD, "hermes auth.json", "no spotify refresh_token")
        return False

    scopes = (sp.get("granted_scope") or sp.get("scope") or "").split()
    _line(OK, "hermes auth.json", f"{len(scopes)} scopes granted")
    for needed in ("user-read-playback-state", "user-read-currently-playing"):
        if needed not in scopes:
            _line(WARN, "  scope", f"{needed} not granted (position read-out off)")
    return True


def check_spotify_token() -> bool:
    """Refresh the access token and make one cheap identity call.

    Deliberately ``/v1/me`` and not a search: search is the rate-limited
    endpoint whose quota this project has already exhausted once, and a status
    check must never be the thing that exhausts it.
    """
    from karaoke.spotify_client import (SpotifyAuthError, SpotifyClient,
                                        SpotifyRateLimited)

    try:
        user_id = SpotifyClient().current_user_id()
    except SpotifyRateLimited as exc:
        _line(WARN, "spotify api", f"rate limited; retry in {exc.retry_after}s")
        return False
    except SpotifyAuthError as exc:
        _line(BAD, "spotify api", f"{exc} -- re-authorise Hermes")
        return False
    except Exception as exc:                # network, DNS, anything transient
        _line(WARN, "spotify api", f"unreachable: {type(exc).__name__}")
        return False

    # The id is the account's own public handle, not a secret, and seeing it is
    # how you catch a token that authenticates as the wrong account.
    _line(OK, "spotify api", f"token valid; user {user_id or '?'}")
    return True


def check_kiosk_profile() -> bool:
    """The playback profile exists on disk (i.e. has been launched before)."""
    if not KIOSK_PROFILE.is_dir():
        _line(WARN, "kiosk profile", f"not created yet at {KIOSK_PROFILE}")
        return False
    _line(OK, "kiosk profile", str(KIOSK_PROFILE))
    return True


def check_cdp() -> bool:
    """Whether the playback window is up and driveable over CDP."""
    try:
        with urllib.request.urlopen(CDP_URL, timeout=2.0) as resp:
            tabs = json.loads(resp.read().decode())
    except Exception:
        _line(WARN, "playback window", "not running (start with: make tui)")
        return False
    pages = [t for t in tabs if t.get("type") == "page"]
    where = (pages[0].get("url", "") if pages else "")[:58]
    _line(OK, "playback window", f"CDP :9222, {len(pages)} page(s) {where}")
    return True


def main() -> int:
    print("\nplayback auth status\n")
    have_creds = check_hermes_file()
    token_ok = check_spotify_token() if have_creds else False
    check_kiosk_profile()
    check_cdp()

    print()
    if not token_ok:
        print("  Spotify API is not usable. Re-authorise Hermes, then re-check.")
    print("  Browser sessions are separate from the API token: sign those in")
    print("  with 'make auth-spotify' and 'make auth-youtube' (close the")
    print("  playback window first).\n")
    return 0 if token_ok else 1


if __name__ == "__main__":
    sys.exit(main())
