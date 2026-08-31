"""Console entrypoints: karaoke, lyricsearch, music-index."""
from __future__ import annotations

import argparse
import sys
from typing import Optional

from .identify import SongRef, from_file, identify_live, parse_query
from .player import DEFAULT_LEAD_S


def _resolve(args) -> Optional[SongRef]:
    if args.file:
        return from_file(args.file)
    if args.youtube:
        from .youtube import resolve_youtube
        print("Resolving YouTube video…", file=sys.stderr)
        try:
            ref = resolve_youtube(
                args.youtube, download=args.download,
                cookies_from_browser=args.cookies_from_browser,
                cookies_file=args.cookies,
            )
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return None
        if ref:
            print(f"YouTube: {ref.artist} - {ref.title}"
                  f"{' (audio downloaded)' if ref.path else ''}", file=sys.stderr)
        else:
            print("Could not parse an artist/title from that video.", file=sys.stderr)
        return ref
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


def karaoke_main(argv: Optional[list[str]] = None) -> int:
    """Run the `karaoke` CLI for lookup, printing, playback and live sync modes."""
    ap = argparse.ArgumentParser(prog="karaoke", description="Terminal karaoke with synced lyrics")
    ap.add_argument("query", nargs="*", help="'Artist - Title' (or a title)")
    ap.add_argument("--file", "-f", help="local audio file (read tags)")
    ap.add_argument("--listen", "-l", action="store_true", help="identify room audio via mic")
    ap.add_argument("--output", "-o", action="store_true", help="identify laptop output audio")
    ap.add_argument("--youtube", "-y", metavar="URL",
                    help="karaoke a YouTube video URL (yt-dlp metadata -> lyrics)")
    ap.add_argument("--download", action="store_true",
                    help="with --youtube: download audio so Whisper/beats can run")
    ap.add_argument("--cookies-from-browser", metavar="BROWSER",
                    help="with --youtube: use a logged-in browser's cookies for "
                         "Premium-quality/library access (e.g. firefox, chrome, "
                         "'firefox:PROFILE')")
    ap.add_argument("--cookies", metavar="FILE",
                    help="with --youtube: cookies.txt for authenticated access")
    ap.add_argument("--spotify", "-s", action="store_true",
                    help="sync lyrics to the track currently playing on Spotify")
    ap.add_argument("--radio", "-r", action="store_true",
                    help="continuously follow live audio (mic): re-identify + re-sync as songs change")
    ap.add_argument("--reidentify", type=float, default=30.0,
                    help="seconds between re-identifications in --radio mode (default 30)")
    ap.add_argument("--timeout", "-t", type=int, default=30, help="listen timeout secs")
    ap.add_argument("--no-cache", action="store_true", help="skip OpenSearch cache, always fetch")
    ap.add_argument("--transcribe", action="store_true",
                    help="if no LRCLIB lyrics, transcribe a local --file with Whisper")
    ap.add_argument("--force-transcribe", action="store_true",
                    help="always transcribe --file with Whisper (skip cache + LRCLIB)")
    ap.add_argument("--offset", type=float, default=0.0, help="lyric clock offset secs")
    ap.add_argument("--lead", type=float, default=None,
                    help="forward pre-bias (secs) for mic/radio sync to offset the "
                         f"~10s recognition window (default {DEFAULT_LEAD_S}; use 0 to disable)")
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="print lyrics instead of the live player")
    ap.add_argument("--no-beats", action="store_true",
                    help="skip librosa beat detection in --file mode (use per-line pulse)")
    args = ap.parse_args(argv)

    # Forward pre-bias for live recognition (mic/radio) modes only. Spotify has an
    # exact position and text/file modes start from a keypress, so no lead there.
    lead = DEFAULT_LEAD_S if args.lead is None else args.lead

    # Continuous radio mode: self-contained loop, no single-song resolution.
    if args.radio:
        from .player import play_radio_synced
        play_radio_synced(mic=not args.output, reidentify_interval=args.reidentify,
                          extra_latency=args.offset + lead, listen_timeout=args.timeout)
        return 0

    # Continuous Spotify mode: follow playback track-by-track. Tracks with no
    # synced lyrics are skipped (wait for the next song) instead of exiting.
    # (--print falls through to the one-shot resolve+dump path below.)
    if args.spotify and not args.print_only:
        from .player import play_spotify_loop
        play_spotify_loop(offset=args.offset, use_cache=not args.no_cache)
        return 0

    ref = _resolve(args)
    if not ref or not ref.title:
        ap.error("no song given. Use --file, --listen, or 'Artist - Title'.")

    from .player import get_synced, timeline_from_lyrics, render_lines, play

    stats_mode = ref.source  # file | query | songrec | spotify
    if args.listen:
        stats_mode = "listen"
    elif args.output:
        stats_mode = "output"

    ly = get_synced(ref.artist, ref.title, ref.album, ref.duration,
                    use_cache=not args.no_cache,
                    audio_path=ref.path,
                    transcribe=args.transcribe or args.force_transcribe,
                    force_transcribe=args.force_transcribe,
                    stats_mode=stats_mode)
    tl = timeline_from_lyrics(ly)

    if not args.print_only:
        from . import localcache
        localcache.log_event(
            stats_mode, "play", artist=ref.artist, title=ref.title,
            source=ly.source, has_synced=bool(tl.lines),
        )

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

    # Live mic/output identification returns a position offset -> auto-sync to it.
    if ref.offset is not None and ref.offset_mono is not None:
        from .player import play_offset_synced
        play_offset_synced(tl, title=ref.title, artist=ref.artist,
                           offset=ref.offset, offset_mono=ref.offset_mono,
                           extra_latency=args.offset + lead)
        return 0

    # File/text mode: if we have a local audio file, detect real beats so the
    # display flashes on the beat (needs librosa; degrades to per-line pulse).
    beat_times = None
    if ref.path and not args.no_beats:
        from .beats import detect_beats
        print("Detecting beats (first run may take a moment)…", file=sys.stderr)
        bpm, beat_times = detect_beats(ref.path)
        if beat_times:
            print(f"Beat track: {bpm:.0f} BPM, {len(beat_times)} beats.", file=sys.stderr)
        else:
            print("No beats detected (librosa missing or unreadable audio); "
                  "using per-line pulse.", file=sys.stderr)

    play(tl, title=ref.title, artist=ref.artist, offset=args.offset,
         beat_times=beat_times)
    return 0


def karaoke_yt_main(argv: Optional[list[str]] = None) -> int:
    """Run the `karaoke-yt` CLI: karaoke a YouTube URL directly.

    A thin, friendlier front-end over ``karaoke --youtube``: the URL is the
    positional argument (no flag needed) and it translates the download/print/
    transcribe/offset options into a ``karaoke_main`` invocation, so the whole
    downstream lyrics -> render pipeline is reused with zero duplication.
    """
    ap = argparse.ArgumentParser(
        prog="karaoke-yt",
        description="Karaoke a YouTube video URL (yt-dlp metadata -> synced lyrics)",
    )
    ap.add_argument("url", help="YouTube video URL")
    ap.add_argument("--download", "-d", action="store_true",
                    help="download audio so Whisper/beats can run (unlike Spotify)")
    ap.add_argument("--transcribe", action="store_true",
                    help="if no LRCLIB lyrics, transcribe downloaded audio with Whisper "
                         "(implies --download)")
    ap.add_argument("--print", dest="print_only", action="store_true",
                    help="print lyrics instead of the live player")
    ap.add_argument("--no-cache", action="store_true",
                    help="skip caches, always fetch fresh")
    ap.add_argument("--no-beats", action="store_true",
                    help="skip librosa beat detection on downloaded audio")
    ap.add_argument("--cookies-from-browser", metavar="BROWSER",
                    help="use a logged-in browser's cookies for Premium-quality/"
                         "library access (e.g. firefox, chrome, 'firefox:PROFILE')")
    ap.add_argument("--cookies", metavar="FILE",
                    help="cookies.txt for authenticated (Premium/library) access")
    ap.add_argument("--offset", type=float, default=0.0, help="lyric clock offset secs")
    args = ap.parse_args(argv)

    # Transcription needs local audio, so it forces a download.
    download = args.download or args.transcribe

    forwarded: list[str] = ["--youtube", args.url]
    if download:
        forwarded.append("--download")
    if args.transcribe:
        forwarded.append("--transcribe")
    if args.print_only:
        forwarded.append("--print")
    if args.no_cache:
        forwarded.append("--no-cache")
    if args.no_beats:
        forwarded.append("--no-beats")
    if args.cookies_from_browser:
        forwarded += ["--cookies-from-browser", args.cookies_from_browser]
    if args.cookies:
        forwarded += ["--cookies", args.cookies]
    if args.offset:
        forwarded += ["--offset", str(args.offset)]

    return karaoke_main(forwarded)


def lyricsearch_main() -> int:
    """Run the `lyricsearch` CLI for semantic or keyword lyric search."""
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
    """Run the `music-index` CLI to scan local audio into OpenSearch."""
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


def stats_main() -> int:
    """Run the `karaoke-stats` CLI to report play and radio-discovery stats."""
    ap = argparse.ArgumentParser(
        prog="karaoke-stats",
        description="Play counts and radio-discovery stats from the local cache",
    )
    ap.add_argument("-n", "--limit", type=int, default=10, help="rows in top lists")
    ap.add_argument("--days", type=float, default=None,
                    help="only count events in the last N days")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = ap.parse_args()

    import time as _time
    from . import localcache

    since = _time.time() - args.days * 86400 if args.days else None
    s = localcache.summarize(limit=args.limit, since=since)

    if args.json:
        import json
        print(json.dumps({
            "total_events": s.total_events, "plays": s.plays,
            "discoveries": s.discoveries, "cache_hits": s.cache_hits,
            "cache_misses": s.cache_misses, "cache_hit_rate": round(s.cache_hit_rate, 3),
            "distinct_tracks": s.distinct_tracks, "distinct_artists": s.distinct_artists,
            "top_tracks": [{"artist": a, "title": t, "plays": n} for a, t, n in s.top_tracks],
            "top_artists": [{"artist": a, "plays": n} for a, n in s.top_artists],
            "by_mode": [{"mode": m, "plays": n} for m, n in s.by_mode],
        }, indent=2))
        return 0

    window = f" (last {args.days:g} days)" if args.days else ""
    print(f"Karaoke stats{window}")
    print(f"  plays={s.plays}  discoveries={s.discoveries}  "
          f"tracks={s.distinct_tracks}  artists={s.distinct_artists}")
    print(f"  local-cache hits={s.cache_hits}  misses={s.cache_misses}  "
          f"hit-rate={s.cache_hit_rate*100:.0f}%")
    if s.by_mode:
        print("\n  by mode:")
        for mode, n in s.by_mode:
            print(f"    {mode:10s} {n}")
    if s.top_tracks:
        print("\n  top tracks:")
        for a, t, n in s.top_tracks:
            label = f"{a} - {t}" if a else t
            print(f"    {n:4d}  {label}")
    if s.top_artists:
        print("\n  top artists:")
        for a, n in s.top_artists:
            print(f"    {n:4d}  {a}")
    if s.total_events == 0:
        print("  (no events yet — play something with `karaoke` first)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(karaoke_main())
