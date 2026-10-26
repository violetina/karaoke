"""A newly ingested song gets its artists: the post-processing artist step."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from karaoke import artist_identity as ai, artist_index, localcache, musicbrainz
from karaoke import postprocess_worker as pw, tasks
from karaoke.postprocess_queue import needs_postprocessing
from karaoke.tasks import PostprocessContext

GORILLAZ = "e21857d5-3256-4547-afb3-4b6ded592596"


class FakeMB:
    def __init__(self, recordings=None, artists=None, lookups=None):
        self.recordings, self.artists, self.lookups = recordings, artists, lookups or {}
        self.calls = 0

    def search_recordings(self, title, artist):
        self.calls += 1
        return self.recordings

    def search_artists(self, name):
        self.calls += 1
        return self.artists

    def artist(self, mbid):
        self.calls += 1
        return self.lookups.get(mbid)


def _rec(title, mbid, name, length):
    return {"id": "rec", "score": 100, "title": title, "length": length,
            "artist-credit": [{"name": name, "joinphrase": "", "artist": {"id": mbid, "name": name}}]}


def _artist_json(mbid, name, genres=()):
    return {"id": mbid, "name": name, "sort-name": name, "relations": [],
            "genres": [{"name": g, "count": c} for g, c in genres]}


def _track(conn, artist, title, duration=200.0):
    conn.execute("INSERT INTO tracks (artist, title, duration) VALUES (%s, %s, %s)",
                 (artist, title, duration))
    conn.commit()
    return conn.execute("SELECT track_id FROM tracks WHERE artist = %s AND title = %s",
                        (artist, title)).fetchone()["track_id"]


# -- the machine-wide MusicBrainz pace -----------------------------------------

def test_wait_turn_spaces_requests_through_the_shared_file(tmp_path):
    slot = tmp_path / "mb.slot"
    now = [100.0]
    slept = []
    clock = lambda: now[0]

    def sleep(s):
        slept.append(round(s, 2))
        now[0] += s

    musicbrainz.wait_turn(slot, clock=clock, sleep=sleep)
    now[0] += 0.3
    musicbrainz.wait_turn(slot, clock=clock, sleep=sleep)
    assert slept == [round(musicbrainz.SPACING_S - 0.3, 2)]
    now[0] += 5
    musicbrainz.wait_turn(slot, clock=clock, sleep=sleep)
    assert len(slept) == 1


# -- resolve_track -----------------------------------------------------------------

def test_a_new_artist_is_resolved_through_the_new_song_and_gets_genres():
    with localcache.connect() as conn:
        tid = _track(conn, "Gorillaz", "Désolé (Extended)", 333.0)
        mb = FakeMB(recordings=[_rec("Désolé", GORILLAZ, "Gorillaz", 333000)],
                    lookups={GORILLAZ: _artist_json(GORILLAZ, "Gorillaz", [("trip hop", 4)])})
        status, fetched = ai.resolve_track(tid, conn, mb)
        assert (status, fetched) == ("resolved", [GORILLAZ])
        assert conn.execute("SELECT mbid FROM track_artists WHERE track_id = %s",
                            (tid,)).fetchone()["mbid"] == GORILLAZ
        assert conn.execute("SELECT broad_genre FROM artist_genres WHERE artist_normalized = 'gorillaz'"
                            ).fetchone()["broad_genre"] in ("electronic", "hip hop")
        assert conn.execute("SELECT status FROM artist_lookups").fetchone()["status"] == "resolved"


def test_a_known_artist_is_linked_like_its_siblings_without_asking_musicbrainz():
    with localcache.connect() as conn:
        first = _track(conn, "Gorillaz", "Désolé")
        mb = FakeMB(recordings=[_rec("Désolé", GORILLAZ, "Gorillaz", 200000)],
                    lookups={GORILLAZ: _artist_json(GORILLAZ, "Gorillaz")})
        ai.resolve_track(first, conn, mb)
        new = _track(conn, "Gorillaz", "Feel Good Inc.")
        mb.calls = 0
        assert ai.resolve_track(new, conn, mb) == ("linked", [])
        assert mb.calls == 0
        assert conn.execute("SELECT mbid, role FROM track_artists WHERE track_id = %s",
                            (new,)).fetchone() == {"mbid": GORILLAZ, "role": "main"}


def test_unmatched_strings_are_left_alone_and_busy_musicbrainz_is_recorded_as_failed():
    with localcache.connect() as conn:
        ai.ensure_schema(conn)
        tid = _track(conn, "Romantic Collection", "Song")
        ai.record_lookup("Romantic Collection", ai.Resolution("unmatched"), conn)
        mb = FakeMB()
        assert ai.resolve_track(tid, conn, mb) == ("unmatched", []) and mb.calls == 0
        other = _track(conn, "Busy Night", "Song")
        assert ai.resolve_track(other, conn, FakeMB(recordings=None, artists=None))[0] == "failed"
        assert conn.execute("SELECT status FROM artist_lookups WHERE artist_text = 'Busy Night'"
                            ).fetchone()["status"] == "failed"


# -- what the chain thinks is pending ---------------------------------------------

def test_artist_is_pending_for_new_or_known_strings_only(monkeypatch):
    from karaoke import postprocess_queue

    monkeypatch.setattr(postprocess_queue, "_missing_search_artefacts", lambda tid: [])
    with localcache.connect() as conn:
        ai.ensure_schema(conn)
        new = _track(conn, "Brand New Act", "Song")
        assert "artist" in needs_postprocessing(new, conn)
        assert "artist" not in needs_postprocessing(new, conn, include_search_artefacts=False)
        for text, status in (("Gave Up", "unmatched"), ("Was Busy", "failed")):
            tid = _track(conn, text, "Song")
            ai.record_lookup(text, ai.Resolution(status), conn)
            assert "artist" not in needs_postprocessing(tid, conn)


# -- the task and the worker -------------------------------------------------------

def test_resolve_artist_task_runs_only_when_pending(monkeypatch):
    ran = []
    monkeypatch.setattr(pw, "run_artist_logic", lambda tid, conn: ran.append(tid) or "linked")
    monkeypatch.setattr(localcache, "connect", MagicMock())
    ctx = PostprocessContext(artist="A", title="B", url=None, track_id=7, pending=["vectors"])
    tasks.resolve_artist.run(ctx.to_dict())
    assert ran == []
    ctx.pending = ["artist", "vectors"]
    out = tasks.resolve_artist.run(ctx.to_dict())
    assert ran == [7] and out["pending"] == ["artist", "vectors"]


def test_new_artists_get_wikidata_and_an_index_document_and_failures_stay_quiet(monkeypatch):
    from karaoke import artist_wikidata, osclient

    monkeypatch.setattr(ai, "resolve_track", lambda tid, conn, mb: ("resolved", ["m1"]))
    seen = {}
    monkeypatch.setattr(artist_wikidata, "run", lambda conn, wd, **kw: seen.setdefault("wd", kw["mbids"]))
    monkeypatch.setattr(osclient, "client", lambda: "os")

    def boom(conn, client, mbids):
        raise RuntimeError("opensearch down")
    monkeypatch.setattr(artist_index, "upsert", boom)
    assert pw.run_artist_logic(1, MagicMock()) == "resolved"
    assert seen["wd"] == ["m1"]

    def broken(tid, conn, mb):
        raise RuntimeError("db hiccup")
    monkeypatch.setattr(ai, "resolve_track", broken)
    assert pw.run_artist_logic(1, MagicMock()) == "error"


def test_the_clap_step_labels_a_genre(monkeypatch):
    from karaoke import genre

    monkeypatch.setattr(pw, "_genre_labels", {"jazz": [1.0]})
    monkeypatch.setattr(genre, "classify", lambda vec, labels: SimpleNamespace(genre="jazz"))
    recorded = []
    monkeypatch.setattr(localcache, "record_genre", lambda tid, v, conn: recorded.append((tid, v.genre)))
    assert pw.label_clap_genre(5, [0.1, 0.2], MagicMock()) == "jazz"
    assert recorded == [(5, "jazz")]
    monkeypatch.setattr(genre, "classify", lambda vec, labels: None)
    assert pw.label_clap_genre(6, [0.1], MagicMock()) is None


def test_index_upsert_waits_for_the_first_rebuild(monkeypatch):
    osc = MagicMock()
    osc.indices.exists_alias.return_value = False
    assert artist_index.upsert(MagicMock(), osc, ["m1"]) == 0
    from opensearchpy import helpers

    osc.indices.exists_alias.return_value = True
    monkeypatch.setattr(artist_index, "build_documents", lambda conn, mbids: [{"mbid": "m1"}])
    sent = []
    monkeypatch.setattr(helpers, "bulk", lambda c, actions, **kw: (sent.extend(actions), (1, []))[1])
    assert artist_index.upsert(MagicMock(), osc, ["m1"]) == 1
    assert sent == [{"_index": "karaoke-artists", "_id": "m1", "_source": {"mbid": "m1"}}]
