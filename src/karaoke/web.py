"""Web search and extraction."""
from __future__ import annotations
import requests
import subprocess
from bs4 import BeautifulSoup

def search(query: str, limit: int = 5) -> list[dict]:
    """Search the web and return a list of results."""
    try:
        # Use lynx to get a clean text-based dump of search results
        search_url = f"https://duckduckgo.com/html/?q={query.replace(' ', '+')}"
        proc = subprocess.run(
            ["lynx", "-dump", "-listonly", search_url],
            capture_output=True, text=True, timeout=15, check=True
        )
        results = []
        for line in proc.stdout.splitlines():
            if "duckduckgo.com" in line or not line.strip():
                continue
            parts = line.strip().split()
            if len(parts) > 1 and parts[1].startswith("http"):
                url = parts[1]
                title = " ".join(parts[2:])
                results.append({'url': url, 'title': title})
                if len(results) >= limit:
                    break
        return results
    except (subprocess.SubprocessError, FileNotFoundError):
        return []

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
