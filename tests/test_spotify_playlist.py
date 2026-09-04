"""Tests for building a Spotify playlist from karaoke-ready tracks (offline)."""
from unittest.mock import MagicMock

from karaoke import localcache, spotify_playlist as sp
from karaoke.lyrics import Lyrics


def _conn(tmp_path):
    return localcache.connect(tmp_path / "karaoke.db")


def _add(conn, artist, title, *, synced=True, url=None, kind="youtube"):
    ly = Lyrics(
        plain="a\nb",
        synced_raw="[00:01.00] a\n[00:05.00] b" if synced else "",
        source="lrclib",
    )
    localcache.add_track_and_lyrics(artist, title, ly, url=url, kind=kind, conn=conn)


# --- URI extraction --------------------------------------------------------

def test_extract_track_uri_from_open_url():
    assert sp.extract_track_uri(
        "https://open.spotify.com/track/2MrcvjSU1G6hNQivTHbttX"
    ) == "spotify:track:2MrcvjSU1G6hNQivTHbttX"


def test_extract_track_uri_from_uri_form():
    assert sp.extract_track_uri("spotify:track:abc123") == "spotify:track:abc123"


def test_extract_track_uri_ignores_non_spotify():
    assert sp.extract_track_uri("https://youtu.be/xyz") == ""
    assert sp.extract_track_uri("") == ""


# --- candidate selection ---------------------------------------------------

def test_karaoke_tracks_requires_synced_lyrics(tmp_path):
    """Plain-only tracks cannot drive a karaoke session, so they are excluded."""
    c = _conn(tmp_path)
    _add(c, "Synced Artist", "Timed Song", synced=True)
    _add(c, "Plain Artist", "Untimed Song", synced=False)

    names = {(t.artist, t.title) for t in sp.karaoke_tracks(c)}
    assert ("Synced Artist", "Timed Song") in names
    assert ("Plain Artist", "Untimed Song") not in names


def test_karaoke_tracks_uses_stored_spotify_uri(tmp_path):
    c = _conn(tmp_path)
    _add(c, "A", "B", url="https://open.spotify.com/track/XYZ", kind="spotify")

    got = [t for t in sp.karaoke_tracks(c) if t.artist == "A"]
    assert got[0].uri == "spotify:track:XYZ"
    assert got[0].resolved_by == "stored"


def test_karaoke_tracks_keeps_youtube_only_tracks_for_search(tmp_path):
    """A YouTube-only track must survive to the search step, not be dropped."""
    c = _conn(tmp_path)
    _add(c, "YT Only", "Song", url="https://youtu.be/abc", kind="youtube")

    got = [t for t in sp.karaoke_tracks(c) if t.artist == "YT Only"]
    assert len(got) == 1
    assert got[0].uri == ""


def test_karaoke_tracks_skips_blank_artist(tmp_path):
    c = _conn(tmp_path)
    _add(c, "", "Orphan Title")
    assert all(t.artist for t in sp.karaoke_tracks(c))


# --- URI resolution --------------------------------------------------------

def test_resolve_uris_fills_missing_from_search():
    client = MagicMock()
    client.search_track.return_value = "spotify:track:FOUND"
    cands = [sp.Candidate("A", "B")]

    sp.resolve_uris(cands, client)

    assert cands[0].uri == "spotify:track:FOUND"
    assert cands[0].resolved_by == "search"


def test_resolve_uris_marks_unresolved_on_miss():
    client = MagicMock()
    client.search_track.return_value = None
    cands = [sp.Candidate("Obscure", "Track")]

    sp.resolve_uris(cands, client)

    assert cands[0].uri == ""
    assert cands[0].resolved_by == "unresolved"


def test_resolve_uris_survives_search_error():
    """A failing search must not abort the whole playlist build."""
    client = MagicMock()
    client.search_track.side_effect = RuntimeError("rate limited")
    cands = [sp.Candidate("A", "B")]

    sp.resolve_uris(cands, client)

    assert cands[0].resolved_by == "unresolved"


def test_resolve_uris_does_not_search_for_stored_uris():
    client = MagicMock()
    cands = [sp.Candidate("A", "B", uri="spotify:track:HAVE", resolved_by="stored")]

    sp.resolve_uris(cands, client)

    client.search_track.assert_not_called()


# --- playlist build --------------------------------------------------------

def test_build_playlist_dry_run_touches_nothing(tmp_path):
    c = _conn(tmp_path)
    _add(c, "A", "B", url="https://open.spotify.com/track/XYZ", kind="spotify")
    client = MagicMock()

    res = sp.build_playlist(dry_run=True, client=client, conn=c)

    assert res.dry_run and res.resolved == 1
    client.create_playlist.assert_not_called()
    client.add_playlist_tracks.assert_not_called()


def test_build_playlist_creates_and_adds(tmp_path):
    c = _conn(tmp_path)
    _add(c, "A", "B", url="https://open.spotify.com/track/XYZ", kind="spotify")
    client = MagicMock()
    client.find_playlist.return_value = None
    client.create_playlist.return_value = "PL1"
    client.add_playlist_tracks.return_value = 1

    res = sp.build_playlist(client=client, conn=c)

    assert res.playlist_id == "PL1" and res.added == 1
    client.add_playlist_tracks.assert_called_once_with("PL1", ["spotify:track:XYZ"])


def test_build_playlist_reuses_existing_and_skips_duplicates(tmp_path):
    """Re-running must not duplicate tracks already in the playlist."""
    c = _conn(tmp_path)
    _add(c, "A", "B", url="https://open.spotify.com/track/OLD", kind="spotify")
    _add(c, "C", "D", url="https://open.spotify.com/track/NEW", kind="spotify")
    client = MagicMock()
    client.find_playlist.return_value = "PL1"
    client.playlist_track_uris.return_value = ["spotify:track:OLD"]
    client.add_playlist_tracks.return_value = 1

    res = sp.build_playlist(client=client, conn=c)

    client.create_playlist.assert_not_called()
    client.add_playlist_tracks.assert_called_once_with("PL1", ["spotify:track:NEW"])
    assert res.already_present == 1


def test_build_playlist_dedupes_same_uri_twice(tmp_path):
    """Two track rows pointing at one Spotify track must add it once."""
    c = _conn(tmp_path)
    _add(c, "A", "Song", url="https://open.spotify.com/track/SAME", kind="spotify")
    _add(c, "A", "Song - Remastered",
         url="https://open.spotify.com/track/SAME", kind="spotify")
    client = MagicMock()
    client.find_playlist.return_value = None
    client.create_playlist.return_value = "PL1"

    res = sp.build_playlist(client=client, conn=c)

    assert res.resolved == 1
    client.add_playlist_tracks.assert_called_once_with("PL1", ["spotify:track:SAME"])


def test_build_playlist_reports_unresolved(tmp_path):
    c = _conn(tmp_path)
    _add(c, "Obscure", "Track", url="https://youtu.be/abc", kind="youtube")
    client = MagicMock()
    client.search_track.return_value = None
    client.find_playlist.return_value = None
    client.create_playlist.return_value = "PL1"

    res = sp.build_playlist(client=client, conn=c)

    assert [(u.artist, u.title) for u in res.unresolved] == [("Obscure", "Track")]
    assert res.resolved == 0


# --- search normalization --------------------------------------------------

def test_primary_artist_trims_credited_list():
    assert sp.primary_artist("Motorpsycho, Bent Sæther, Hans Magnus Ryan") == "Motorpsycho"
    assert sp.primary_artist("Inpatient, Ren & Chris Webby") == "Inpatient"
    assert sp.primary_artist("Nathan Dawe x Ella Henderson") == "Nathan Dawe"


def test_primary_artist_leaves_plain_names_alone():
    assert sp.primary_artist("The Slits") == "The Slits"
    assert sp.primary_artist("Gotye") == "Gotye"


def test_search_title_drops_feat_suffix():
    """Spotify indexes "Instant Crush", not the ft. credit."""
    assert sp.search_title("Instant Crush ft. Julian Casablancas") == "Instant Crush"
    assert sp.search_title("Morning Sunset (Feat. @Mounika.)") == "Morning Sunset"


def test_search_title_keeps_ordinary_titles():
    assert sp.search_title("Somebody That I Used to Know") == "Somebody That I Used to Know"


def test_track_matches_accepts_real_match():
    assert sp.track_matches("Linkin Park", "Faint", ["Linkin Park"], "Faint")


def test_track_matches_rejects_same_title_other_artist():
    """A loose query must not silently return a different artist's song."""
    assert not sp.track_matches("Seven Hells", "Life Sentence", ["J. Cole"],
                                "Life Sentence")
    assert not sp.track_matches("Dead Mall", "RATS", ["Reese Lansangan"],
                                "Mall Rats")


def test_track_matches_rejects_unrelated_title():
    assert not sp.track_matches("Gotye", "Somebody That I Used to Know",
                                ["Gotye"], "Hearts A Mess")


# --- rate limiting ---------------------------------------------------------

def test_resolve_uris_stops_on_rate_limit_instead_of_marking_missing():
    """A 429 must not be recorded as "no such track" for every remaining song."""
    from karaoke.spotify_client import SpotifyRateLimited
    client = MagicMock()
    client.search_track.side_effect = SpotifyRateLimited(3600)
    cands = [sp.Candidate("A", "B"), sp.Candidate("C", "D")]

    completed = sp.resolve_uris(cands, client)

    assert completed is False
    assert client.search_track.call_count == 1      # stopped, did not keep asking
    assert cands[1].resolved_by == ""               # not falsely "unresolved"


def test_build_playlist_flags_incomplete_run(tmp_path):
    from karaoke.spotify_client import SpotifyRateLimited
    c = _conn(tmp_path)
    _add(c, "Obscure", "Track", url="https://youtu.be/abc", kind="youtube")
    client = MagicMock()
    client.search_track.side_effect = SpotifyRateLimited(3600)

    res = sp.build_playlist(dry_run=True, client=client, conn=c)

    assert res.completed is False


def test_resolve_uris_caches_searched_uri_as_source(tmp_path):
    """A searched-out URI is persisted so the next run spends no quota."""
    c = _conn(tmp_path)
    _add(c, "YT Only", "Song", url="https://youtu.be/abc", kind="youtube")
    client = MagicMock()
    client.search_track.return_value = "spotify:track:FOUND"

    sp.resolve_uris(sp.karaoke_tracks(c), client, conn=c)

    again = [t for t in sp.karaoke_tracks(c) if t.artist == "YT Only"]
    assert again[0].uri == "spotify:track:FOUND"
    assert again[0].resolved_by == "stored"
