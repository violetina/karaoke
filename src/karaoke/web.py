"""Web search and extraction."""
from __future__ import annotations
import requests
from bs4 import BeautifulSoup

from .logger import log

try:  # the package was renamed duckduckgo_search -> ddgs
    from ddgs import DDGS
except ImportError:  # pragma: no cover - fallback for old installs
    from duckduckgo_search import DDGS  # type: ignore

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def search(query: str, limit: int = 5) -> list[dict]:
    """Search the web and return a list of {'url', 'title'} results.

    Best-effort: DDGS raises (``DDGSException: No results found``, rate limits,
    transport errors) where callers only want "nothing found". An empty list is
    returned instead so a dead search never aborts the caller's pipeline.
    """
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=limit))
    except Exception as exc:
        log.debug("web.search failed for %r: %s", query, exc)
        return []
    out: list[dict] = []
    for r in results:
        url = r.get("href") or r.get("url") or r.get("link")
        if url:
            out.append({"url": url, "title": r.get("title", "")})
    return out


def extract(urls: list[str]) -> list[dict]:
    """Extract readable text from a list of URLs."""
    results = []
    for url in urls:
        try:
            response = requests.get(
                url, timeout=15, headers={"User-Agent": _UA}
            )
            soup = BeautifulSoup(response.text, "html.parser")
            results.append({"url": url, "content": soup.get_text("\n")})
        except Exception:
            results.append({"url": url, "content": ""})
    return results


def fetch_genius_lyrics(url: str) -> str:
    """Fetch and parse plain lyrics from a Genius song URL.

    Genius renders lyrics inside one or more ``div[data-lyrics-container]``
    blocks with ``<br>`` line breaks; the old ``## ... Lyrics`` markdown regex
    never matched the plain-text extraction. This parses those containers
    directly. Returns "" if the page can't be fetched or has no lyrics.
    """
    try:
        response = requests.get(url, timeout=15, headers={"User-Agent": _UA})
    except Exception:
        return ""
    soup = BeautifulSoup(response.text, "html.parser")
    containers = soup.select("div[data-lyrics-container='true']")
    if not containers:
        # Older layout fallback: a single .lyrics block.
        legacy = soup.select_one("div.lyrics")
        containers = [legacy] if legacy else []
    lines: list[str] = []
    for container in containers:
        for br in container.find_all("br"):
            br.replace_with("\n")
        text = container.get_text()
        # The first container often begins with a metadata blob (contributor
        # counts, translations, a "Read More" description) fused onto the first
        # section marker, e.g. "...Read More [Verse 1]". Cut everything before
        # the first section marker so only lyrics remain.
        first_marker = text.find("[")
        if first_marker > 0 and "Read More" in text[:first_marker + 1]:
            text = text[first_marker:]
        for raw in text.splitlines():
            line = raw.strip()
            # Skip Genius section markers like "[Verse 1]", "[Chorus]".
            if not line or (line.startswith("[") and line.endswith("]")):
                continue
            lines.append(line)
    return "\n".join(lines).strip()
