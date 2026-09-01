"""Tests for play_spotify_loop."""
from unittest.mock import MagicMock

from karaoke.lyrics import Lyrics
from karaoke.player import play_spotify_loop
from karaoke.spotify_client import Playback


def test_play_spotify_loop_fetches_synced_lyrics(monkeypatch):
    mock_pb = Playback(
        is_playing=True,
        progress_ms=5000,
        duration_ms=180000,
        artist="The Slits",
        title="I Heard It Through The Grapevine",
        track_id="track123",
    )

    mock_sp = MagicMock()
    mock_sp.current_playback.side_effect = [mock_pb, KeyboardInterrupt()]

    monkeypatch.setattr("karaoke.spotify_client.SpotifyClient", lambda: mock_sp)

    fetched_refs = []

    def mock_get_synced(ref, *, use_cache=True, stats_mode=None):
        fetched_refs.append(ref)
        return Lyrics(
            synced_raw="[00:05.00] I heard it through the grapevine",
            lines=[(5.0, "I heard it through the grapevine")],
            source="lrclib",
        )

    monkeypatch.setattr("karaoke.player.get_synced", mock_get_synced)
    monkeypatch.setattr("karaoke.player._sync_one_spotify_track", lambda *args, **kwargs: None)

    play_spotify_loop(poll_interval=0.01)

    assert len(fetched_refs) == 1
    assert fetched_refs[0].artist == "The Slits"
    assert fetched_refs[0].title == "I Heard It Through The Grapevine"
    assert fetched_refs[0].duration == 180.0
    assert fetched_refs[0].source == "spotify"
