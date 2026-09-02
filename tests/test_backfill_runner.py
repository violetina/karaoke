"""Tests for the backfill runner."""
from unittest.mock import patch, MagicMock
from karaoke import backfill_runner

@patch("karaoke.backfill_runner.localcache.connect")
def test_run_processes_gaps(mock_connect):
    mock_conn = MagicMock()
    mock_connect.return_value = mock_conn
    mock_conn.__enter__.return_value.cursor.return_value.fetchall.return_value = [
        {'gap_id': 1, 'artist': 'A', 'title': 'B'}
    ]

    with patch("karaoke.backfill_runner._process_gap") as mock_process_gap:
        backfill_runner.run()

    assert mock_process_gap.call_count == 1
    mock_process_gap.assert_called_with(1, 'A', 'B')
