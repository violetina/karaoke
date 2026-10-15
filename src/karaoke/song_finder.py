"""Find what is audible, and make sure the library can place it.

Behind the graph's "Find with songrec" button. Three steps:

1. **Identify.** A running radio listener already knows the song, so its
   answer is reused rather than starting a second recogniser -- two songrec
   processes on one microphone just take turns failing. Otherwise songrec
   listens: to the output monitor when a desktop player is playing (what this
   machine plays is identified more reliably there than through the room), to
   the microphone when nothing is.
2. **Match** the result against the library.
3. **Process.** A song the library lacks, or one missing the analysis the
   graph needs (key, chords, search document), is queued for the full
   post-processing chain, which also finds a source for an unknown song.
   :func:`status` reports when it is ready to be placed.
"""
from __future__ import annotations

import threading
from typing import Any, Optional

from .logger import log

#: Steps whose absence leaves a track unplaceable in the graph. Lyric timing
#: steps are not among them: the graph never reads lyrics.
GRAPH_STEPS = ("analysis", "chords", "vectors")

#: One recogniser at a time: a second click while songrec listens would only
#: compete with the first for the same audio.
_identify_lock = threading.Lock()


class Busy(RuntimeError):
    """An identification is already running."""


def _listen(source: str) -> tuple[Optional[Any], str]:
    """Run songrec. Returns (SongRef or None, the source actually used)."""
    from . import identify, playerctl

    if source == "auto":
        try:
            source = "output" if playerctl.playing_player() else "mic"
        except Exception:
            source = "mic"
    ref = identify.identify_live(mic=(source == "mic"), timeout=20)
    return ref, source


def identify(source: str = "auto") -> dict[str, Any]:
    """Identify the audible song and report where the library stands on it.

    ``source`` is ``auto``, ``mic`` or ``output``. Never raises for a miss --
    a miss is a result -- but raises :class:`Busy` while another
    identification runs.
    """
    from . import live_state

    live = None
    for name in live_state.SOURCES:
        live = live_state.read(name)
        if live:
            break
    if live is not None:
        if not live.get("title"):
            # Radio holds the mic and has not matched yet. Listening as well
            # would only fail for both.
            return {"status": "listening", "via": live["source"],
                    "message": "Radio mode is listening; no match yet"}
        artist, title, via = live.get("artist") or "", live["title"], live["source"]
    else:
        if not _identify_lock.acquire(blocking=False):
            raise Busy("songrec is already listening")
        try:
            ref, via = _listen(source)
        finally:
            _identify_lock.release()
        if ref is None or not ref.title:
            return {"status": "no_match", "via": via,
                    "message": "songrec heard nothing it recognised"}
        artist, title = ref.artist or "", ref.title

    out = locate(artist, title)
    out["via"] = via
    return out


def locate(artist: str, title: str) -> dict[str, Any]:
    """Match a song to the library, queueing whatever the graph still needs."""
    from . import localcache
    from .postprocess_queue import needs_postprocessing

    with localcache.connect() as conn:
        track_id = localcache.find_track_id_relaxed(artist, title, conn)
        pending = needs_postprocessing(track_id, conn) if track_id is not None else []
    missing = [p for p in pending if p in GRAPH_STEPS]
    out: dict[str, Any] = {"artist": artist, "title": title,
                           "track_id": track_id, "pending": missing}
    if track_id is not None and not missing:
        out["status"] = "found"
        return out
    if _queue_full(artist, title):
        out["status"] = "queued"
    else:
        out["status"] = "found" if track_id is not None else "unknown"
        out["message"] = "could not queue processing"
    return out


def _queue_full(artist: str, title: str) -> bool:
    """Queue the whole post-processing chain, downloads included."""
    try:
        from .tasks import enqueue_postprocess

        # full: a bounded, user-requested run may pull audio for harmony too,
        # which the playback-driven path deliberately does not.
        enqueue_postprocess({"artist": artist, "title": title, "url": "", "full": True})
        log.info("song_finder: queued full processing for %s - %s", artist, title)
        return True
    except Exception:
        log.warning("song_finder: could not queue %s - %s", artist, title, exc_info=True)
        return False


def status(artist: str, title: str) -> dict[str, Any]:
    """Whether a queued song can be placed in the graph yet."""
    from . import localcache
    from .postprocess_queue import needs_postprocessing

    with localcache.connect() as conn:
        track_id = localcache.find_track_id_relaxed(artist, title, conn)
        if track_id is None:
            return {"track_id": None, "ready": False, "pending": list(GRAPH_STEPS)}
        missing = [p for p in needs_postprocessing(track_id, conn) if p in GRAPH_STEPS]
    return {"track_id": track_id, "ready": not missing, "pending": missing}


def ensure_processed(artist: str, title: str) -> str:
    """Make a heard song a complete library track. Returns what was done.

    For radio mode, on each new song. A song the library lacks, or one
    missing what search and the graph need, gets the *full* chain: the
    playback path's light chain fetches audio only for key/BPM or lyric sync,
    so a track missing just its chords stayed that way, and one with no
    lyrics was never added at all. A song that is already complete goes the
    old way, for the lyric-timing gaps the light chain still covers.

    ``queued-full`` | ``queued`` | ``complete`` | ``failed``.
    """
    out = locate(artist, title)
    if out["status"] == "queued":
        return "queued-full"
    if out["status"] != "found":
        return "failed"
    from .postprocess_queue import enqueue_if_needed

    return "queued" if enqueue_if_needed(artist, title, "") else "complete"
