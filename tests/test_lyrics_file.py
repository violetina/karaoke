"""Tests for the --lyrics-file feature."""
from unittest.mock import patch, MagicMock

from karaoke.cli import karaoke_main


@patch("karaoke.player.play")
@patch("karaoke.cli.from_file")
def test_lyrics_file_passed_to_transcriber(mock_from_file, mock_play, tmp_path):
    lyrics_file = tmp_path / "lyrics.txt"
    lyrics_file.write_text("hello world")

    audio_file = tmp_path / "audio.mp3"
    audio_file.write_text("dummy audio")

    mock_from_file.return_value = MagicMock(
        artist="Test Artist",
        title="Test Title",
        album="",
        duration=0,
        path=str(audio_file),
        source="file",
    )

    with patch("karaoke.whisper_sync.transcribe_to_lrc") as mock_transcribe:
        karaoke_main([
            "--file", str(audio_file),
            "--force-transcribe",
            "--lyrics-file", str(lyrics_file),
        ])

    assert mock_transcribe.call_count == 1
    mock_transcribe.assert_called_with(str(audio_file), text="hello world")
