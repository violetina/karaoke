"""Plain lyrics get a rhythm from a transcription.

The player's own lyrics panel supplies words and no timings, and LRCLIB
sometimes has only plain text. Words alone cannot drive a karaoke session, and
Whisper's words on sung audio are unreliable -- so its *timings* are taken and
the real words kept. lyric_align already did this for the console player; this
is the trigger that makes it happen for a track that arrives without timings.
"""
import pytest

from karaoke import localcache
from karaoke.postprocess_queue import needs_postprocessing


@pytest.fixture()
def conn(tmp_path):
    c = localcache.connect(tmp_path / "t.db")
    c.executescript("""
        INSERT INTO tracks (track_id, artist, title, duration) VALUES
            (1, 'A', 'Plain Only', 200.0),
            (2, 'B', 'Already Synced', 200.0),
            (3, 'C', 'No Lyrics At All', 200.0);
        INSERT INTO lyrics (track_id, kind, plain_lyrics, synced_lyrics) VALUES
            (1, 'approved', 'one line\ntwo line', ''),
            (2, 'approved', 'w', '[00:01.00] w');
        INSERT INTO sources (track_id, kind, url) VALUES
            (1, 'youtube', 'https://youtu.be/aaa'),
            (2, 'youtube', 'https://youtu.be/bbb');
    """)
    c.commit()
    yield c
    c.close()


# -- when the work is needed --------------------------------------------

def test_plain_lyrics_need_syncing(conn):
    assert "sync" in needs_postprocessing(1, conn)


def test_synced_lyrics_do_not(conn):
    pending = needs_postprocessing(2, conn)
    assert "sync" not in pending
    assert "timings" in pending      # word-level upgrade is a separate job


def test_a_track_with_no_lyrics_needs_no_sync(conn):
    """Nothing to align; that is a job for backfill, not for the worker."""
    assert "sync" not in needs_postprocessing(3, conn)


# -- doing the work -----------------------------------------------------

def test_alignment_keeps_the_real_words(conn, monkeypatch, tmp_path):
    """Whisper mishears sung audio -- "up to do" becomes "up to doom" -- so its
    words are discarded and only its timings used."""
    from karaoke import postprocess_worker as pw

    monkeypatch.setattr("karaoke.whisper_sync.transcribe_to_words",
                        lambda path, text=None: [])
    monkeypatch.setattr("karaoke.lyric_align.align_lyrics_to_lrc",
                        lambda plain, words, total_duration=None:
                        "[00:00.00] one line\n[00:05.00] two line")

    assert pw._run_sync(1, tmp_path / "audio.webm", conn) is True
    row = conn.execute("SELECT synced_lyrics, plain_lyrics, source FROM lyrics"
                       " WHERE track_id = 1").fetchone()
    assert "one line" in row["synced_lyrics"]
    assert row["plain_lyrics"] == "one line\ntwo line"   # untouched
    assert row["source"] == "whisper_aligned"


def test_a_track_with_no_plain_text_is_refused(conn, tmp_path):
    from karaoke import postprocess_worker as pw

    assert pw._run_sync(3, tmp_path / "audio.webm", conn) is False


def test_an_alignment_that_produces_nothing_is_not_stored(conn, monkeypatch,
                                                          tmp_path):
    """Storing an empty LRC would mark the track synced and stop anything from
    trying again."""
    from karaoke import postprocess_worker as pw

    monkeypatch.setattr("karaoke.whisper_sync.transcribe_to_words",
                        lambda path, text=None: [])
    monkeypatch.setattr("karaoke.lyric_align.align_lyrics_to_lrc",
                        lambda plain, words, total_duration=None: "")

    assert pw._run_sync(1, tmp_path / "audio.webm", conn) is False
    row = conn.execute("SELECT synced_lyrics FROM lyrics WHERE track_id = 1"
                       ).fetchone()
    assert not row["synced_lyrics"]


def test_a_transcription_failure_is_not_fatal(conn, monkeypatch, tmp_path):
    from karaoke import postprocess_worker as pw

    def boom(path, text=None):
        raise RuntimeError("no whisper here")

    monkeypatch.setattr("karaoke.whisper_sync.transcribe_to_words", boom)
    assert pw._run_sync(1, tmp_path / "audio.webm", conn) is False


# -- the trigger --------------------------------------------------------

def test_the_tui_queues_a_track_that_has_audio(conn, monkeypatch):
    from karaoke.tui import KaraokeTui

    sent = []
    monkeypatch.setattr("karaoke.postprocess_queue.enqueue_if_needed",
                        lambda a, t, u="", c=None: sent.append((a, t)) or True)
    app = KaraokeTui.__new__(KaraokeTui)
    app.call_from_thread = lambda fn, *a, **k: None
    app._enqueue_sync("A", "Plain Only", conn)
    assert sent == [("A", "Plain Only")]


def test_the_tui_does_not_queue_what_it_cannot_fetch(conn, monkeypatch):
    """The panel answers for plenty of tracks whose audio cannot be fetched;
    queueing those would fail on every retry."""
    from karaoke.tui import KaraokeTui

    conn.execute("DELETE FROM sources WHERE track_id = 1")
    conn.commit()
    monkeypatch.setattr("karaoke.postprocess_queue.enqueue_if_needed",
                        lambda *a, **k: pytest.fail("nothing to align against"))
    app = KaraokeTui.__new__(KaraokeTui)
    app.call_from_thread = lambda fn, *a, **k: None
    app._enqueue_sync("A", "Plain Only", conn)


def test_an_unknown_track_is_not_queued(conn, monkeypatch):
    from karaoke.tui import KaraokeTui

    monkeypatch.setattr("karaoke.postprocess_queue.enqueue_if_needed",
                        lambda *a, **k: pytest.fail("no such track"))
    app = KaraokeTui.__new__(KaraokeTui)
    app.call_from_thread = lambda fn, *a, **k: None
    app._enqueue_sync("Nobody", "Nothing", conn)
