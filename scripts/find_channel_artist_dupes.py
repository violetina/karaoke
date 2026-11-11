"""Classify and clean up tracks filed under channel handles, typos, or track numbers.

``db_cleanup.run_deduplication`` groups by normalized artist string for speed,
so it cannot see cross-artist duplicates such as:

- uploader / channel handles: ``Vidiot411 - Roger And Out`` -> ``Neil Young``
- track numbers parsed as artist: ``02 - Magic`` -> ``Coldplay``
- artist typos / variants: ``Frankie Rosie`` -> ``Frankie Rose``,
  ``Geoghio Zamfir`` -> ``Gheorghe Zamfir``, ``D. Shanon`` -> ``Del Shannon``
- reordered collaborations: ``Eric Clapton & J.J. Cale`` <-> ``J.J. Cale & Eric Clapton``

At the same time, a naive title + duration match across different artists has
false positives: covers by real musicians (``Richie Kotzen - Money`` vs
``Pink Floyd - Money``, ``Steve Howe - Goodbye Blue Sky``) and numeric strings
that MusicBrainz matched by name (``'01'``, ``'13'``).

This script separates those cases using multiple signals:

1. Rejects numeric strings (``^\\d{1,3}$``) and placeholders (``[dialogue]``,
   ``[unknown]``, ``Various Artists``) as valid resolved artists.
2. Repairs tracks whose artist is a bare track number (``^\\d{1,3}$``) by
   extracting the real artist from their local file path in ``sources``
   (renaming in-place or merging when the target track already exists).
3. Classifies remaining cross-artist matches against genuinely resolved
   MusicBrainz artists into:
   - ``AUTO_TYPO_VARIANT``: high artist string similarity (>= 0.84), ``The X``
     vs ``X, The``, or single-word prefix of a multi-word band on the same song.
   - ``AUTO_COLLAB_REORDER``: identical set of artist tokens in a different
     order or separator (``A & B`` vs ``B & A``, ``A / A`` vs ``A``).
   - ``AUTO_CHANNEL_UPLOADER``: source artist is explicitly ``unmatched`` in
     ``artist_lookups`` (MusicBrainz confirmed no such artist exists), has no
     local audio file of its own, and either matches channel/handle hints or
     has <= 2 tracks in the library, matching a single resolved artist.
   - ``REVIEW_COLLAB_SUBSET``: one artist credit is a subset of the other on a
     same-duration recording (e.g. ``Miles Davis & John Coltrane`` vs
     ``Miles Davis``).
   - ``SKIP_COVER_OR_AMBIGUOUS``: real or unverified artists covering the same
     song title (e.g. ``Richie Kotzen``, ``Steve Howe``, ``Janis Joplin``), or
     titles shared by multiple distinct resolved artists without disambiguating
     duration/artist evidence.

Run:
    ``PYTHONPATH=src .venv/bin/python scripts/find_channel_artist_dupes.py``
    ``PYTHONPATH=src .venv/bin/python scripts/find_channel_artist_dupes.py --apply``
"""
from __future__ import annotations

import argparse
import importlib.util
import re
import sys
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from karaoke import localcache  # noqa: E402
from karaoke.artist_identity import is_placeholder  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "db_cleanup", Path(__file__).resolve().parent / "db_cleanup.py"
)
dbc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dbc)

_NUMERIC_ARTIST = re.compile(r"^\d{1,3}$")
_CHANNEL_HINT = re.compile(
    r"(official|vevo|records?|music|tv|channel|topic|lyrics?|karaoke|"
    r"videos?|global\s+hits|\[ost\]|#\d+|\d{2,}|^[a-z]+[A-Z][a-z]+\d*$)",
    re.IGNORECASE,
)
_COLLAB_SPLIT = re.compile(
    r"\s*(?:/|,|&|\band\b|\bfeat\.?\b|\bft\.?\b|\bvs\.?\b|\bwith\b|\bx\b)\s*",
    re.IGNORECASE,
)


def is_valid_resolved_artist(artist: str, status: str | None) -> bool:
    """True only when ``artist`` is a real resolved MusicBrainz artist (not a number or placeholder)."""
    a = (artist or "").strip()
    if not a or status != "resolved":
        return False
    if _NUMERIC_ARTIST.match(a):
        return False
    if is_placeholder("", a) or a.lower() in ("various artists", "unknown", "artist"):
        return False
    return True


def _norm_artist_str(a: str) -> str:
    s = (a or "").lower().strip()
    # Normalize "Gap Band, The" -> "the gap band"
    m = re.match(r"^(.+),\s*(the|a|an)$", s)
    if m:
        s = f"{m.group(2)} {m.group(1)}"
    s = re.sub(r"^(?:the|a|an)\s+", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return s


def _artist_tokens(a: str) -> set[str]:
    parts = [_norm_artist_str(p) for p in _COLLAB_SPLIT.split(a or "")]
    return {p for p in parts if p}


def infer_from_local_path(p_str: str, cur_artist: str, cur_title: str) -> tuple[str, str]:
    """Infer ``(artist, title)`` from a local audio file path when artist is a track number."""
    p = Path(p_str)
    parts = p.parts
    for base in ("Music-backup-DATA", "Music"):
        if base in parts:
            idx = parts.index(base)
            sub = parts[idx + 1 :]
            if sub:
                top = sub[0]
                if top in ("Frank Sinatra", "Motorpsycho", "Frank Zappa"):
                    return top, cur_title
                if "COLDPLAY" in top.upper():
                    if cur_artist == "42" and cur_title.lower() == "coldplay":
                        return "Coldplay", "42"
                    return "Coldplay", cur_title
                if "MADNESS" in top.upper():
                    return "Madness", cur_title
            break
    stem = p.stem
    m = re.match(r"^\d{1,3}\s*-\s*([^-]+?)\s*-\s*(.+)$", stem)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    m2 = re.match(r"^([^-]+?)\s*-\s*[^-]+?\s*-\s*\d{1,3}\s*-\s*(.+)$", stem)
    if m2:
        return m2.group(1).strip(), m2.group(2).strip()
    m3 = re.match(r"^\d{1,3}\s*-\s*(.+?)\s*\(([^)]+)\)\s*$", stem)
    if m3:
        return m3.group(2).strip(), m3.group(1).strip()
    return "", cur_title


def repair_numeric_artist_tracks(conn, *, apply: bool = False) -> tuple[int, int]:
    """Repair or merge tracks whose artist is a bare track number (e.g. '01', '02').

    Returns ``(renamed_count, merged_count)``.
    """
    rows = conn.execute(
        """
        SELECT t.track_id, t.artist, t.title, t.album, t.duration,
               array_agg(s.url) FILTER (WHERE s.kind = 'local' OR s.url LIKE '/%%') AS local_paths
        FROM tracks t
        LEFT JOIN sources s USING (track_id)
        WHERE t.artist ~ '^[0-9]{1,3}$'
        GROUP BY t.track_id
        ORDER BY t.track_id
        """
    ).fetchall()

    renamed = 0
    merged = 0
    for r in rows:
        paths = r["local_paths"] or []
        if not paths:
            continue
        new_artist, new_title = infer_from_local_path(paths[0], r["artist"], r["title"])
        if not new_artist or _NUMERIC_ARTIST.match(new_artist):
            continue

        # Check if a track with (new_artist, new_title) or compatible duplicate already exists
        dest_rows = conn.execute(
            """
            SELECT track_id, artist, title, duration
            FROM tracks
            WHERE lower(artist) = lower(%s) AND track_id <> %s
            ORDER BY track_id
            """,
            (new_artist, r["track_id"]),
        ).fetchall()

        match_dest = None
        for d in dest_rows:
            if d["title"].lower().strip() == new_title.lower().strip():
                match_dest = d
                break
            fake_src = {"artist": new_artist, "title": new_title, "duration": r["duration"]}
            if dbc.is_duplicate(fake_src, d):
                match_dest = d
                break

        if match_dest is not None:
            merged += 1
            if apply:
                with conn.transaction():
                    dbc.merge_tracks(r["track_id"], match_dest["track_id"], conn)
        else:
            renamed += 1
            if apply:
                with conn.transaction():
                    conn.execute(
                        "UPDATE tracks SET artist = %s, title = %s WHERE track_id = %s",
                        (new_artist, new_title, r["track_id"]),
                    )
                    # Clear stale numeric track_artists links so they re-link to the real artist
                    conn.execute("DELETE FROM track_artists WHERE track_id = %s", (r["track_id"],))

    if apply and (renamed or merged):
        with conn.transaction():
            conn.execute("DELETE FROM artist_lookups WHERE artist_text ~ '^[0-9]{1,3}$'")
        conn.commit()
    return renamed, merged


def classify_pair(
    src: dict[str, Any],
    hits: list[tuple[dict[str, Any], str]],
    artist_track_counts: Counter,
    local_paths_by_tid: dict[int, list[str]],
    shared_urls: dict[int, set[str]],
) -> tuple[str, dict[str, Any] | None, str]:
    """Return ``(category, chosen_dest, reason)`` for a suspect track and its resolved hits."""
    s_art = src["artist"]
    s_norm = _norm_artist_str(s_art)
    s_toks = _artist_tokens(s_art)
    s_paths = local_paths_by_tid.get(src["track_id"], [])

    # 1. Check if any hit is a direct artist typo/variant, collab reorder, or local-folder artist match
    for r, rel in hits:
        r_art = r["artist"]
        r_norm = _norm_artist_str(r_art)
        r_toks = _artist_tokens(r_art)

        if s_norm and r_norm:
            sim = SequenceMatcher(None, s_norm, r_norm).ratio()
            min_sim = 0.82 if rel == "same" else 0.84
            if sim >= min_sim:
                return ("AUTO_TYPO_VARIANT", r, f"artist_sim={sim:.2f}, dur={rel}")
            if len(s_norm) >= 7 and r_norm.startswith(s_norm + " ") and (rel == "same" or len(hits) == 1):
                return ("AUTO_TYPO_VARIANT", r, f"prefix_variant, dur={rel}")

        # Same collaborator set in different order or duplicated ("Eric Clapton & J.J. Cale" vs
        # "J.J. Cale & Eric Clapton", "Johnny Cash/Johnny Cash" vs "Johnny Cash")
        if s_toks and r_toks and s_toks == r_toks:
            return ("AUTO_COLLAB_REORDER", r, f"same_tokens={sorted(s_toks)}, dur={rel}")

        # Local path folder explicitly belongs to the destination artist (e.g. song title parsed as artist
        # inside "Neil Young Discography/..." or "Iron Maiden/...")
        if (
            rel == "same"
            and s_paths
            and len(s_toks) == 1
            and artist_track_counts[s_art.lower()] <= 2
            and len(r_norm) >= 4
            and r_norm != dbc._norm_title(r["title"])
        ):
            for p_str in s_paths:
                p_low = p_str.lower()
                if f"/{r_norm}/" in p_low or f"/{r_norm} discography" in p_low:
                    return ("AUTO_TYPO_VARIANT", r, f"path_matches_dest_artist={r_art!r}, dur={rel}")

    # 2. Check for subset collaborations (e.g. "Miles Davis & John Coltrane" vs "Miles Davis")
    for r, rel in hits:
        r_toks = _artist_tokens(r["artist"])
        if s_toks and r_toks and len(s_toks) != len(r_toks) and (s_toks < r_toks or r_toks < s_toks):
            if rel == "same" or len(hits) == 1:
                return ("REVIEW_COLLAB_SUBSET", r, f"collab_subset, dur={rel}")

    # 3. Multiple distinct destination artists with no artist-name match -> ambiguous title
    distinct_dest_artists = {_norm_artist_str(r["artist"]) for r, _ in hits}
    if len(distinct_dest_artists) > 1:
        same_dur_hits = [(r, rel) for r, rel in hits if rel == "same"]
        same_dur_artists = {_norm_artist_str(r["artist"]) for r, _ in same_dur_hits}
        if len(same_dur_artists) != 1:
            return ("SKIP_COVER_OR_AMBIGUOUS", None, f"ambiguous_artists={sorted(distinct_dest_artists)}")
        hits = same_dur_hits

    r, rel = hits[0]

    # 4. Shared source URL is decisive
    s_urls = shared_urls.get(src["track_id"], set())
    r_urls = shared_urls.get(r["track_id"], set())
    if s_urls and r_urls and (s_urls & r_urls):
        return ("AUTO_CHANNEL_UPLOADER", r, "shared_source_url")

    # 5. Channel / uploader handle vs cover or multi-artist collaboration:
    # Multi-artist credits ("Alan Walker, K-391 & Tungevaag", "Tony Bennett & Willie Nelson")
    # are often 'unmatched' as a combined string in artist_lookups, so never treat a
    # multi-artist credit (len(s_toks) > 1) as a channel uploader when its tokens are
    # unrelated to the destination artist.
    is_unmatched = src["status"] == "unmatched"
    is_local = bool(s_paths)
    n_tracks = artist_track_counts[s_art.lower()]
    looks_like_channel = bool(_CHANNEL_HINT.search(s_art))
    is_multi_artist = len(s_toks) > 1

    if is_unmatched and not is_local and not is_multi_artist and (looks_like_channel or n_tracks <= 2):
        norm_t = dbc._norm_title(src["title"])
        if rel == "same" or looks_like_channel or len(norm_t.split()) >= 2 or len(norm_t) >= 6:
            return ("AUTO_CHANNEL_UPLOADER", r, f"unmatched_uploader(tracks={n_tracks}), dur={rel}")

    return (
        "SKIP_COVER_OR_AMBIGUOUS",
        r,
        f"status={src['status']!r}, tracks={n_tracks}, local={is_local}, multi={is_multi_artist}, dur={rel}",
    )


def find_and_classify(conn):
    tracks = conn.execute(
        """
        SELECT t.track_id, t.artist, t.title, t.duration, l.status
        FROM tracks t
        LEFT JOIN artist_lookups l ON l.artist_text = t.artist
        WHERE length(trim(t.artist)) > 0
          AND t.artist !~ '^[0-9]{1,3}$'
        ORDER BY t.track_id
        """
    ).fetchall()

    src_rows = conn.execute("SELECT track_id, kind, url FROM sources").fetchall()
    local_paths_by_tid: dict[int, list[str]] = defaultdict(list)
    shared_urls: dict[int, set[str]] = defaultdict(set)
    for sr in src_rows:
        u = sr["url"] or ""
        if sr["kind"] == "local" or u.startswith("/"):
            local_paths_by_tid[sr["track_id"]].append(u)
        elif u:
            shared_urls[sr["track_id"]].add(u)

    artist_track_counts: Counter = Counter(t["artist"].lower() for t in tracks)

    resolved = [t for t in tracks if is_valid_resolved_artist(t["artist"], t["status"])]
    suspect = [t for t in tracks if not is_valid_resolved_artist(t["artist"], t["status"])]

    by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in resolved:
        key = dbc._norm_title(t["title"])[:6]
        if key:
            by_key[key].append(t)

    buckets: dict[str, list[tuple[dict[str, Any], dict[str, Any] | None, str, list[tuple[dict[str, Any], str]]]]] = defaultdict(list)
    for s in suspect:
        key = dbc._norm_title(s["title"])[:6]
        if not key:
            continue
        hits: list[tuple[dict[str, Any], str]] = []
        for r in by_key.get(key, ()):
            if r["artist"].lower() == s["artist"].lower():
                continue
            if not dbc.same_version(s["title"], r["title"]):
                continue
            if not dbc.compatible_parts(s["title"], r["title"]):
                continue
            rel = dbc.duration_relation(s["duration"], r["duration"])
            if rel == "different":
                continue
            threshold = (
                dbc.TITLE_SIMILARITY if rel == "same" else dbc.TITLE_SIMILARITY_NO_DURATION
            )
            if dbc.are_titles_similar(s["title"], r["title"], threshold):
                hits.append((r, rel))
        if not hits:
            continue
        cat, chosen, reason = classify_pair(
            s, hits, artist_track_counts, local_paths_by_tid, shared_urls
        )
        buckets[cat].append((s, chosen, reason, hits))

    return tracks, buckets


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--apply",
        action="store_true",
        help="apply safe repairs (numeric track-number artists + AUTO_* categories)",
    )
    args = ap.parse_args(argv)

    with localcache.connect() as conn:
        renamed_num, merged_num = repair_numeric_artist_tracks(conn, apply=args.apply)
        verb_num = "Applied" if args.apply else "Would apply"
        print(
            f"=== Numeric Track-Number Artists: {verb_num} {renamed_num} rename(s), "
            f"{merged_num} merge(s) ===\n"
        )

        tracks, buckets = find_and_classify(conn)
        auto_cats = ("AUTO_TYPO_VARIANT", "AUTO_COLLAB_REORDER", "AUTO_CHANNEL_UPLOADER")
        all_cats = (*auto_cats, "REVIEW_COLLAB_SUBSET", "SKIP_COVER_OR_AMBIGUOUS")

        for cat in all_cats:
            rows = buckets.get(cat, [])
            print(f"=== {cat}: {len(rows)} ===")
            for s, chosen, reason, hits in sorted(rows, key=lambda x: x[0]["artist"].lower()):
                print(
                    f"  {s['track_id']:>6} {s['artist']!r} - {s['title']!r} "
                    f"[{s['duration']}] ({reason})"
                )
                if chosen is not None:
                    print(
                        f"      -> {chosen['track_id']:>6} {chosen['artist']!r} - "
                        f"{chosen['title']!r} [{chosen['duration']}]"
                    )
                else:
                    for r, rel in hits:
                        print(
                            f"      ?  {r['track_id']:>6} {r['artist']!r} - "
                            f"{r['title']!r} [{r['duration']}] dur={rel}"
                        )
            print()

        if args.apply:
            merged = 0
            for cat in auto_cats:
                for s, chosen, reason, _ in buckets.get(cat, []):
                    if chosen is None:
                        continue
                    with conn.transaction():
                        dbc.merge_tracks(s["track_id"], chosen["track_id"], conn)
                    merged += 1
            conn.commit()
            print(
                f"Done: {renamed_num} numeric artist(s) renamed, "
                f"{merged_num} numeric artist track(s) merged, "
                f"{merged} cross-artist duplicate(s) merged."
            )
        else:
            total_auto = sum(len(buckets.get(c, [])) for c in auto_cats)
            print(
                f"Dry run: {renamed_num} rename(s) + {merged_num + total_auto} safe merge(s) "
                f"ready. Re-run with --apply to execute."
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
