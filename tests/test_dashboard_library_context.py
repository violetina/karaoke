"""Read-only dashboard song metadata lookup."""
from unittest.mock import MagicMock, patch

from karaoke.dashboard_api import (
    _dj_chat_system_prompt,
    embedded_mood,
    song_library_context,
)


def test_dj_chat_prompt_forbids_suggesting_current_song():
    """The AI DJ booth must never offer the song already on stage as 'next'.

    The local model parrots the current track back on a 'suggest next song'
    prompt unless the system prompt explicitly rules it out.
    """
    prompt = _dj_chat_system_prompt("Method Man", "Built For This")
    assert "ALREADY PLAYING" in prompt
    assert "never suggest" in prompt.lower()
    assert "must be a DIFFERENT track" in prompt
    # The current track identity is still named so the model knows what to avoid.
    assert "Built For This" in prompt
    assert "Method Man" in prompt


def test_embedded_mood_reuses_live_canvas_and_in_square_chords():
    html = embedded_mood()
    assert 'requestAnimationFrame(renderLoop)' in html
    assert 'id="chord-overlay"' in html
    assert "new EventSource('/api/stage/stream')" in html
    assert '#hud-top, #info-box { display: none !important; }' in html
    assert 'async function toggleMic() { return;' in html
    assert "new EventSource('/api/captions/stream')" not in html


def test_unknown_song_has_no_invented_stats():
    with patch('karaoke.localcache.connect') as connect:
        result = song_library_context('', '')
    assert result == {'track_id': None, 'play_count': None, 'playlists': []}
    connect.return_value.__enter__.return_value.execute.assert_not_called()


def test_library_context_returns_count_and_playlist_names():
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = {'play_count': 12}
    conn.execute.return_value.fetchall.return_value = [
        {'playlist_id': 'party', 'name': 'Party'},
    ]
    with patch('karaoke.localcache.connect') as connect, patch(
        'karaoke.localcache.find_track_id', return_value=42,
    ):
        connect.return_value.__enter__.return_value = conn
        result = song_library_context('Pixies', 'Gouge Away')
    assert result == {
        'track_id': 42, 'play_count': 12,
        'playlists': [{'playlist_id': 'party', 'name': 'Party'}],
    }
    assert all(call.args[0].lstrip().startswith('SELECT') for call in conn.execute.call_args_list)
