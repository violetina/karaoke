"""Tests for URL-based lyric resolution and gap recording."""

from karaoke import detect, localcache
from karaoke.detect import Detection
from karaoke.lyrics import Lyrics


def test_resolve_lyrics_prefers_url_match(tmp_path):
    conn = localcache.connect(tmp_path / "k.db")
    localcache.add_track_and_lyrics(
        "Ren", "Hi Ren",
        Lyrics(synced_raw="[00:01.00] hi", source="lrclib", lines=[(1.0, "hi")]),
        url="https://youtu.be/xyz", kind="youtube", conn=conn,
    )
    det = Detection("scan", "firefox", "wrong", "wrong", "https://youtu.be/xyz")
    artist, title, lyrics = detect.resolve_lyrics(det, conn)
    assert (artist, title) == ("Ren", "Hi Ren")
    assert lyrics is not None and lyrics.has_synced


def test_resolve_lyrics_miss_returns_none(tmp_path):
    conn = localcache.connect(tmp_path / "k.db")
    det = Detection("scan", "firefox", "Nobody", "Nothing", "")
    artist, title, lyrics = detect.resolve_lyrics(det, conn)
    assert (artist, title) == ("Nobody", "Nothing")
    assert lyrics is None


def test_resolve_lyrics_strips_remaster_suffix(tmp_path):
    # The cached track has a plain title; the browser reports a decorated one.
    conn = localcache.connect(tmp_path / "k.db")
    localcache.add_track_and_lyrics(
        "Brant Bjork", "Too Many Chiefs... Not Enough Indians",
        Lyrics(synced_raw="[00:01.00] hey", source="lrclib", lines=[(1.0, "hey")]),
        conn=conn,
    )
    det = Detection(
        "scan", "chromium", "Brant Bjork",
        "Too Many Chiefs... Not Enough Indians (2019 Remastered)", "",
    )
    artist, title, lyrics = detect.resolve_lyrics(det, conn)
    assert title == "Too Many Chiefs... Not Enough Indians"
    assert lyrics is not None and lyrics.has_synced


def test_record_gap_logs_gap_and_source(tmp_path):
    conn = localcache.connect(tmp_path / "k.db")
    det = Detection("scan", "firefox", "New", "Song", "https://youtu.be/new")
    detect.record_gap(det, conn)

    gaps = conn.execute(
        "SELECT artist, title FROM lyric_gaps WHERE status = 'pending'"
    ).fetchall()
    assert ("New", "Song") in [(r["artist"], r["title"]) for r in gaps]

    found = localcache.find_track_by_url("https://youtu.be/new", conn)
    assert found is not None
    assert found[1:] == ("New", "Song")


def test_resolve_lyrics_empty_artist_fallback(tmp_path):
    conn = localcache.connect(tmp_path / "k.db")
    localcache.add_track_and_lyrics(
        "Kiki Rockwell", "Cup Runneth Over",
        Lyrics(synced_raw="[00:01.00] hi", source="lrclib", lines=[(1.0, "hi")]),
        conn=conn,
    )
    # Detection from browser has empty artist and no URL, but has the correct title
    det = Detection("scan", "chromium", "", "Cup Runneth Over", "")
    artist, title, lyrics = detect.resolve_lyrics(det, conn)
    assert (artist, title) == ("Kiki Rockwell", "Cup Runneth Over")
    assert lyrics is not None and lyrics.has_synced

