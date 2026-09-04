"""Tests for web search/extraction and the Genius lyrics parser (offline)."""
from unittest.mock import MagicMock, patch

from karaoke import web


_GENIUS_HTML = """
<html><body>
<div data-lyrics-container="true">
289 ContributorsTranslations Creep LyricsSome description… Read More [Verse 1]
When you were here before<br>Couldn't look you in the eye<br>
[Chorus]<br>But I'm a creep<br>I'm a weirdo
</div>
</body></html>
"""


def test_fetch_genius_lyrics_parses_container():
    resp = MagicMock()
    resp.text = _GENIUS_HTML
    with patch("karaoke.web.requests.get", return_value=resp):
        text = web.fetch_genius_lyrics("https://genius.com/x-lyrics")
    lines = text.splitlines()
    assert lines[0] == "When you were here before"
    # section markers and the metadata blob are dropped
    assert "[Verse 1]" not in text
    assert "Contributors" not in text
    assert "But I'm a creep" in text


def test_fetch_genius_lyrics_no_container_returns_empty():
    resp = MagicMock()
    resp.text = "<html><body><p>no lyrics here</p></body></html>"
    with patch("karaoke.web.requests.get", return_value=resp):
        assert web.fetch_genius_lyrics("https://genius.com/x") == ""


def test_fetch_genius_lyrics_network_error_returns_empty():
    with patch("karaoke.web.requests.get", side_effect=Exception("boom")):
        assert web.fetch_genius_lyrics("https://genius.com/x") == ""


def test_search_maps_href_to_url():
    fake_ddgs = MagicMock()
    fake_ddgs.__enter__.return_value.text.return_value = [
        {"title": "T1", "href": "https://a.com", "body": "b"},
        {"title": "T2", "href": "https://b.com", "body": "b"},
    ]
    with patch("karaoke.web.DDGS", return_value=fake_ddgs):
        results = web.search("query", limit=2)
    assert results == [
        {"url": "https://a.com", "title": "T1"},
        {"url": "https://b.com", "title": "T2"},
    ]


def test_search_skips_results_without_url():
    fake_ddgs = MagicMock()
    fake_ddgs.__enter__.return_value.text.return_value = [
        {"title": "no-url"},
        {"title": "T", "href": "https://ok.com"},
    ]
    with patch("karaoke.web.DDGS", return_value=fake_ddgs):
        results = web.search("q")
    assert results == [{"url": "https://ok.com", "title": "T"}]


def test_search_returns_empty_on_ddgs_exception():
    """A failing/empty DDGS search must not abort the caller's pipeline."""
    with patch("karaoke.web.DDGS", side_effect=RuntimeError("No results found.")):
        assert web.search("anything") == []


def test_search_returns_empty_when_ddgs_text_raises():
    ddgs = MagicMock()
    ddgs.__enter__.return_value.text.side_effect = RuntimeError("No results found.")
    with patch("karaoke.web.DDGS", return_value=ddgs):
        assert web.search("anything") == []
