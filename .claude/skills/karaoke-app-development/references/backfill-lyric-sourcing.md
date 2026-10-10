# Backfill lyric sourcing — debugging notes

The automated backfill (`karaoke-backfill --run`, driven by `backfill_runner.py`)
was completely non-functional despite looking plausible. Two independent silent
bugs meant a gap could never be filled. Both are easy to reintroduce.

## Bug 1: `web.search` used the renamed/dead `duckduckgo_search`

`duckduckgo_search` was renamed to `ddgs`. The old package still pip-installs and
imports, but `DDGS().text(...)` now returns an **empty list** (emitting only a
`RuntimeWarning: This package has been renamed to ddgs`). So `web.search` always
returned 0 results and the Genius branch was never entered.

Fix in `src/karaoke/web.py`:

```python
try:  # the package was renamed duckduckgo_search -> ddgs
    from ddgs import DDGS
except ImportError:  # pragma: no cover
    from duckduckgo_search import DDGS  # type: ignore

def search(query, limit=5):
    with DDGS() as ddgs:
        results = list(ddgs.text(query, max_results=limit))
    out = []
    for r in results:
        url = r.get("href") or r.get("url") or r.get("link")  # ddgs uses 'href'
        if url:
            out.append({"url": url, "title": r.get("title", "")})
    return out
```

Note the result key is `href`, not `url`.

## Bug 2: Genius extractor matched markdown that never existed

The old code did:

```python
page = web.extract([genius_url])[0]          # returns BeautifulSoup.get_text()
match = re.search(r'## .*? Lyrics(.*?)(##|More on Genius)', page['content'], re.DOTALL)
```

`get_text()` produces **plain text with no `##` markdown headings**, so the regex
never matched and lyrics were never captured. Genius renders lyrics inside one or
more `div[data-lyrics-container='true']` blocks. Parse those directly:

```python
def fetch_genius_lyrics(url):
    resp = requests.get(url, timeout=15, headers={"User-Agent": _UA})  # UA or Genius blocks
    soup = BeautifulSoup(resp.text, "html.parser")
    containers = soup.select("div[data-lyrics-container='true']") \
                 or ([soup.select_one("div.lyrics")] if soup.select_one("div.lyrics") else [])
    lines = []
    for c in containers:
        for br in c.find_all("br"):
            br.replace_with("\n")
        text = c.get_text()
        # first container fuses a contributor/"Read More" blob onto the first [section] tag
        first = text.find("[")
        if first > 0 and "Read More" in text[:first + 1]:
            text = text[first:]
        for raw in text.splitlines():
            line = raw.strip()
            if not line or (line.startswith("[") and line.endswith("]")):  # skip [Verse]/[Chorus]
                continue
            lines.append(line)
    return "\n".join(lines).strip()
```

## Bug 3 (design): download audio only AFTER lyrics are found

`_process_gap` used to download the YouTube audio first, then look for lyrics.
Reorder to find lyrics first (cheap) and bail early if none:

1. LRCLIB first — it is the primary, reliable, no-scrape source. Try both the raw
   title and `clean_title(title)` (strips "- Remastered 2012", "(Live)", etc.).
   If LRCLIB returns only synced, strip timestamps with `parse_lrc` to get plain.
2. Genius fallback via `web.search("<artist> <title> lyrics genius")` +
   `fetch_genius_lyrics` on the first `genius.com` hit.
3. Only if lyrics found: `youtube.search` -> `youtube.download` -> `get_synced(ref,
   force_transcribe=True, lyrics_file=...)` to align plain text to the audio.

Instrumentals (e.g. "Mellon Collie And The Infinite Sadness - Remastered 2012")
correctly resolve to empty from both sources and the runner raises
`RuntimeError("No lyrics found")` — that is expected, not a bug.

## Testing

All three fixes are covered offline in `tests/test_web.py` (mock `DDGS` and
`requests.get`) and `tests/test_backfill_runner.py` (mock `fetch_lrclib` /
`web.search` / `web.fetch_genius_lyrics`). Verified live: Nirvana and Smashing
Pumpkins ("1979") resolve via LRCLIB.
