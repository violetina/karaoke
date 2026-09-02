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


@patch("karaoke.backfill_runner.fetch_lrclib")
def test_find_lyrics_prefers_lrclib(mock_lrclib):
    from karaoke.lyrics import Lyrics
    mock_lrclib.return_value = Lyrics(plain="line one\nline two", source="lrclib")
    text = backfill_runner._find_lyrics_text("Artist", "Song")
    assert text == "line one\nline two"


@patch("karaoke.backfill_runner.web.fetch_genius_lyrics")
@patch("karaoke.backfill_runner.web.search")
@patch("karaoke.backfill_runner.fetch_lrclib")
def test_find_lyrics_falls_back_to_genius(mock_lrclib, mock_search, mock_genius):
    from karaoke.lyrics import Lyrics
    mock_lrclib.return_value = Lyrics()  # LRCLIB miss
    mock_search.return_value = [{"url": "https://genius.com/x-lyrics", "title": "x"}]
    mock_genius.return_value = "genius line one\ngenius line two"
    text = backfill_runner._find_lyrics_text("Artist", "Song")
    assert text == "genius line one\ngenius line two"
    mock_genius.assert_called_once()


@patch("karaoke.backfill_runner.web.search", return_value=[])
@patch("karaoke.backfill_runner.fetch_lrclib")
def test_find_lyrics_returns_empty_when_all_miss(mock_lrclib, _mock_search):
    from karaoke.lyrics import Lyrics
    mock_lrclib.return_value = Lyrics()
    assert backfill_runner._find_lyrics_text("Artist", "Song") == ""


@patch("karaoke.backfill_runner._find_lyrics_text", return_value="")
def test_process_gap_raises_without_lyrics(mock_find):
    import pytest
    with pytest.raises(RuntimeError, match="No lyrics found"):
        backfill_runner._process_gap(1, "A", "B")
