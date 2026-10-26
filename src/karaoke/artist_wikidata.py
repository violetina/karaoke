"""What Wikidata knows about the library's artists, cached per MBID.

Step 3 of docs/artist_metadata_plan.md. :mod:`karaoke.artist_identity` gives
each artist a MusicBrainz ID and, usually, the Wikidata item MusicBrainz links
to. This fetches that item and keeps the parts the library can use: genres,
country, dates, record labels, members and bands, influences, instruments,
the Commons image name, IDs on other services, and the Wikipedia article.

The item comes from MusicBrainz's link when there is one, else from the item
whose MusicBrainz ID (P434) is the artist's. Referenced items (a genre, a
label, a member) are stored with their label, so nothing downstream needs to
ask Wikidata again. An artist Wikidata has no item for gets a row with an
empty ``qid``, so a re-run skips it until ``--refresh``.
"""
from __future__ import annotations

import time
from collections import Counter
from typing import Any, Callable, Optional

from psycopg.types.json import Jsonb

_SCHEMA = """
CREATE TABLE IF NOT EXISTS artist_wikidata (
    mbid          TEXT PRIMARY KEY REFERENCES artists(mbid) ON DELETE CASCADE,
    -- '' when Wikidata has no item for this artist.
    qid           TEXT NOT NULL DEFAULT '',
    label         TEXT NOT NULL DEFAULT '',
    description   TEXT NOT NULL DEFAULT '',
    -- Lists of referenced items: [{"qid": "Q43343", "label": "folk music"}, ...]
    kinds         JSONB NOT NULL DEFAULT '[]'::jsonb,
    genres        JSONB NOT NULL DEFAULT '[]'::jsonb,
    countries     JSONB NOT NULL DEFAULT '[]'::jsonb,
    record_labels JSONB NOT NULL DEFAULT '[]'::jsonb,
    members       JSONB NOT NULL DEFAULT '[]'::jsonb,
    member_of     JSONB NOT NULL DEFAULT '[]'::jsonb,
    influenced_by JSONB NOT NULL DEFAULT '[]'::jsonb,
    instruments   JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- Formed or born / dissolved or died, to the precision Wikidata gives.
    began         TEXT NOT NULL DEFAULT '',
    ended         TEXT NOT NULL DEFAULT '',
    -- A Wikimedia Commons file name; the file itself is not downloaded.
    image         TEXT NOT NULL DEFAULT '',
    external_ids  JSONB NOT NULL DEFAULT '{}'::jsonb,
    wikipedia     JSONB NOT NULL DEFAULT '{}'::jsonb,
    fetched_at    DOUBLE PRECISION NOT NULL
);
"""

#: Item-valued properties kept, by column.
ITEM_PROPERTIES = {
    "kinds": "P31",           # instance of: human, musical group, ...
    "genres": "P136",
    "countries": "P495",      # country of origin (bands); citizenship is the fallback
    "record_labels": "P264",
    "members": "P527",        # has part(s): a band's members
    "member_of": "P463",
    "influenced_by": "P737",
    "instruments": "P1303",
}
CITIZENSHIP = "P27"
BEGAN = ("P571", "P569")      # inception, date of birth
ENDED = ("P576", "P570")      # dissolved, date of death
IMAGE = "P18"
#: External-ID properties, by the name stored in ``external_ids``.
EXTERNAL_IDS = {
    "spotify": "P1902", "discogs": "P1953", "allmusic": "P1728", "lastfm": "P3192",
    "apple_music": "P2850", "deezer": "P2722", "youtube_channel": "P2397",
}
#: Artists handled per round of Wikidata requests.
CHUNK = 200


def ensure_schema(conn) -> None:
    conn.execute(_SCHEMA)
    conn.commit()


# -- the pure part -------------------------------------------------------------

def statements(entity: dict[str, Any], prop: str) -> list[dict[str, Any]]:
    """A property's statements as Wikidata ranks them.

    Preferred statements when there are any (they mark the current value),
    else the normal ones; deprecated statements never.
    """
    claims = [s for s in (entity.get("claims") or {}).get(prop, [])
              if s.get("rank") != "deprecated"]
    preferred = [s for s in claims if s.get("rank") == "preferred"]
    return preferred or claims


def _value(statement: dict[str, Any]) -> Any:
    return ((statement.get("mainsnak") or {}).get("datavalue") or {}).get("value")


def item_ids(entity: dict[str, Any], prop: str) -> list[str]:
    out = []
    for s in statements(entity, prop):
        v = _value(s)
        if isinstance(v, dict) and v.get("id") and v["id"] not in out:
            out.append(v["id"])
    return out


def first_string(entity: dict[str, Any], prop: str) -> str:
    for s in statements(entity, prop):
        v = _value(s)
        if isinstance(v, str) and v:
            return v
    return ""


def format_time(value: dict[str, Any]) -> str:
    """A Wikidata time at its precision: "1970", "1970-12" or "1970-12-24"."""
    t = (value or {}).get("time") or ""
    if len(t) < 11:
        return ""
    date = t.lstrip("+")[:10]
    precision = int(value.get("precision") or 11)
    if precision <= 9:
        return date[:4]
    if precision == 10:
        return date[:7]
    return date


def first_time(entity: dict[str, Any], props: tuple[str, ...]) -> str:
    for prop in props:
        for s in statements(entity, prop):
            v = _value(s)
            if isinstance(v, dict) and v.get("time"):
                return format_time(v)
    return ""


def referenced_items(entity: dict[str, Any]) -> set[str]:
    """Every item the stored fields point at, to fetch their labels in one go."""
    refs = {q for prop in ITEM_PROPERTIES.values() for q in item_ids(entity, prop)}
    return refs | set(item_ids(entity, CITIZENSHIP))


def parse(entity: dict[str, Any], names: dict[str, str]) -> dict[str, Any]:
    """The ``artist_wikidata`` fields of one entity; ``names`` labels its references."""
    from .wikidata import label_of

    def items(prop: str) -> list[dict[str, str]]:
        return [{"qid": q, "label": names.get(q, "")} for q in item_ids(entity, prop)]

    row = {col: items(prop) for col, prop in ITEM_PROPERTIES.items()}
    if not row["countries"]:
        row["countries"] = items(CITIZENSHIP)
    descriptions = entity.get("descriptions") or {}
    row.update({
        "qid": entity.get("id", ""),
        "label": label_of(entity),
        "description": next((descriptions[lang]["value"] for lang in ("en", "nl")
                             if lang in descriptions), ""),
        "began": first_time(entity, BEGAN),
        "ended": first_time(entity, ENDED),
        "image": first_string(entity, IMAGE),
        "external_ids": {name: v for name, prop in EXTERNAL_IDS.items()
                         if (v := first_string(entity, prop))},
        "wikipedia": {site[:2]: link["title"]
                      for site, link in (entity.get("sitelinks") or {}).items()
                      if site in ("enwiki", "nlwiki")},
    })
    return row


# -- storage -------------------------------------------------------------------

_JSON_COLUMNS = (*ITEM_PROPERTIES, "external_ids", "wikipedia")
_TEXT_COLUMNS = ("qid", "label", "description", "began", "ended", "image")


def save(mbid: str, row: dict[str, Any], conn) -> None:
    """Store (replace) one artist's Wikidata facts; an empty row means "no item"."""
    cols = [*_TEXT_COLUMNS, *_JSON_COLUMNS]
    values = [row.get(c, "") for c in _TEXT_COLUMNS]
    values += [Jsonb(row.get(c, {} if c in ("external_ids", "wikipedia") else []))
               for c in _JSON_COLUMNS]
    conn.execute(f"""
        INSERT INTO artist_wikidata (mbid, {', '.join(cols)}, fetched_at)
        VALUES (%s, {', '.join(['%s'] * len(cols))}, %s)
        ON CONFLICT (mbid) DO UPDATE SET
            {', '.join(f'{c} = excluded.{c}' for c in cols)},
            fetched_at = excluded.fetched_at""", (mbid, *values, time.time()))


def pending(conn, *, refresh: bool = False, limit: Optional[int] = None,
            mbids: Optional[list[str]] = None) -> list[dict[str, Any]]:
    """Artists without Wikidata facts yet (all of them with ``refresh``).

    ``mbids`` narrows it to those artists, as for a newly ingested song.
    """
    rows = conn.execute(f"""
        SELECT a.mbid, a.name, a.wikidata FROM artists a
        LEFT JOIN artist_wikidata w ON w.mbid = a.mbid
        WHERE {'TRUE' if refresh else 'w.mbid IS NULL'}
          AND (%s::text[] IS NULL OR a.mbid = ANY(%s::text[]))
        ORDER BY (SELECT count(*) FROM track_artists ta WHERE ta.mbid = a.mbid) DESC, a.mbid
        """, (mbids, mbids)).fetchall()
    return [dict(r) for r in (rows[:limit] if limit else rows)]


def run(conn, wd, *, limit: Optional[int] = None, refresh: bool = False,
        dry_run: bool = False, mbids: Optional[list[str]] = None,
        out: Callable[[str], None] = print) -> Counter:
    """Fetch and store Wikidata facts for pending artists, ``CHUNK`` at a time.

    ``wd`` is the :mod:`karaoke.wikidata` module or a stand-in with the same
    ``qids_for_mbids``, ``entities`` and ``labels``.
    """
    ensure_schema(conn)
    todo = pending(conn, refresh=refresh, limit=limit, mbids=mbids)
    totals: Counter = Counter()
    for start in range(0, len(todo), CHUNK):
        chunk = todo[start:start + CHUNK]
        qid_of = {a["mbid"]: a["wikidata"] for a in chunk if a["wikidata"]}
        missing = [a["mbid"] for a in chunk if not a["wikidata"]]
        if missing:
            found = wd.qids_for_mbids(missing)
            if found is None:
                totals["failed"] += len(chunk)
                out(f"[{start + len(chunk)}/{len(todo)}] Wikidata unreachable; chunk left for next run")
                continue
            qid_of.update(found)
        ents = wd.entities(qid_of.values())
        names = wd.labels({q for e in (ents or {}).values() for q in referenced_items(e)})
        if ents is None or names is None:
            totals["failed"] += len(chunk)
            out(f"[{start + len(chunk)}/{len(todo)}] Wikidata unreachable; chunk left for next run")
            continue
        for a in chunk:
            ent = ents.get(qid_of.get(a["mbid"], ""))
            row = parse(ent, names) if ent else {}
            totals["enriched" if row else "no item"] += 1
            if row and not a["wikidata"]:
                totals["linked by MBID"] += 1
            if dry_run:
                genres = ", ".join(g["label"] for g in row.get("genres", [])[:3])
                out(f"  {a['name'][:30]:30} {row.get('qid', '-'):10} {row.get('began', ''):10} "
                    f"{genres[:50]}")
                continue
            save(a["mbid"], row, conn)
            if row and not a["wikidata"]:
                conn.execute("UPDATE artists SET wikidata = %s WHERE mbid = %s",
                             (row["qid"], a["mbid"]))
        if not dry_run:
            conn.commit()
        out(f"[{start + len(chunk)}/{len(todo)}] {dict(totals)}")
    return totals
