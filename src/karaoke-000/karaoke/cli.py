"""Console entrypoints: karaoke, lyricsearch, music-index."""
from __future__ import annotations

import argparse
import sys
from typing import Optional

from .identify import SongRef, from_file, identify_live, parse_query


def _resolve(args) -> Optional[SongRef]:
    if args.file:
        return from_file(args.file)
    if args.spotify:
        from .spotify_client import SpotifyClient
        pb = SpotifyClient().current_playback()
        if not pb or not pb.title:
            print("Nothing playing on Spotify. Start a track first.", file=sys.stderr)
            return None
        print(f"Spotify: {pb.artist} - {pb.title}", file=sys.stderr)
        return SongRef(artist=pb.artist, title=pb.title,
                       duration=pb.duration_ms / 1000.0, source="spotify")
    if args.listen or args.output:
        print(f"Listening ({'output' if args.output else 'mic'})...", file=sys.stderr)
        ref = identify_live(mic=not args.output, timeout=args.timeout)
        if ref:
            print(f"Identified: {ref.artist} - {ref.title}", file=sys.stderr)
        return ref
    if args.query:
        return parse_query(" ".join(args.query))
    return None


def karaoke_main() -> int:
    ap = argparse.ArgumentParser(prog="karaoke", description="Terminal karaoke with synced lyrics")
    ap.add_argument("query", nargs="*", help="'Artist - Title' (or a title)")
    ap.add_argument("--file", "-f", help="local audio file (read tags)")
    ap.add_argument("--listen", "-l", action="store_true", help="identify room audio via mic")
    ap.add_argument("--output", "-o", action="store_true", help="identify laptop output audio")
    ap.add_argument("--spotify", "-s", action="store_true",
                    help="sync lyrics to the track currently playing on Spotify")
    ap.add_argument("--timeout", "-t", type=int, default=30, help="listen timeout secs")
    ap.add_argument("--no-cache", action="store_true", help="skip OpenSearch cache, always fetch")
    ap.add_argument("--transcribe", action="store_true",
                    help="if no LRCLIB lyrics, transcribe a local --file with Whisper")
    ap.add_argument("--force-transcribe", action="store_true",
                    help="always transcribe --file with Whisper (skip cache + LRCLIB)")
    ap.add_argument("--offset", type=float, default=0.0, help="lyric clock offset secs")
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="print lyrics instead of the live player")
    args = ap.parse_args()

    ref = _resolve(args)
    if not ref or not ref.title:
        ap.error("no song given. Use --file, --listen, or 'Artist - Title'.")

    from .player import get_synced, timeline_from_lyrics, render_lines, play

    ly = get_synced(ref.artist, ref.title, ref.album, ref.duration,
                    use_cache=not args.no_cache,
                    audio_path=ref.path,
                    transcribe=args.transcribe or args.force_transcribe,
                    force_transcribe=args.force_transcribe)
    tl = timeline_from_lyrics(ly)

    if not tl.lines:
        print(f"No synced lyrics for {ref.artist} - {ref.title} "
              f"(source={ly.source}).", file=sys.stderr)
        if ly.plain:
            print("\n--- plain lyrics ---\n" + ly.plain)
            return 0
        return 2

    if args.print_only:
        for t, text in tl.lines:
            print(f"[{int(t//60):02d}:{t%60:05.2f}] {text}")
        return 0

    if args.spotify:
        from .player import play_spotify_synced
        play_spotify_synced(tl, title=ref.title, artist=ref.artist, offset=args.offset)
        return 0

    # Live mic/output identification returns a position offset -> auto-sync to it.
    if ref.offset is not None and ref.offset_mono is not None:
        from .player import play_offset_synced
        play_offset_synced(tl, title=ref.title, artist=ref.artist,
                           offset=ref.offset, offset_mono=ref.offset_mono,
                           extra_latency=args.offset)
        return 0

    play(tl, title=ref.title, artist=ref.artist, offset=args.offset)
    return 0


def lyricsearch_main() -> int:
    ap = argparse.ArgumentParser(prog="lyricsearch",
                                 description="Find a song by what its lyrics mean")
    ap.add_argument("query", nargs="+", help="words / a phrase from the song")
    ap.add_argument("-k", type=int, default=5, help="number of results")
    ap.add_argument("--keyword", action="store_true", help="keyword search instead of semantic")
    args = ap.parse_args()

    from .search import keyword_search, semantic_search

    q = " ".join(args.query)
    hits = keyword_search(q, k=args.k) if args.keyword else semantic_search(q, k=args.k)
    if not hits:
        print("no matches", file=sys.stderr)
        return 1
    for h in hits:
        mark = "♪" if h.has_synced else " "
        print(f"{h.score:6.3f} {mark} {h.artist} - {h.title}  "
              f"[{h.source}{'/'+h.album if h.album else ''}]")
    return 0


def index_main() -> int:
    ap = argparse.ArgumentParser(prog="music-index",
                                 description="Scan a music library into OpenSearch")
    ap.add_argument("--dir", help="music dir (default MUSIC_DIR)")
    ap.add_argument("--force", action="store_true", help="re-fetch even if indexed")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    from pathlib import Path
    from .scanner import scan

    stats = scan(Path(args.dir) if args.dir else None,
                 force=args.force, limit=args.limit)
    print(f"\nseen={stats.seen} indexed={stats.indexed} skipped={stats.skipped} "
          f"synced={stats.with_synced} errors={stats.errors}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(karaoke_main())
