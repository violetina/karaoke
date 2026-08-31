"""Web search and extraction."""
from __future__ import annotations
import requests
from bs4 import BeautifulSoup

def search(query: str, limit: int = 5) -> list[dict]:
    """Search the web and return a list of results."""
    # This is a placeholder implementation.
    # A real implementation would use a search engine API.
    return [{'url': 'https://genius.com/Red-hot-chili-peppers-give-it-away-lyrics', 'title': 'Give It Away Lyrics'}]

def extract(urls: list[str]) -> list[dict]:
    """Extract content from a list of URLs."""
    results = []
    for url in urls:
        try:
            response = requests.get(url)
            soup = BeautifulSoup(response.text, 'html.parser')
            results.append({'url': url, 'content': soup.get_text()})
        except Exception:
            results.append({'url': url, 'content': ''})
    return results
