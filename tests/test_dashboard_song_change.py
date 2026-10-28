"""Song-change artist resolution without playback side effects."""
import asyncio
from unittest.mock import AsyncMock, patch

from karaoke import dashboard_api as api


def test_known_artist_skips_resolution():
    with patch('karaoke.localcache.connect') as connect, patch(
        'karaoke.localcache.find_track_id', return_value=42,
    ), patch('karaoke.postprocess_worker.run_artist_logic') as resolve, patch.object(
        api, 'get_artist_info', new=AsyncMock(return_value={'name': 'Pixies'}),
    ):
        connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = {'exists': 1}
        result = asyncio.run(api.refresh_playing_artist(api.SongActionPayload(artist='Pixies', title='Debaser')))
    assert result['processing_status'] == 'known'
    assert result['artist']['name'] == 'Pixies'
    resolve.assert_not_called()


def test_new_artist_runs_existing_pipeline():
    with patch('karaoke.localcache.connect') as connect, patch(
        'karaoke.localcache.find_track_id', return_value=None,
    ), patch('karaoke.localcache.add_track_source', return_value=123) as create, patch(
        'karaoke.postprocess_worker.run_artist_logic', return_value='resolved',
    ) as resolve, patch.object(api, 'get_artist_info', new=AsyncMock(return_value={'name': 'New Artist'})):
        conn = connect.return_value.__enter__.return_value
        conn.execute.return_value.fetchone.return_value = None
        result = asyncio.run(api.refresh_playing_artist(api.SongActionPayload(artist='New Artist', title='New Song')))
    create.assert_called_once()
    resolve.assert_called_once_with(123, conn)
    assert result['processing_status'] == 'resolved'
