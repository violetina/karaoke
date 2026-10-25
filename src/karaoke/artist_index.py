"""The ``karaoke-artists`` OpenSearch index: one searchable document per artist.

Step 4 of docs/artist_metadata_plan.md. Postgres stays the source of truth
(:mod:`karaoke.artist_identity`, :mod:`karaoke.artist_wikidata`); this index
is rebuilt from it and serves what Postgres is poor at:

- **finding an artist as you type, forgiving spelling**: name and every
  alias in a ``search_as_you_type`` field, accent-folded, so "bonnie prince",
  "desole" and "einsturzende" all land;
- **facets**: broad genre families, main genre, country, kind (person or
  group), record label, and the decade begun -- born for a person, formed for
  a group, so it is not "active in" (Aphex Twin: 1970s);
- **links**: band members and bands, influences, and the library's own
  collaborations (artists credited together on a track), each with the MBID
  of the other artist when it is in the library too.

A rebuild writes a fresh index and moves the ``karaoke-artists`` alias onto it
in one step, so a search never sees a half-built index; the old one is
deleted after.
"""
from __future__ import annotations

import time
from collections import Counter, defaultdict
from typing import Any, Iterable, Optional

from .logger import log

ARTIST_INDEX = "karaoke-artists"

_FOLDED = {"type": "custom", "tokenizer": "standard",
           "filter": ["lowercase", "asciifolding"]}
_NAME = {"type": "text", "analyzer": "folded", "fields": {"raw": {"type": "keyword"}}}

MAPPING = {
    "settings": {
        "index": {"number_of_replicas": 0},
        "analysis": {"analyzer": {"folded": _FOLDED}},
    },
    "mappings": {
        "properties": {
            "mbid": {"type": "keyword"},
            "name": _NAME,
            "sort_name": {"type": "keyword"},
            "aliases": _NAME,
            # Name and aliases for as-you-type search.
            "suggest": {"type": "search_as_you_type", "analyzer": "folded"},
            "disambiguation": {"type": "text"},
            "description": {"type": "text"},
            "kind": {"type": "keyword"},
            "country": {"type": "keyword"},
            "countries": {"type": "keyword"},
            "began_year": {"type": "integer"},
            "ended_year": {"type": "integer"},
            # Decade begun: born for people, formed for groups -- not "active in".
            "began_decade": {"type": "keyword"},
            "genres": {"type": "keyword"},
            "broad_genres": {"type": "keyword"},
            "main_genre": {"type": "keyword"},
            "record_labels": {"type": "keyword"},
            "instruments": {"type": "keyword"},
            "member_names": {"type": "text", "analyzer": "folded"},
            "member_mbids": {"type": "keyword"},
            "member_of_names": {"type": "text", "analyzer": "folded"},
            "member_of_mbids": {"type": "keyword"},
            "influenced_by_names": {"type": "text", "analyzer": "folded"},
            "influenced_by_mbids": {"type": "keyword"},
            "collaborator_mbids": {"type": "keyword"},
            # The artist strings in the library that resolve to this artist.
            "library_names": {"type": "keyword"},
            "track_count": {"type": "integer"},
            "main_track_count": {"type": "integer"},
            "featured_track_count": {"type": "integer"},
            "wikidata": {"type": "keyword"},
            "image": {"type": "keyword", "index": False},
            # Kept for display, not searched.
            "links": {"type": "object", "enabled": False},
            "updated_at": {"type": "date", "format": "epoch_second"},
        }
    },
}

#: Collaborators kept per artist, most shared tracks first.
MAX_COLLABORATORS = 15


def _year(date: str) -> Optional[int]:
    head = (date or "")[:4]
    return int(head) if head.isdigit() else None


def genre_profile(raw: dict[str, Any], wikidata_genres: Iterable[str] = ()
                  ) -> tuple[list[str], list[str], str]:
    """(voted genre names, strong broad families, main family) for one artist.

    MusicBrainz votes when there are any, as in ``artist_genres``; otherwise
    the Wikidata genres, one vote each. A family is strong when it has more
    than half the weight of the strongest, the rule smart playlists use.
    """
    from .artist_classifier import map_to_broad_genres
    from .artist_identity import voted_genres

    votes = voted_genres(raw or {})
    if not votes:
        votes = [(g.lower().removesuffix(" music"), 1) for g in wikidata_genres if g]
    weight: Counter = Counter()
    for name, count in votes:
        for family in map_to_broad_genres(name):
            weight[family] += count
    if not weight:
        return [n for n, _ in votes], [], ""
    top = max(weight.values())
    broad = sorted((f for f, w in weight.items() if 2 * w > top), key=lambda f: (-weight[f], f))
    return [n for n, _ in votes], broad, broad[0]


def build_documents(conn) -> list[dict[str, Any]]:
    """One document per artist in ``artists``, from Postgres."""
    from . import artist_wikidata

    artist_wikidata.ensure_schema(conn)
    artists = conn.execute("""
        SELECT a.*, w.qid, w.description, w.countries, w.genres AS wd_genres,
               w.record_labels, w.members, w.member_of, w.influenced_by, w.instruments,
               w.began, w.ended, w.image, w.external_ids, w.wikipedia
        FROM artists a LEFT JOIN artist_wikidata w ON w.mbid = a.mbid""").fetchall()
    mbid_of_qid = {a["wikidata"]: a["mbid"] for a in artists if a["wikidata"]}
    aliases = defaultdict(list)
    for r in conn.execute("SELECT mbid, alias FROM artist_aliases ORDER BY alias").fetchall():
        aliases[r["mbid"]].append(r["alias"])
    counts = {r["mbid"]: r for r in conn.execute("""
        SELECT mbid, count(DISTINCT track_id) AS n,
               count(DISTINCT track_id) FILTER (WHERE role = 'main') AS main,
               count(DISTINCT track_id) FILTER (WHERE role = 'featured') AS featured
        FROM track_artists GROUP BY mbid""").fetchall()}
    names = defaultdict(list)
    for r in conn.execute("""
        SELECT DISTINCT ta.mbid, t.artist FROM track_artists ta
        JOIN tracks t ON t.track_id = ta.track_id ORDER BY t.artist""").fetchall():
        names[r["mbid"]].append(r["artist"])
    collaborators = defaultdict(list)
    for r in conn.execute("""
        SELECT a.mbid, b.mbid AS other, count(DISTINCT a.track_id) AS n
        FROM track_artists a JOIN track_artists b
          ON b.track_id = a.track_id AND b.mbid <> a.mbid
        GROUP BY a.mbid, b.mbid ORDER BY a.mbid, n DESC, b.mbid""").fetchall():
        if len(collaborators[r["mbid"]]) < MAX_COLLABORATORS:
            collaborators[r["mbid"]].append(r["other"])

    def linked(items: Optional[list[dict[str, str]]]) -> tuple[list[str], list[str]]:
        items = items or []
        return ([i["label"] for i in items if i.get("label")],
                [mbid_of_qid[i["qid"]] for i in items if i.get("qid") in mbid_of_qid])

    docs = []
    now = int(time.time())
    for a in artists:
        genres, broad, main = genre_profile(
            a["raw"], [g["label"] for g in a["wd_genres"] or [] if g.get("label")])
        began = _year(a["begin_date"]) or _year(a["began"] or "")
        member_names, member_mbids = linked(a["members"])
        of_names, of_mbids = linked(a["member_of"])
        infl_names, infl_mbids = linked(a["influenced_by"])
        c = counts.get(a["mbid"]) or {}
        all_names = list(dict.fromkeys([a["name"], *aliases[a["mbid"]]]))
        docs.append({
            "mbid": a["mbid"], "name": a["name"], "sort_name": a["sort_name"],
            "aliases": aliases[a["mbid"]], "suggest": all_names,
            "disambiguation": a["disambiguation"], "description": a["description"] or "",
            "kind": a["kind"], "country": a["country"],
            "countries": [x["label"] for x in a["countries"] or [] if x.get("label")],
            "began_year": began, "ended_year": _year(a["end_date"]) or _year(a["ended"] or ""),
            "began_decade": f"{began // 10 * 10}s" if began else None,
            "genres": genres, "broad_genres": broad, "main_genre": main or None,
            "record_labels": [x["label"] for x in a["record_labels"] or [] if x.get("label")],
            "instruments": [x["label"] for x in a["instruments"] or [] if x.get("label")],
            "member_names": member_names, "member_mbids": member_mbids,
            "member_of_names": of_names, "member_of_mbids": of_mbids,
            "influenced_by_names": infl_names, "influenced_by_mbids": infl_mbids,
            "collaborator_mbids": collaborators[a["mbid"]],
            "library_names": names[a["mbid"]],
            "track_count": int(c.get("n") or 0), "main_track_count": int(c.get("main") or 0),
            "featured_track_count": int(c.get("featured") or 0),
            "wikidata": a["wikidata"], "image": a["image"] or "",
            "links": {"external_ids": a["external_ids"] or {}, "wikipedia": a["wikipedia"] or {}},
            "updated_at": now,
        })
    return docs


def rebuild(conn, os_client, *, alias: str = ARTIST_INDEX) -> dict[str, Any]:
    """Index every artist into a fresh index and point ``alias`` at it."""
    from opensearchpy import helpers

    docs = build_documents(conn)
    new = f"{alias}-{time.strftime('%Y%m%d-%H%M%S')}"
    os_client.indices.create(index=new, body=MAPPING)
    ok, errors = helpers.bulk(
        os_client, ({"_index": new, "_id": d["mbid"], "_source": d} for d in docs),
        raise_on_error=False, stats_only=False)
    if errors:
        log.warning("artist index: %d document(s) failed, first: %s", len(errors), errors[0])
    os_client.indices.refresh(index=new)
    old = []
    if os_client.indices.exists_alias(name=alias):
        old = list(os_client.indices.get_alias(name=alias))
    elif os_client.indices.exists(index=alias):
        # A plain index squatting on the alias name cannot be aliased over.
        os_client.indices.delete(index=alias)
    os_client.indices.update_aliases(body={"actions": [
        *({"remove": {"index": o, "alias": alias}} for o in old),
        {"add": {"index": new, "alias": alias}}]})
    for o in old:
        os_client.indices.delete(index=o)
    return {"index": new, "documents": len(docs), "indexed": ok, "failed": len(errors),
            "replaced": old}


def search(query: str, os_client, *, size: int = 10, index: str = ARTIST_INDEX,
           **filters: Any) -> list[dict[str, Any]]:
    """Artists matching what was typed so far, best first.

    ``filters`` narrow by keyword fields: ``main_genre="jazz"``,
    ``country="BE"``, ``began_decade="1970s"``, ``kind="Group"`` ...
    """
    must = []
    if query.strip():
        must.append({"bool": {"should": [
            {"multi_match": {"query": query, "type": "bool_prefix",
                             "fields": ["suggest", "suggest._2gram", "suggest._3gram"]}},
            {"multi_match": {"query": query, "fields": ["name^2", "aliases"],
                             "fuzziness": "AUTO"}},
        ]}})
    body = {"size": size, "query": {"bool": {
        "must": must or [{"match_all": {}}],
        "filter": [{"term": {k: v}} for k, v in filters.items() if v not in (None, "")]}}}
    if not must:
        body["sort"] = [{"track_count": "desc"}]
    hits = os_client.search(index=index, body=body)["hits"]["hits"]
    return [{**h["_source"], "score": h.get("_score")} for h in hits]
