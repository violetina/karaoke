"""Tests for the --lyrics-file feature."""
from unittest.mock import patch, MagicMock

from karaoke.cli import karaoke_main


# play_offset_synced is stubbed as well as play: alignment now yields a real
# timeline, so the CLI would otherwise render playback against a MagicMock ref.
@patch("karaoke.player.play_offset_synced")
@patch("karaoke.player.play")
@patch("karaoke.cli.from_file")
def test_lyrics_file_passed_to_transcriber(mock_from_file, mock_play,
                                           mock_play_synced, tmp_path):
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

    # With lyrics supplied, Whisper is asked for word TIMINGS rather than an
    # LRC: the real words are laid onto its rhythm instead of being replaced by
    # its transcription. The text is still passed as an initial_prompt bias.
    with patch("karaoke.whisper_sync.transcribe_to_words") as mock_transcribe:
        mock_transcribe.return_value = []
        karaoke_main([
            "--file", str(audio_file),
            "--force-transcribe",
            "--lyrics-file", str(lyrics_file),
        ])

    assert mock_transcribe.call_count == 1
    mock_transcribe.assert_called_with(str(audio_file), text="hello world")
