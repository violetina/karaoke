"""Artist navigation must preserve identity, not choose a partial-name hit."""
import asyncio
from unittest.mock import AsyncMock, patch

from karaoke import dashboard_api as api


def test_jimmy_page_never_accepts_jimmy_rosenberg():
    wrong = {'name': 'Jimmy Rosenberg', 'wikidata': 'Q927242'}
    async def enrich(doc):
        return doc
    with patch.object(api.os_client, 'search', return_value={'hits': {'hits': [{'_source': wrong}]}}), patch.object(
        api, 'enrich_artist_profile', side_effect=enrich,
    ), patch('karaoke.localcache.connect') as connect:
        connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = None
        result = asyncio.run(api.get_artist_info('Jimmy Page'))
    assert result['name'] == 'Jimmy Page'
    assert result['wikidata'] is None


def test_qid_lookup_uses_exact_identifier():
    with patch.object(api.os_client, 'search', return_value={'hits': {'hits': []}}) as search, patch.object(
        api, 'enrich_artist_profile', new=AsyncMock(return_value={'name': 'Jimmy Page', 'wikidata': 'Q165467'}),
    ), patch('karaoke.localcache.connect') as connect:
        connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = None
        result = asyncio.run(api.get_artist_info('Q165467'))
    assert search.call_args.kwargs['body']['query'] == {'term': {'wikidata': 'Q165467'}}
    assert result['name'] == 'Jimmy Page'


def test_graph_member_preserves_relation_qid():
    artist = {'name': 'Led Zeppelin', 'wikidata': 'Q2331',
              'member_entities': [{'name': 'Jimmy Page', 'wikidata': 'Q165467'}]}
    with patch.object(api, 'get_artist_info', new=AsyncMock(return_value=artist)):
        graph = asyncio.run(api.get_artist_relations_graph('Q2331'))
    member = next(n['data'] for n in graph['nodes'] if n['data']['type'] == 'member')
    assert member['label'] == 'Jimmy Page'
    assert member['wikidata'] == 'Q165467'
    assert member['id'] == 'member_Q165467'
