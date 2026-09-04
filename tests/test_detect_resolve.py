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



# --- relaxed matching ------------------------------------------------------
#
# Radio mode (songrec) caches under the full credit and a decorated title,
# while a browser reports a plainer spelling — so an exact lookup made tracks
# the radio had already fetched look absent to the TUI.

def _seed(conn, artist, title, *, synced=True):
    from karaoke import localcache
    from karaoke.lyrics import Lyrics
    localcache.add_track_and_lyrics(
        artist, title,
        Lyrics(plain="w", synced_raw="[00:01.00] w" if synced else "",
               source="lrclib"),
        conn=conn)


def _resolve(conn, artist, title):
    from karaoke import detect
    return detect.resolve_lyrics(
        detect.Detection(mode="scan", artist=artist, title=title), conn)


def test_relaxed_match_ignores_punctuation_and_case(tmp_path):
    from karaoke import localcache
    conn = localcache.connect(tmp_path / "k.db")
    _seed(conn, "Otis Redding", "(Sittin' on) The Dock of the Bay")

    _, _, ly = _resolve(conn, "Otis Redding", "(Sittin On) The Dock Of The Bay")
    assert ly is not None and ly.has_synced


def test_relaxed_match_ignores_a_bracketed_edition(tmp_path):
    from karaoke import localcache
    conn = localcache.connect(tmp_path / "k.db")
    _seed(conn, "Otis Redding", "Dock of the Bay [2020 Remaster]")

    _, _, ly = _resolve(conn, "Otis Redding", "Dock of the Bay")
    assert ly is not None and ly.has_synced


def test_relaxed_match_accepts_a_shorter_artist_credit(tmp_path):
    from karaoke import localcache
    conn = localcache.connect(tmp_path / "k.db")
    _seed(conn, "James Brown & The Famous Flames", "Man's World")

    artist, _, ly = _resolve(conn, "James Brown", "Man's World")
    assert ly is not None and ly.has_synced
    assert artist == "James Brown & The Famous Flames"   # canonical name wins


def test_relaxed_match_ignores_a_leading_the(tmp_path):
    from karaoke import localcache
    conn = localcache.connect(tmp_path / "k.db")
    _seed(conn, "The Mothers of Invention", "I'm Not Satisfied")

    _, _, ly = _resolve(conn, "Mothers of Invention", "I'm Not Satisfied")
    assert ly is not None


def test_relaxed_match_still_refuses_a_different_artist(tmp_path):
    """The guard that stops a loose title match grabbing the wrong song."""
    from karaoke import localcache
    conn = localcache.connect(tmp_path / "k.db")
    _seed(conn, "Otis Redding", "Dock of the Bay")

    _, _, ly = _resolve(conn, "Nirvana", "Dock of the Bay")
    assert ly is None


def test_relaxed_match_returns_nothing_for_an_absent_track(tmp_path):
    from karaoke import localcache
    conn = localcache.connect(tmp_path / "k.db")
    _seed(conn, "Otis Redding", "Dock of the Bay")

    _, _, ly = _resolve(conn, "Somebody", "A Song Not Here")
    assert ly is None
