"""Tests for sourcing tracks that have lyrics but no URL (offline)."""
from karaoke import find_sources as fs
from karaoke.find_sources import Candidate


def _cand(duration=None, lyric_end=None):
    return Candidate(track_id=1, artist="Kyuss", title="Green Machine",
                     duration=duration, lyric_end=lyric_end)


def _result(url="https://youtu.be/x", title="Kyuss - Green Machine",
            duration=200.0, uploader="Kyuss - Topic"):
    return {"url": url, "title": title, "duration": duration, "uploader": uploader}


# --- the length gate -------------------------------------------------------

def test_a_song_cannot_be_shorter_than_its_own_lyrics():
    """The last synced lyric is real evidence even with no stored duration."""
    cand = _cand(lyric_end=180.0)
    assert not fs.plausible_length(120.0, cand)
    assert fs.plausible_length(200.0, cand)


def test_an_album_rip_is_rejected_by_the_upper_bound():
    cand = _cand(lyric_end=180.0)
    assert not fs.plausible_length(4200.0, cand)      # 70-minute upload


def test_the_gate_abstains_without_evidence():
    """No duration on either side means no opinion, not a rejection."""
    assert fs.plausible_length(None, _cand(lyric_end=180.0))
    assert fs.plausible_length(200.0, _cand())


# --- selection -------------------------------------------------------------

def test_exact_duration_uses_the_tight_tolerance(monkeypatch):
    """With a real duration the picker's own gate is the right one."""
    seen = {}
    monkeypatch.setattr(fs.youtube, "search", lambda q, limit=5: [_result()])

    def fake_select(results, artist, title, reference):
        seen["reference"] = reference
        return results[0]
    monkeypatch.setattr(fs, "select_best_source", fake_select)

    fs.find_for(_cand(duration=205.0, lyric_end=180.0))
    assert seen["reference"] == 205.0


def test_without_a_duration_candidates_are_prefiltered(monkeypatch):
    """Wrong-length results are dropped before the picker sees them."""
    results = [_result(duration=60.0), _result(duration=200.0),
               _result(duration=4200.0)]
    monkeypatch.setattr(fs.youtube, "search", lambda q, limit=5: results)
    passed = {}

    def fake_select(rs, artist, title, reference):
        passed["durations"] = [r["duration"] for r in rs]
        return rs[0] if rs else None
    monkeypatch.setattr(fs, "select_best_source", fake_select)

    fs.find_for(_cand(lyric_end=180.0))
    assert passed["durations"] == [200.0]


def test_no_results_is_not_an_error(monkeypatch):
    monkeypatch.setattr(fs.youtube, "search", lambda q, limit=5: [])
    assert fs.find_for(_cand()) is None


def test_a_failed_search_is_survivable(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("rate limited")
    monkeypatch.setattr(fs.youtube, "search", boom)
    assert fs.find_for(_cand()) is None


# --- selection over the real library ---------------------------------------

def test_needs_source_finds_lyrics_without_a_url(tmp_path):
    from karaoke import localcache
    from karaoke.lyrics import Lyrics

    conn = localcache.connect(tmp_path / "k.db")
    lrc = "[00:01.00] a\n[03:00.00] b"
    localcache.add_track_and_lyrics("A", "Needs Source",
                                    Lyrics(plain="a", synced_raw=lrc,
                                           source="lrclib"), conn=conn)
    localcache.add_track_and_lyrics("B", "Has Source",
                                    Lyrics(plain="a", synced_raw=lrc,
                                           source="lrclib"),
                                    url="https://www.youtube.com/watch?v=_3tkup9b-iM",
                                    conn=conn)

    got = fs.needs_source(conn)
    assert [c.title for c in got] == ["Needs Source"]
    assert got[0].lyric_end == 180.0          # last lyric, used as the floor


def test_needs_source_skips_tracks_with_no_lyrics(tmp_path):
    from karaoke import localcache
    from karaoke.lyrics import Lyrics

    conn = localcache.connect(tmp_path / "k.db")
    localcache.add_track_and_lyrics("A", "No Lyrics", Lyrics(), conn=conn)
    assert fs.needs_source(conn) == []


def test_dry_run_stores_nothing(tmp_path, monkeypatch):
    from karaoke import localcache
    from karaoke.lyrics import Lyrics

    conn = localcache.connect(tmp_path / "k.db")
    localcache.add_track_and_lyrics("Kyuss", "Green Machine",
                                    Lyrics(plain="a", synced_raw="[00:01.00] a",
                                           source="lrclib"), conn=conn)
    monkeypatch.setattr(fs.youtube, "search", lambda q, limit=5: [_result()])

    found, missed = fs.run(dry_run=True, pause=0, conn=conn)
    assert (found, missed) == (1, 0)
    assert fs.needs_source(conn)              # still unsourced


def test_a_real_run_stores_the_url(tmp_path, monkeypatch):
    from karaoke import localcache
    from karaoke.lyrics import Lyrics

    conn = localcache.connect(tmp_path / "k.db")
    localcache.add_track_and_lyrics("Kyuss", "Green Machine",
                                    Lyrics(plain="a", synced_raw="[00:01.00] a",
                                           source="lrclib"), conn=conn)
    monkeypatch.setattr(fs.youtube, "search",
                        lambda q, limit=5: [_result(url="https://youtu.be/abc")])

    fs.run(pause=0, conn=conn)
    assert fs.needs_source(conn) == []        # now sourced


def test_an_early_last_lyric_does_not_cap_the_song_absurdly():
    """A bare multiple collapsed here: a 1s last lyric capped the song at 3s.

    Songs with an intro-only vocal or a long instrumental tail are real.
    """
    cand = _cand(lyric_end=1.0)
    assert fs.plausible_length(200.0, cand)
    assert not fs.plausible_length(4200.0, cand)     # still rejects album rips


def test_the_ceiling_never_admits_a_full_album():
    for lyric_end in (60.0, 180.0, 400.0, 900.0):
        assert not fs.plausible_length(4200.0, _cand(lyric_end=lyric_end)), lyric_end


def test_a_short_youtube_link_counts_as_sourced(tmp_path):
    """Matching only "watch?v=" would re-search these on every run."""
    from karaoke import localcache
    from karaoke.lyrics import Lyrics

    conn = localcache.connect(tmp_path / "k.db")
    localcache.add_track_and_lyrics("A", "Short Link",
                                    Lyrics(plain="a", synced_raw="[00:01.00] a",
                                           source="lrclib"),
                                    url="https://youtu.be/_3tkup9b-iM", conn=conn)
    assert fs.needs_source(conn) == []
