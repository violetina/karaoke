"""Artist identity: matching through a recording, and the cached tables."""
import pytest

from karaoke import artist_identity as ai, localcache

GORILLAZ = "e21857d5-3256-4547-afb3-4b6ded592596"
DIAWARA = "6f5064bb-1a2a-4d3c-9b3c-2d4c7e5b8f11"
BPB = "7b0a63b2-b8e8-490e-b724-1d30cb5edfe3"


def _rec(title, credits, length=None, score=100, rid="rec-1"):
    return {"id": rid, "score": score, "title": title, "length": length,
            "artist-credit": [{"name": n, "joinphrase": j, "artist": {"id": i, "name": n}}
                              for n, i, j in credits]}


DESOLE = [("Gorillaz", GORILLAZ, " feat. "), ("Fatoumata Diawara", DIAWARA, "")]


def test_normalize_drops_accents_quotes_and_case():
    assert ai.normalize("Bonnie “Prince” Billy") == ai.normalize("bonnie 'prince' billy")
    assert ai.normalize("Désolé") == "desole"
    assert ai.normalize("Simon & Garfunkel") == ai.normalize("Simon and Garfunkel")


def test_clean_title_drops_version_tags():
    assert ai.clean_title("Désolé (feat. Fatoumata Diawara) - Extended Version") == "Désolé"
    assert ai.clean_title("Heroes [2017 Remaster]") == "Heroes"
    assert ai.clean_title("(Untitled)") == "(Untitled)"


@pytest.mark.parametrize("artist, first", [
    ("Gorillaz, Fatoumata Diawara", "Gorillaz"),
    ("Calvin Harris feat. Rihanna", "Calvin Harris"),
    ("Tommy Dorsey with Frank Sinatra", "Tommy Dorsey"),
    ("Simon & Garfunkel", "Simon & Garfunkel"),
    ("AC/DC", "AC/DC"),
])
def test_primary_name(artist, first):
    assert ai.primary_name(artist) == first


def test_a_guest_follows_a_feat_join_phrase():
    credits = ai.credits_of(_rec("Désolé", DESOLE))
    assert [(c.mbid, c.role) for c in credits] == [(GORILLAZ, "main"), (DIAWARA, "featured")]


def test_credit_matches_first_act_or_whole_credit():
    credits = ai.credits_of(_rec("Désolé", DESOLE))
    assert ai.credit_matches("Gorillaz, Fatoumata Diawara", credits)
    ewf = ai.credits_of(_rec("September", [("Earth, Wind & Fire", "ewf", "")]))
    assert ai.credit_matches("Earth, Wind & Fire", ewf)
    assert not ai.credit_matches("Blur", credits)


def test_pick_recording_prefers_the_same_length_and_rejects_wrong_songs():
    recs = [_rec("Désolé", DESOLE, length=236569, rid="short"),
            _rec("Désolé", DESOLE, length=333053, rid="extended"),
            _rec("Feel Good Inc.", DESOLE, length=333053, rid="other song"),
            _rec("Désolé", [("Someone Else", "x", "")], length=333053, rid="other act"),
            _rec("Désolé", DESOLE, length=333053, score=60, rid="weak")]
    rec, timed = ai.pick_recording("Gorillaz, Fatoumata Diawara",
                                   "Désolé (feat. Fatoumata Diawara) - Extended Version",
                                   333.053, recs)
    assert (rec["id"], timed) == ("extended", True)


def test_name_fallback_needs_one_exact_match():
    one = [{"id": BPB, "score": 100, "name": "Bonnie “Prince” Billy",
            "aliases": [{"name": "Bonnie Prince Billy"}]}]
    assert ai.pick_artist_by_name("Bonnie Prince Billy", one)["id"] == BPB
    two = [{"id": "fr", "score": 100, "name": "Phoenix"},
           {"id": "classical", "score": 100, "name": "Phoenix"}]
    assert ai.pick_artist_by_name("Phoenix", two) is None


class FakeMB:
    def __init__(self, recordings=None, artists=None, lookups=None):
        self.recordings, self.artists, self.lookups = recordings, artists, lookups or {}
        self.searched = []

    def search_recordings(self, title, artist):
        self.searched.append((title, artist))
        return self.recordings

    def search_artists(self, name):
        return self.artists

    def artist(self, mbid):
        return self.lookups.get(mbid)


def test_resolve_through_a_recording():
    mb = FakeMB(recordings=[_rec("Désolé", DESOLE, length=333053, rid="r")])
    res = ai.resolve("Gorillaz, Fatoumata Diawara", [("Désolé - Extended Version", 333.0)], mb)
    assert (res.status, res.method, res.recording_mbid) == ("resolved", "recording", "r")
    assert mb.searched == [("Désolé", "Gorillaz")]


def test_resolve_is_unmatched_when_nothing_convinces_and_failed_when_unreachable():
    assert ai.resolve("Phoenix", [("1901", 190.0)], FakeMB(recordings=[], artists=[
        {"id": "a", "score": 100, "name": "Phoenix"},
        {"id": "b", "score": 100, "name": "Phoenix"}])).status == "unmatched"
    assert ai.resolve("Phoenix", [("1901", 190.0)],
                      FakeMB(recordings=None, artists=None)).status == "failed"


def test_parse_artist_keeps_vote_counts_aliases_and_wikidata():
    row = ai.parse_artist({
        "id": BPB, "name": "Bonnie “Prince” Billy", "sort-name": "Billy, Bonnie “Prince”",
        "type": "Person", "country": "US", "life-span": {"begin": "1970-12-24"},
        "aliases": [{"name": "Bonnie Prince Billy"}, {"name": "Palace Music"}],
        "genres": [{"name": "country", "count": 1}, {"name": "folk", "count": 3}],
        "relations": [{"type": "wikidata",
                       "url": {"resource": "https://www.wikidata.org/wiki/Q709464"}}]})
    assert row["genres"] == [{"name": "folk", "count": 3}, {"name": "country", "count": 1}]
    assert row["wikidata"] == "Q709464"
    assert "Palace Music" in row["aliases"] and row["begin_date"] == "1970-12-24"


def _artist_json(mbid, name, aliases=()):
    return {"id": mbid, "name": name, "sort-name": name,
            "aliases": [{"name": a} for a in aliases], "genres": [], "relations": []}


def test_run_links_every_track_of_a_string_and_records_the_lookup():
    with localcache.connect() as conn:
        ai.ensure_schema(conn)
        for title in ("Désolé (feat. Fatoumata Diawara) - Extended Version", "Désolé"):
            conn.execute("INSERT INTO tracks (artist, title, duration) VALUES (%s, %s, %s)",
                         ("Gorillaz, Fatoumata Diawara", title, 333.053))
        conn.commit()
        mb = FakeMB(recordings=[_rec("Désolé", DESOLE, length=333053, rid="r")],
                    lookups={GORILLAZ: _artist_json(GORILLAZ, "Gorillaz"),
                             DIAWARA: _artist_json(DIAWARA, "Fatoumata Diawara")})
        totals = ai.run(conn, mb, out=lambda line: None)
        assert totals["resolved"] == 1 and totals["tracks linked"] == 2
        links = conn.execute("SELECT position, mbid, role FROM track_artists"
                             " ORDER BY track_id, position").fetchall()
        assert [(r["mbid"], r["role"]) for r in links[:2]] == [(GORILLAZ, "main"),
                                                              (DIAWARA, "featured")]
        assert conn.execute("SELECT status FROM artist_lookups").fetchone()["status"] == "resolved"
        # A second run has nothing left to do.
        assert ai.pending(conn) == []


def test_aliases_make_spellings_one_artist():
    with localcache.connect() as conn:
        ai.ensure_schema(conn)
        ai.save_artist(_artist_json(BPB, "Bonnie “Prince” Billy",
                                    ["Bonnie Prince Billy", "Bonnie 'Prince' Bill"]), conn)
        for spelling in ('Bonnie "Prince" Billy', "Bonnie Prince Billy", "bonnie 'prince' bill"):
            assert ai.mbids_for_name(spelling, conn) == [BPB]


def test_a_dry_run_stores_nothing():
    with localcache.connect() as conn:
        ai.ensure_schema(conn)
        conn.execute("INSERT INTO tracks (artist, title, duration) VALUES ('Gorillaz', 'Désolé', 333)")
        conn.commit()
        mb = FakeMB(recordings=[_rec("Désolé", [("Gorillaz", GORILLAZ, "")], length=333000)],
                    lookups={GORILLAZ: _artist_json(GORILLAZ, "Gorillaz")})
        ai.run(conn, mb, dry_run=True, out=lambda line: None)
        assert conn.execute("SELECT count(*) AS n FROM artists").fetchone()["n"] == 0
        assert conn.execute("SELECT count(*) AS n FROM artist_lookups").fetchone()["n"] == 0


def test_placeholder_artists_are_never_accepted():
    unknown = [{"id": "125ec42a-7229-4250-afc5-e057484327fe", "score": 100, "name": "[unknown]",
                "aliases": [{"name": "artist"}]}]
    assert ai.pick_artist_by_name("artist", unknown) is None
    various = ai.credits_of(_rec("Song", [("Various Artists", ai.VARIOUS_ARTISTS, "")]))
    assert not ai.credit_matches("Various Artists", various)


def test_voted_genres_keep_votes_and_drop_the_long_tail():
    raw = {"genres": [{"name": "progressive rock", "count": 13}, {"name": "jazz fusion", "count": 9},
                      {"name": "doo-wop", "count": 2}, {"name": "spoken word", "count": 1}]}
    assert ai.voted_genres(raw) == [("progressive rock", 13), ("jazz fusion", 9)]


def test_voted_genres_fall_back_to_tags_that_name_a_genre():
    raw = {"genres": [], "tags": [{"name": "blues", "count": 2}, {"name": "british", "count": 5}]}
    assert ai.voted_genres(raw) == [("blues", 2)]


def _raw(mbid, name, genres):
    return {"id": mbid, "name": name, "sort-name": name, "relations": [],
            "genres": [{"name": g, "count": c} for g, c in genres]}


def test_artist_genres_come_from_main_credits_votes_and_replace_older_rows():
    with localcache.connect() as conn:
        ai.ensure_schema(conn)
        localcache.ensure_artist_genres_table(conn)
        # A wrong name-searched row and a seed row the votes should replace.
        conn.execute("INSERT INTO artist_genres VALUES ('phoenix', 'classical', 'classical', 1, 'online', 0)")
        conn.execute("INSERT INTO artist_genres VALUES ('nobody known', 'folk', 'folk', 1, 'seed', 0)")
        for artist, title in (("Phoenix", "1901"), ("Gorillaz, Fatoumata Diawara", "Désolé"),
                              ("Nobody Known", "Song")):
            conn.execute("INSERT INTO tracks (artist, title) VALUES (%s, %s)", (artist, title))
        conn.commit()
        ai.save_artist(_raw("phx", "Phoenix", [("indie pop", 6), ("synth-pop", 3)]), conn)
        ai.save_artist(_raw(GORILLAZ, "Gorillaz", [("trip hop", 4), ("alternative rock", 3)]), conn)
        ai.save_artist(_raw(DIAWARA, "Fatoumata Diawara", [("folk", 9)]), conn)
        ai.save_artist(_raw("nk", "Nobody Known", []), conn)
        ids = {r["artist"]: r["track_id"] for r in conn.execute("SELECT * FROM tracks").fetchall()}
        for artist, credits in (("Phoenix", [("phx", "main")]),
                                ("Gorillaz, Fatoumata Diawara", [(GORILLAZ, "main"), (DIAWARA, "featured")]),
                                ("Nobody Known", [("nk", "main")])):
            for pos, (mbid, role) in enumerate(credits):
                conn.execute("INSERT INTO track_artists VALUES (%s, %s, %s, %s, '', '', '', 'recording', 0)",
                             (ids[artist], pos, mbid, role))
        conn.commit()
        totals = ai.derive_artist_genres(conn)
        assert totals == {"from musicbrainz": 2, "kept as was": 1}
        rows = conn.execute("SELECT artist_normalized, genre, broad_genre, weight, source"
                            " FROM artist_genres ORDER BY 1, 2, 3").fetchall()
        by = {}
        for r in rows:
            by.setdefault(r["artist_normalized"], set()).add((r["genre"], r["weight"], r["source"]))
        assert ("classical", 1.0, "online") not in by["phoenix"]
        assert ("indie pop", 6.0, "musicbrainz") in by["phoenix"]
        # The guest's folk does not make the Gorillaz song folk.
        assert {g for g, _, _ in by["gorillaz, fatoumata diawara"]} == {"trip hop", "alternative rock"}
        assert by["nobody known"] == {("folk", 1.0, "seed")}
