"""A polite Wikidata client: the entity API and the SPARQL endpoint.

Wikidata asks clients to identify themselves in the User-Agent, to send
``maxlag`` so they back off when the database replicas lag, and to honour
``Retry-After`` on 429. Requests are sequential; batches keep their number
small (50 entities per entity request, a few hundred MBIDs per query).

Like :mod:`karaoke.musicbrainz`, failures come back as None so callers can
tell "Wikidata was unreachable" from "Wikidata has nothing".
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable, Optional

from .logger import log

API_URL = "https://www.wikidata.org/w/api.php"
SPARQL_URL = "https://query.wikidata.org/sparql"
USER_AGENT = "KaraokeArtistIdentity/1.0 (https://github.com/violetina/karaoke)"
#: The entity API takes at most 50 ids per request.
ENTITY_BATCH = 50
#: MBIDs per SPARQL query; a VALUES list this long stays well inside the
#: endpoint's 60-second limit.
SPARQL_BATCH = 200
#: Back off when replicas lag more than this many seconds (Wikidata's advice).
MAXLAG = 5
RETRY_WAITS_S = (5.0, 15.0, 30.0)
TIMEOUT_S = 60.0
#: Languages for labels: English first, Dutch as the fallback.
LANGUAGES = ("en", "nl")


def _request(req: urllib.request.Request, *, sleep=time.sleep) -> Optional[dict[str, Any]]:
    for wait in (*RETRY_WAITS_S, None):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 503) and wait is not None:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                sleep(float(retry_after) if retry_after and retry_after.isdigit() else wait)
                continue
            log.warning("Wikidata request failed: %s", exc)
            return None
        except Exception as exc:
            log.warning("Wikidata request failed: %s", exc)
            return None
        # The entity API reports lag as an error in a 200 response.
        if (data.get("error") or {}).get("code") == "maxlag" and wait is not None:
            sleep(wait)
            continue
        if data.get("error"):
            log.warning("Wikidata error: %s", data["error"].get("info"))
            return None
        return data
    return None


def _batches(items: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def entities(ids: Iterable[str], *, props: str = "claims|labels|descriptions|sitelinks",
             sleep=time.sleep) -> Optional[dict[str, dict[str, Any]]]:
    """Entities by QID, or None if any batch could not be fetched."""
    wanted = list(dict.fromkeys(i for i in ids if i))
    out: dict[str, dict[str, Any]] = {}
    for batch in _batches(wanted, ENTITY_BATCH):
        query = urllib.parse.urlencode({
            "action": "wbgetentities", "ids": "|".join(batch), "props": props,
            "languages": "|".join(LANGUAGES), "sitefilter": "enwiki|nlwiki",
            "format": "json", "maxlag": str(MAXLAG)})
        data = _request(urllib.request.Request(f"{API_URL}?{query}", headers={
            "User-Agent": USER_AGENT, "Accept": "application/json"}), sleep=sleep)
        if data is None:
            return None
        for qid, ent in (data.get("entities") or {}).items():
            if "missing" not in ent:
                out[qid] = ent
    return out


def labels(ids: Iterable[str], *, sleep=time.sleep) -> Optional[dict[str, str]]:
    """The English (else Dutch) label of each QID, or None on failure."""
    ents = entities(ids, props="labels", sleep=sleep)
    if ents is None:
        return None
    return {qid: label_of(ent) for qid, ent in ents.items()}


def label_of(entity: dict[str, Any]) -> str:
    names = entity.get("labels") or {}
    for lang in LANGUAGES:
        if lang in names:
            return names[lang]["value"]
    return next((v["value"] for v in names.values()), "")


def qids_for_mbids(mbids: Iterable[str], *, sleep=time.sleep) -> Optional[dict[str, str]]:
    """Wikidata items carrying these MusicBrainz artist IDs (P434), or None."""
    wanted = list(dict.fromkeys(m for m in mbids if m))
    out: dict[str, str] = {}
    for batch in _batches(wanted, SPARQL_BATCH):
        values = " ".join(json.dumps(m) for m in batch)
        query = f"SELECT ?mbid ?item WHERE {{ VALUES ?mbid {{ {values} }} ?item wdt:P434 ?mbid }}"
        body = urllib.parse.urlencode({"query": query}).encode()
        data = _request(urllib.request.Request(SPARQL_URL, data=body, headers={
            "User-Agent": USER_AGENT, "Accept": "application/sparql-results+json",
            "Content-Type": "application/x-www-form-urlencoded"}), sleep=sleep)
        if data is None:
            return None
        for b in data.get("results", {}).get("bindings", []):
            out.setdefault(b["mbid"]["value"], b["item"]["value"].rsplit("/", 1)[-1])
    return out
