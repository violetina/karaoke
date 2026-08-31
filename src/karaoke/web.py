"""Web search and extraction."""
from __future__ import annotations
import requests
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS

def search(query: str, limit: int = 5) -> list[dict]:
    """Search the web and return a list of results."""
    with DDGS() as ddgs:
        results = list(ddgs.text(query, max_results=limit))
    return [{'url': r['href'], 'title': r['title']} for r in results]

def extract(urls: list[str]) -> list[dict]:
    """Extract content from a list of URLs."""
    results = []
    for url in urls:
        try:
            response = requests.get(url, timeout=15)
            soup = BeautifulSoup(response.text, 'html.parser')
            results.append({'url': url, 'content': soup.get_text()})
        except Exception:
            results.append({'url': url, 'content': ''})
    return results
