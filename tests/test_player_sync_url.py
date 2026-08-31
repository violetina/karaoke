"""Tests for the URL-based player sync."""
from unittest.mock import patch, MagicMock

from karaoke.player_sync import play_synced_to_player


@patch("karaoke.player_sync.localcache.connect")
@patch("karaoke.playerctl.current_metadata")
@patch("subprocess.run")
def test_url_based_sync_happy_path(mock_run, mock_current_metadata, mock_connect):
    mock_current_metadata.return_value = MagicMock(
        url="https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    )
    mock_conn = MagicMock()
    mock_connect.return_value = mock_conn
    mock_conn.__enter__.return_value.cursor.return_value.fetchone.return_value = {
        "track_id": 1, "artist": "Rick Astley", "title": "Never Gonna Give You Up"
    }
    mock_conn.__enter__.return_value.cursor.return_value.fetchall.return_value = [
        {"synced_lyrics": "[00:01.00] We're no strangers to love"}
    ]
    mock_run.side_effect = [
        MagicMock(stdout="1.23"),
        KeyboardInterrupt,
    ]

    with patch("rich.live.Live"):
        play_synced_to_player()
    
    assert mock_run.call_count > 0
