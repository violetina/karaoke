"""Back up OpenSearch document indices and vector embeddings into PostgreSQL JSONB.

Usage:
    python scripts/backup_opensearch_to_postgres.py [--indices tracks,karaoke-clap,karaoke-progression]
"""
from __future__ import annotations

import argparse
import json
import time
from typing import Any

from karaoke import localcache, osclient
from karaoke.logger import log

DDL = """
CREATE TABLE IF NOT EXISTS opensearch_snapshots (
    index_name  TEXT NOT NULL,
    doc_id      TEXT NOT NULL,
    track_id    INTEGER,
    body        JSONB NOT NULL,
    updated_at  DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (index_name, doc_id)
);
CREATE INDEX IF NOT EXISTS idx_opensearch_snapshots_track ON opensearch_snapshots (track_id);
CREATE INDEX IF NOT EXISTS idx_opensearch_snapshots_index ON opensearch_snapshots (index_name);
"""

DEFAULT_INDICES = [
    "tracks",
    "karaoke-clap",
    "karaoke-progression",
    "tracks-lines",
    "tracks-notes",
]


def ensure_table(conn: Any) -> None:
    conn.execute(DDL)
    conn.commit()


def backup_index(cli: Any, conn: Any, index_name: str, batch_size: int = 500) -> int:
    if not cli.indices.exists(index=index_name):
        log.info("Index %s does not exist, skipping", index_name)
        return 0

    log.info("Backing up OpenSearch index '%s' to PostgreSQL...", index_name)
    now = time.time()
    
    # Scroll through all docs in index
    query: dict[str, Any] = {"query": {"match_all": {}}}
    resp = cli.search(index=index_name, body=query, scroll="2m", size=batch_size)
    scroll_id = resp.get("_scroll_id")
    hits = resp["hits"]["hits"]
    
    total_backed_up = 0

    try:
        while hits:
            records = []
            for hit in hits:
                doc_id = hit["_id"]
                body = hit["_source"]
                track_id = body.get("track_id")
                records.append((index_name, str(doc_id), track_id, json.dumps(body), now))

            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO opensearch_snapshots (index_name, doc_id, track_id, body, updated_at)
                    VALUES (%s, %s, %s, %s::jsonb, %s)
                    ON CONFLICT (index_name, doc_id) DO UPDATE SET
                        track_id = EXCLUDED.track_id,
                        body = EXCLUDED.body,
                        updated_at = EXCLUDED.updated_at
                    """,
                    records
                )
            total_backed_up += len(records)
            log.info("  Index '%s': %d docs backed up...", index_name, total_backed_up)

            resp = cli.scroll(scroll_id=scroll_id, scroll="2m")
            scroll_id = resp.get("_scroll_id")
            hits = resp["hits"]["hits"]
    finally:
        if scroll_id:
            try:
                cli.clear_scroll(scroll_id=scroll_id)
            except Exception:
                pass

    log.info("Completed index '%s': %d docs snapshot to Postgres.", index_name, total_backed_up)
    return total_backed_up


def restore_index(cli: Any, conn: Any, index_name: str, batch_size: int = 500) -> int:
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM opensearch_snapshots WHERE index_name = %s", (index_name,))
    row = cur.fetchone()
    total_count = row["count"] if row else 0
    if total_count == 0:
        log.info("No snapshots found for index '%s', skipping", index_name)
        return 0

    log.info("Restoring OpenSearch index '%s' from %d snapshot records...", index_name, total_count)
    cur.execute("SELECT doc_id, body FROM opensearch_snapshots WHERE index_name = %s", (index_name,))
    
    restored = 0
    batch = []
    from opensearchpy.helpers import bulk
    while True:
        rows = cur.fetchmany(batch_size)
        if not rows:
            break
        for r in rows:
            doc_id = r["doc_id"]
            body = r["body"] if isinstance(r["body"], dict) else json.loads(r["body"])
            batch.append({"_index": index_name, "_id": doc_id, "_source": body})
            if len(batch) >= batch_size:
                bulk(cli, batch)
                restored += len(batch)
                log.info("  Index '%s': %d / %d docs restored...", index_name, restored, total_count)
                batch = []
    if batch:
        bulk(cli, batch)
        restored += len(batch)

    log.info("Completed index '%s': %d docs restored into OpenSearch.", index_name, restored)
    return restored


def main() -> None:
    parser = argparse.ArgumentParser(description="Backup or restore OpenSearch indices using PostgreSQL opensearch_snapshots table.")
    parser.add_argument("--indices", help="Comma-separated list of index names to back up or restore", default=",".join(DEFAULT_INDICES))
    parser.add_argument("--restore", action="store_true", help="Restore indices from PostgreSQL snapshots into OpenSearch")
    args = parser.parse_args()

    target_indices = [i.strip() for i in args.indices.split(",") if i.strip()]

    cli = osclient.client()
    conn = localcache.connect()

    ensure_table(conn)

    grand_total = 0
    if args.restore:
        for idx in target_indices:
            grand_total += restore_index(cli, conn, idx)
        print(f"\nSuccessfully restored {grand_total} OpenSearch documents across {len(target_indices)} indices from PostgreSQL.")
    else:
        for idx in target_indices:
            grand_total += backup_index(cli, conn, idx)
        print(f"\nSuccessfully backed up {grand_total} OpenSearch documents across {len(target_indices)} indices into PostgreSQL.")


if __name__ == "__main__":
    main()
