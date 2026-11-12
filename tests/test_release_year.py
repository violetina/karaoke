"""Release year per album: title parsing, release-group choice, and storage."""
from __future__ import annotations

import pytest

from karaoke import localcache, release_year as ry


@pytest.mark.parametrize("title,expected", [
    ("Cmere (2004)", (2004, "title")),
    ("I'm Afraid Of Americans (2021 Remaster)", (2021, "title-remaster")),
    ("Heroes - 2017 Remix", (2017, "title-remaster")),
    ("Watch (Remastered 2009)", (2009, "title-remaster")),
    ("Monsters Of Rock Festival Donington 1988", None),
    ("October 23, 2026", None),
    ("After Midnight (Live 1991)", None),
    ("Expo 2000 (Virus 13 Mix)", None),
    ("Remastered 2012", None),
    ("Purple Rain (2015 Paisley Park Remaster)", (2015, "title-remaster")),
    ("Plain title", None),
    ("Year 1799 (1799)", None),
])
def test_title_year(title, expected):
    assert ry.title_year(title) == expected


def test_album_rules():
    assert ry.classify_album("Live In Minneapolis") == ry.LIVE
    assert ry.classify_album("Blue Wild Angel - Live At The Isle Of Wight (Cd 1)") == ry.LIVE
    assert ry.classify_album("Greatest Hits") == ry.COMPILATION
    assert ry.classify_album("Mezzanine") is None
    assert ry.clean_album("Machine Head - Disk 1 (The Remasters)") == "Machine Head"
    assert ry.clean_album("Bad As Me (Deluxe)") == "Bad As Me"
    assert ry.clean_album("Cut (Deluxe Edition)") == "Cut"


def _g(title, date, score=100, primary="Album", secondary=()):
    return {"id": title, "title": title, "first-release-date": date, "score": score,
            "primary-type": primary, "secondary-types": list(secondary)}


def test_pick_release_group_takes_the_matching_original():
    group, status = ry.pick_release_group(
        [_g("Mezzanine (Deluxe)", "2019-01-01", 80), _g("Mezzanine", "1998-04-20")], "Mezzanine")
    assert status == ry.MATCHED and group["first-release-date"] == "1998-04-20"


def test_pick_release_group_rules_out_live_and_compilations():
    assert ry.pick_release_group([_g("Cut", "1979", secondary=["Live"])], "Cut") == (None, ry.LIVE)
    assert ry.pick_release_group([_g("Cut", "1979", secondary=["Compilation"])], "Cut") == (
        None, ry.COMPILATION)
    assert ry.pick_release_group([_g("Something else", "1979")], "Cut") == (None, ry.NO_MATCH)
    assert ry.pick_release_group([_g("Cut", "")], "Cut") == (None, ry.NO_MATCH)


class FakeMB:
    def __init__(self, groups):
        self.groups, self.calls = groups, []

    def quote(self, text):
        return f'"{text}"'

    def get(self, path, params):
        self.calls.append(params["query"])
        return None if self.groups is None else {"release-groups": self.groups}


def _seed(conn):
    for i, (artist, title, album) in enumerate([
            ("Massive Attack", "Angel", "Mezzanine"),
            ("Massive Attack", "Teardrop (2021 Remaster)", "Mezzanine"),
            ("Massive Attack", "Karmacoma (2004)", "Mezzanine"),
            ("Pink Floyd", "Money", "Greatest Hits"),
            ("Solo", "Lonely (2010)", "")], start=1):
        conn.execute("INSERT INTO tracks (track_id, artist, title, album) VALUES (%s,%s,%s,%s)",
                     (i, artist, title, album))
    conn.commit()


def test_album_year_goes_to_every_track_and_beats_title_years():
    with localcache.connect() as conn:
        ry.ensure_schema(conn)
        _seed(conn)
        ry.apply_title_years(conn)
        mb = FakeMB([_g("Mezzanine", "1998-04-20")])
        pending = ry.pending_albums(conn)
        assert [p["album"] for p in pending] == ["Mezzanine", "Greatest Hits"]
        status, n = ry.resolve_album(conn, mb, "Massive Attack", "Mezzanine")
        assert (status, n) == (ry.MATCHED, 3)
        assert len(mb.calls) == 1
        status, n = ry.resolve_album(conn, mb, "Pink Floyd", "Greatest Hits")
        assert status == ry.COMPILATION and len(mb.calls) == 1  # ruled out by name, no request
        years = {r["track_id"]: (r["year"], r["source"]) for r in
                 conn.execute("SELECT * FROM track_year").fetchall()}
        assert years == {1: (1998, "album"), 2: (1998, "album"), 3: (1998, "album"),
                         5: (2010, "title")}
        assert ry.pending_albums(conn) == []


def test_unreachable_musicbrainz_stores_nothing_so_the_album_is_retried():
    with localcache.connect() as conn:
        ry.ensure_schema(conn)
        _seed(conn)
        assert ry.resolve_album(conn, FakeMB(None), "Massive Attack", "Mezzanine") == (
            "unreachable", 0)
        assert len(ry.pending_albums(conn)) == 2


def test_remaster_year_is_only_a_fallback():
    with localcache.connect() as conn:
        ry.ensure_schema(conn)
        _seed(conn)
        ry.apply_title_years(conn)
        assert conn.execute("SELECT source FROM track_year WHERE track_id = 2").fetchone()[
            "source"] == "title-remaster"
        assert ry.set_year(conn, 2, 1998, "album") is True
        assert ry.set_year(conn, 2, 2021, "title-remaster") is False


def test_resolve_track_is_one_request_per_new_album():
    with localcache.connect() as conn:
        _seed(conn)
        mb = FakeMB([_g("Mezzanine", "1998-04-20")])
        assert ry.needs_year(1, conn) is True
        assert ry.resolve_track(1, conn, mb) == ry.MATCHED
        assert ry.resolve_track(2, conn, mb) == "known"
        assert ry.needs_year(2, conn) is False
        assert len(mb.calls) == 1
        assert ry.resolve_track(5, conn, mb) == "title"


@pytest.mark.parametrize("album,expected", [
    ("Intégrale #15-2 (1947)", 1947),
    ("King of the Blues (1949-1966)", 1949),
    ("Beatport Top 100 June 2015 ", 2015),
    ("2000 - Let Them Eat Cake", 2000),
    ("Live At Massey Hall 1971", None),
    ("Integrale #3-1", None),
    ("Mezzanine", None),
    ("Track 12345", None),
])
def test_album_name_year(album, expected):
    assert ry.album_name_year(album) == expected


def _rec(title, date, artist="Frank Sinatra", score=100, disambiguation=""):
    return {"title": title, "first-release-date": date, "score": score,
            "disambiguation": disambiguation,
            "artist-credit": [{"name": artist, "joinphrase": "",
                               "artist": {"id": "mb-" + artist, "name": artist}}]}


def test_pick_recording_year_takes_the_earliest_studio_recording():
    recs = [_rec("Fly Me to the Moon", "1964-08-01"),
            _rec("Fly Me to the Moon", "1954-01-01"),
            _rec("Fly Me to the Moon", "1944-01-01", disambiguation="live"),
            _rec("Fly Me to the Moon", "1940-01-01", artist="Someone Else"),
            _rec("Another Song", "1930-01-01"),
            _rec("Fly Me to the Moon", "1950-01-01", score=40)]
    assert ry.pick_recording_year("Frank Sinatra", "Fly Me to the Moon (2008 Remaster)",
                                  recs) == 1954
    assert ry.pick_recording_year("Frank Sinatra", "Unknown", recs) is None


class FakeRecMB(FakeMB):
    def __init__(self, recordings):
        super().__init__([])
        self.recordings = recordings

    def get(self, path, params):
        self.calls.append((path, params["query"]))
        return None if self.recordings is None else {"recordings": self.recordings}


def test_recording_fallback_fills_what_albums_could_not_and_is_not_repeated():
    with localcache.connect() as conn:
        ry.ensure_schema(conn)
        conn.execute("INSERT INTO tracks (track_id, artist, title, album) VALUES"
                     " (1,'Frank Sinatra','My Way','Best Of'),"
                     " (2,'Frank Sinatra','My Way (Live)','Best Of'),"
                     " (3,'Frank Sinatra','Strangers','Greatest Years (1957)')")
        conn.commit()
        ry.apply_album_name_years(conn)
        pending = ry.pending_recordings(conn)
        assert [p["track_id"] for p in pending] == [1]   # live skipped, 3 already has a hint
        mb = FakeRecMB([_rec("My Way", "1969-03-01")])
        assert ry.resolve_recording(conn, mb, pending[0]) == ry.MATCHED
        assert conn.execute("SELECT year, source FROM track_year WHERE track_id = 1"
                            ).fetchone() == {"year": 1969, "source": "recording"}
        assert ry.pending_recordings(conn) == []
        assert ry.resolve_recording(conn, FakeRecMB(None), {
            "track_id": 3, "artist": "Frank Sinatra", "title": "Strangers"}) == "unreachable"
        assert ry.set_year(conn, 3, 1966, "recording") is True   # outranks the name hint
