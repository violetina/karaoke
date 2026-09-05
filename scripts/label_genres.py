"""Label every CLAP-embedded track with a genre, and store it both places.

SQLite is the source of truth, as everywhere else here; the OpenSearch copy
exists so a search can filter or facet on it without a join back to the
database.
"""
from __future__ import annotations

import argparse
import time
from typing import Optional

from karaoke import clap_vector, genre, localcache
from karaoke.logger import log


def _forget_genre(client, track_id: int) -> None:
    """Remove the genre fields from both documents for a track."""
    from karaoke.config import settings
    from karaoke.vector_index import track_doc_id

    script = {"script": {"source":
              "ctx._source.remove('genre'); "
              "ctx._source.remove('genre_score'); "
              "ctx._source.remove('genre_runner_up')"}}
    for index, doc in ((clap_vector.CLAP_INDEX, clap_vector.doc_id(track_id)),
                       (settings.index_name, track_doc_id(track_id))):
        try:
            client.update(index=index, id=doc, body=script)
        except Exception:
            log.debug("could not clear genre on %s/%s", index, doc)


def run(*, overwrite: bool = False, dry_run: bool = False,
        limit: Optional[int] = None) -> int:
    if not clap_vector.available():
        print("CLAP is unavailable: torch and transformers are required.")
        return 1
    from karaoke.osclient import client as os_client

    conn = localcache.connect()
    localcache.ensure_genre_table(conn)
    client = os_client()

    print(f"embedding {len(genre.GENRES)} genre label(s)")
    labels = genre.label_vectors()
    if not labels:
        print("no labels could be embedded")
        return 1

    res = client.search(index=clap_vector.CLAP_INDEX, body={
        "size": 10000, "query": {"match_all": {}},
        "_source": ["track_id", "artist", "title", "clap_vector"]})
    docs = [h["_source"] for h in res["hits"]["hits"] if h["_source"].get("clap_vector")]
    print(f"{len(docs)} embedded track(s)\n")

    labelled = unlabelled = skipped = 0
    for doc in docs:
        if limit and labelled >= limit:
            break
        track_id = int(doc["track_id"])
        if not overwrite and localcache.genre_for(track_id, conn) is not None:
            skipped += 1
            continue
        verdict = genre.classify(doc["clap_vector"], labels)
        name = f"{doc.get('artist', '')[:22]:24} {doc.get('title', '')[:26]:28}"
        if verdict is None:
            # Under --overwrite a track that no longer clears the floor must
            # lose its old row, or a re-run silently keeps an answer the
            # current rules would refuse to make.
            if not dry_run and localcache.clear_genre(track_id, conn):
                # And from the index, or the two stores disagree: SQLite would
                # say unlabelled while a search still filtered the track into
                # its old genre.
                _forget_genre(client, track_id)
                print(f"  {name} unlabelled (previous label removed)")
            else:
                print(f"  {name} unlabelled")
            unlabelled += 1
            continue
        note = "" if verdict.clear else f"  ~ close to {verdict.runner_up}"
        print(f"  {name} {verdict.genre:20} {verdict.score:+.3f}{note}")
        if dry_run:
            labelled += 1
            continue

        localcache.record_genre(track_id, verdict, conn)
        # Mirror onto both documents: the CLAP one so a sounds-like result can
        # show a label, and the track document so ordinary search can filter.
        try:
            body = {"doc": {"genre": verdict.genre,
                            "genre_score": verdict.score,
                            "genre_runner_up": verdict.runner_up}}
            client.update(index=clap_vector.CLAP_INDEX,
                          id=clap_vector.doc_id(track_id), body=body)
        except Exception:
            log.debug("could not annotate clap doc for %s", track_id)
        try:
            from karaoke.config import settings
            from karaoke.vector_index import track_doc_id

            client.update(index=settings.index_name,
                          id=track_doc_id(track_id), body=body)
        except Exception:
            # A track may not be in the lyric index at all; the SQLite row is
            # the one that must land.
            log.debug("could not annotate track doc for %s", track_id)
        labelled += 1

    if not dry_run:
        for index in (clap_vector.CLAP_INDEX,):
            try:
                client.indices.refresh(index=index)
            except Exception:
                pass
    print(f"\n{labelled} labelled, {unlabelled} matched nothing, {skipped} already had one")
    if not dry_run:
        print("\nby genre:")
        for row in localcache.genre_counts(conn):
            print(f"   {row['n']:4}  {row['genre']:22} mean {row['mean_score']:+.3f}")
    conn.close()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args(argv)
    log.info("labelling genres from CLAP embeddings")
    return run(overwrite=args.overwrite, dry_run=args.dry_run, limit=args.limit)


if __name__ == "__main__":
    raise SystemExit(main())
