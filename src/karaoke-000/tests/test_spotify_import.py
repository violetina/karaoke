"""Tests for Spotify saved-tracks -> TrackTags conversion."""
from karaoke.spotify_import import track_to_tags, iter_tracks, spotify_id_to_doc_id


_TRACK = {
    "id": "3q8CTM9sovOh3Ox4d2cNf9",
    "uri": "spotify:track:3q8CTM9sovOh3Ox4d2cNf9",
    "name": "Charlotte",
    "duration_ms": 123200,
    "artists": [{"name": "The Young Gods"}],
    "album": {"name": "L'Eau Rouge/Red Water", "release_date": "1989-08-28"},
}


def test_track_to_tags_basic():
    t = track_to_tags(_TRACK)
    assert t.title == "Charlotte"
    assert t.artist == "The Young Gods"
    assert t.album == "L'Eau Rouge/Red Water"
    assert t.year == 1989
    assert abs((t.duration or 0) - 123.2) < 0.01
    assert t.path == "spotify:track:3q8CTM9sovOh3Ox4d2cNf9"


def test_track_to_tags_multiple_artists():
    tr = dict(_TRACK, artists=[{"name": "A"}, {"name": "B"}])
    assert track_to_tags(tr).artist == "A, B"


def test_track_to_tags_missing_year():
    tr = dict(_TRACK, album={"name": "X", "release_date": ""})
    assert track_to_tags(tr).year is None


def test_iter_tracks_unwraps_saved_items():
    items = [{"added_at": "x", "track": _TRACK}]
    got = list(iter_tracks(items))
    assert len(got) == 1 and got[0]["id"] == _TRACK["id"]


def test_iter_tracks_accepts_bare_tracks():
    got = list(iter_tracks([_TRACK]))
    assert len(got) == 1


def test_iter_tracks_skips_idless():
    assert list(iter_tracks([{"track": {"name": "no id"}}])) == []


def test_doc_id_namespaced_and_stable():
    a = spotify_id_to_doc_id("abc")
    assert a.startswith("spotify:")
    assert a == spotify_id_to_doc_id("abc")
    assert a != spotify_id_to_doc_id("def")
