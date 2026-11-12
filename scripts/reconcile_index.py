"""Reconcile the derived OpenSearch indexes with PostgreSQL (dry-run by default).

PostgreSQL is the source of truth; every index here is derived. After merges,
renames and long post-processing batches the two drift, so this script:

1. deletes docs whose ``track_id`` no longer exists (tracks, tracks-lines,
   karaoke-clap, karaoke-progression);
2. rebuilds library tracks missing from the ``tracks`` index (targeted, by id);
3. partial-updates the ``tracks`` docs whose artist/title/album or Mood v2,
   Chords v2 and CLAP genre fields differ from PostgreSQL, removing fields the
   database no longer backs;
4. does the same for the genre fields on ``karaoke-clap``.

Tracks lacking a CLAP or progression doc need audio and are only reported;
queue them with ``scripts/enqueue_mood_chords.py``.

Usage:
    PYTHONPATH=src python scripts/reconcile_index.py            # report only
    PYTHONPATH=src python scripts/reconcile_index.py --apply
"""
from __future__ import annotations

import argparse
from typing import Any, Iterable

from opensearchpy import helpers

from karaoke import clap_vector, localcache, vector_index
from karaoke.config import settings
from karaoke.vector_index import (AUDIO_ANALYSIS_FIELDS, GENRE_FIELDS, audio_analysis_fields,
                                  genre_fields)

LINES_INDEX = f"{settings.index_name}-lines"
PROGRESSION_INDEX = "karaoke-progression"
IDENTITY = ("artist", "title", "album")
MANAGED = IDENTITY + AUDIO_ANALYSIS_FIELDS + GENRE_FIELDS
CLAP_MANAGED = ("genre", "genre_score", "genre_runner_up")

SQL = """
SELECT t.track_id, t.artist, t.title, t.album,
       tm.mood_version, tm.summary AS mood_summary,
       tc.chords_version, tc.harmony AS chord_harmony,
       tg.genre, tg.score AS genre_score, tg.runner_up, tg.runner_up_score
FROM tracks t
LEFT JOIN track_moods tm ON tm.track_id = t.track_id
LEFT JOIN track_chords tc ON tc.track_id = t.track_id
LEFT JOIN track_genre tg ON tg.track_id = t.track_id
"""


def expected_fields(row: Any) -> dict[str, Any]:
    fields = {"artist": row["artist"] or "", "title": row["title"] or "",
              "album": row["album"] or ""}
    fields.update(audio_analysis_fields(row["mood_version"], row["mood_summary"],
                                        row["chords_version"], row["chord_harmony"]))
    fields.update(genre_fields(row["genre"], row["genre_score"], row["runner_up"],
                               row["runner_up_score"]))
    return fields


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) < 1e-6
        except (TypeError, ValueError):
            return False
    return a == b


def diff_doc(current: dict[str, Any], wanted: dict[str, Any], managed: Iterable[str]):
    """Return (fields to set, fields to remove) for one document."""
    managed = tuple(managed)
    to_set = {k: v for k, v in wanted.items()
              if k in managed and not _same(current.get(k), v)}
    # build_track_doc writes a blank audio_genre* baseline; blank equals absent.
    to_remove = [k for k in managed if current.get(k) not in (None, "") and k not in wanted
                 and k not in IDENTITY]
    return to_set, to_remove


def scan_source(client, index: str, fields: list[str]) -> Iterable[tuple[str, dict]]:
    for h in helpers.scan(client, index=index, query={"_source": fields,
                                                       "query": {"match_all": {}}}):
        yield h["_id"], h["_source"]


def update_actions(index: str, doc_id: str, to_set: dict, to_remove: list[str]):
    if to_remove:
        script = "".join(f"ctx._source.remove('{f}'); " for f in to_remove)
        params = {}
        for i, (k, v) in enumerate(to_set.items()):
            script += f"ctx._source['{k}'] = params.p{i}; "
            params[f"p{i}"] = v
        return {"_op_type": "update", "_index": index, "_id": doc_id,
                "script": {"source": script, "params": params}}
    return {"_op_type": "update", "_index": index, "_id": doc_id, "doc": to_set}


def run(apply: bool) -> int:
    from karaoke.osclient import client as os_client

    client = os_client()
    if client is None:
        print("OpenSearch unreachable")
        return 1
    with localcache.connect() as conn:
        rows = {int(r["track_id"]): r for r in conn.execute(SQL).fetchall()}
    print(f"database: {len(rows)} tracks   mode: {'APPLY' if apply else 'dry-run'}")
    known = set(rows)

    # 1. orphans
    for index in (settings.index_name, LINES_INDEX, clap_vector.CLAP_INDEX, PROGRESSION_INDEX):
        orphans, present = [], set()
        for doc_id, src in scan_source(client, index, ["track_id"]):
            tid = src.get("track_id")
            if tid is None:
                continue
            present.add(int(tid))
            if int(tid) not in known:
                orphans.append(doc_id)
        print(f"{index}: {len(present)} tracks indexed, {len(orphans)} orphan docs, "
              f"{len(known - present)} library tracks without docs")
        if index == settings.index_name:
            missing = sorted(known - present)
        if apply and orphans:
            helpers.bulk(client, ({"_op_type": "delete", "_index": index, "_id": i}
                                  for i in orphans), raise_on_error=False)

    # 2. tracks missing from the lyric index
    if apply and missing:
        stats = vector_index.rebuild_from_db(track_ids=missing, include_lines=True)
        print(f"rebuilt {len(missing)} missing track docs: {stats}")

    # 3. tracks index field sync
    sets = removes = 0
    actions = []
    for doc_id, src in scan_source(client, settings.index_name, ["track_id", *MANAGED]):
        tid = src.get("track_id")
        if tid is None or int(tid) not in known:
            continue
        to_set, to_remove = diff_doc(src, expected_fields(rows[int(tid)]), MANAGED)
        if to_set or to_remove:
            sets += bool(to_set)
            removes += bool(to_remove)
            actions.append(update_actions(settings.index_name, doc_id, to_set, to_remove))
    print(f"{settings.index_name}: {len(actions)} docs differ "
          f"({sets} need values set, {removes} need stale fields removed)")

    # 4. clap genre mirror
    clap_actions = []
    for doc_id, src in scan_source(client, clap_vector.CLAP_INDEX, ["track_id", *CLAP_MANAGED]):
        tid = src.get("track_id")
        if tid is None or int(tid) not in known:
            continue
        wanted = {k: v for k, v in expected_fields(rows[int(tid)]).items() if k in CLAP_MANAGED}
        to_set, to_remove = diff_doc(src, wanted, CLAP_MANAGED)
        if to_set or to_remove:
            clap_actions.append(update_actions(clap_vector.CLAP_INDEX, doc_id, to_set, to_remove))
    print(f"{clap_vector.CLAP_INDEX}: {len(clap_actions)} docs differ")

    if apply:
        for label, acts in (("tracks", actions), ("clap", clap_actions)):
            ok, errs = helpers.bulk(client, acts, chunk_size=500, raise_on_error=False)
            print(f"applied {label}: {ok} ok, {len(errs) if isinstance(errs, list) else errs} errors")
        for index in (settings.index_name, clap_vector.CLAP_INDEX):
            client.indices.refresh(index=index)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="write changes (default: report only)")
    return run(ap.parse_args(argv).apply)


if __name__ == "__main__":
    raise SystemExit(main())
