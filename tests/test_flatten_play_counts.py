"""scripts/flatten_play_counts.py: dev/training play counts flattened."""
from __future__ import annotations

from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.flatten_play_counts import clean_counts, flattened, main  # noqa: E402

DAY = 86400.0
NOON = 1_757_000_000.0  # 2025-09-04, mid-day


def _ev(artist, title, ts):
    return {"artist": artist, "title": title, "ts": ts}


def test_a_fixture_burst_counts_nothing_and_a_relogged_airing_counts_once():
    events = [_ev("The Slits", "I Heard It Through The Grapevine", NOON + i * 300) for i in range(50)]
    # One radio airing logged four times within a minute, then aired again an hour later.
    events += [_ev("Hozier", "Too Sweet", NOON + s) for s in (0, 0, 12, 50)]
    events += [_ev("hozier", "too sweet", NOON + 3600)]
    # Real plays on separate days.
    events += [_ev("Ween", "Mutilated Lips", NOON + d * DAY) for d in range(3)]
    counts = clean_counts(events)
    assert ("the slits", "i heard it through the grapevine") not in counts
    assert counts[("hozier", "too sweet")] == 2
    assert counts[("ween", "mutilated lips")] == 3


def test_flattened_never_raises_a_count():
    assert flattened(current=388, clean=0, cap=1) == 0
    assert flattened(current=3, clean=3, cap=1) == 1
    assert flattened(current=1, clean=5, cap=3) == 1      # events alone do not add plays
    assert flattened(current=4, clean=2, cap=0) == 0


def _seed(conn):
    cur = conn.cursor()
    for tid, artist, title, plays in ((9001, "The Slits", "Grapevine", 388),
                                      (9002, "Ween", "Mutilated Lips", 3),
                                      (9003, "Nobody", "Never Played", 0)):
        cur.execute("INSERT INTO tracks (track_id, artist, title, album, duration, play_count)"
                    " VALUES (%s, %s, %s, '', 100.0, %s)", (tid, artist, title, plays))
    for i in range(40):
        cur.execute("INSERT INTO play_events (ts, mode, artist, title, event, source, has_synced)"
                    " VALUES (%s, 'spotify', 'The Slits', 'Grapevine', 'play', '', 0)", (NOON + i * 300,))
    for d in range(3):
        cur.execute("INSERT INTO play_events (ts, mode, artist, title, event, source, has_synced)"
                    " VALUES (%s, 'radio', 'Ween', 'Mutilated Lips', 'play', '', 0)", (NOON + d * DAY,))
    conn.commit()


def _counts(conn):
    return {r["track_id"]: r["play_count"] for r in conn.execute(
        "SELECT track_id, play_count FROM tracks WHERE track_id BETWEEN 9001 AND 9003").fetchall()}


def test_the_target_flattens_and_keeps_a_backup():
    from karaoke import localcache
    with localcache.connect() as conn:
        _seed(conn)
    assert main(["--dry-run"]) == 0
    with localcache.connect() as conn:
        assert _counts(conn) == {9001: 388, 9002: 3, 9003: 0}   # dry run writes nothing

    assert main([]) == 0
    with localcache.connect() as conn:
        assert _counts(conn) == {9001: 0, 9002: 1, 9003: 0}
        backups = [r["table_name"] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_name LIKE 'tracks_play_count_backup_%'").fetchall()]
        assert backups
        saved = conn.execute(f"SELECT play_count FROM {backups[-1]} WHERE track_id = 9001").fetchone()
        assert saved["play_count"] == 388
        for table in backups:
            conn.execute(f"DROP TABLE {table}")
        conn.commit()
