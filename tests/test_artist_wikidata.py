"""Wikidata enrichment: parsing entities and storing them per artist."""
from karaoke import artist_identity as ai, artist_wikidata as aw, localcache

PHOENIX = "8d455809-96b3-4bb6-8829-ea4beb580d35"
OLDHAM = "7b0a63b2-b8e8-490e-b724-1d30cb5edfe3"


def _item(qid, rank="normal"):
    return {"rank": rank, "mainsnak": {"datavalue": {"type": "wikibase-entityid",
                                                     "value": {"id": qid}}}}


def _string(value, rank="normal"):
    return {"rank": rank, "mainsnak": {"datavalue": {"type": "string", "value": value}}}


def _time(t, precision):
    return {"rank": "normal", "mainsnak": {"datavalue": {
        "type": "time", "value": {"time": t, "precision": precision}}}}


BAND = {
    "id": "Q1045752",
    "labels": {"en": {"value": "Phoenix"}},
    "descriptions": {"en": {"value": "French indie pop band"}},
    "sitelinks": {"enwiki": {"title": "Phoenix (French band)"}, "dewiki": {"title": "x"}},
    "claims": {
        "P31": [_item("Q215380")],
        "P136": [_item("Q11366"), _item("Q9778", rank="deprecated")],
        "P495": [_item("Q142")],
        "P527": [_item("Q1"), _item("Q2")],
        "P571": [_time("+1999-00-00T00:00:00Z", 9)],
        "P18": [_string("Phoenix live.jpg")],
        "P1902": [_string("1xU878Z1QtBldR7ru9owdU")],
    },
}
NAMES = {"Q215380": "musical group", "Q11366": "alternative rock", "Q142": "France",
         "Q1": "Thomas Mars", "Q2": "Laurent Brancowitz", "Q9778": "electronic music"}


def test_parse_keeps_labelled_items_dates_image_ids_and_articles():
    row = aw.parse(BAND, NAMES)
    assert row["qid"] == "Q1045752" and row["label"] == "Phoenix"
    assert row["genres"] == [{"qid": "Q11366", "label": "alternative rock"}]
    assert [m["label"] for m in row["members"]] == ["Thomas Mars", "Laurent Brancowitz"]
    assert row["countries"][0]["label"] == "France"
    assert row["began"] == "1999" and row["image"] == "Phoenix live.jpg"
    assert row["external_ids"] == {"spotify": "1xU878Z1QtBldR7ru9owdU"}
    assert row["wikipedia"] == {"en": "Phoenix (French band)"}


def test_preferred_statements_win_and_deprecated_never_count():
    ent = {"claims": {"P136": [_item("Q1"), _item("Q2", rank="preferred"),
                               _item("Q3", rank="deprecated")]}}
    assert aw.item_ids(ent, "P136") == ["Q2"]
    ent = {"claims": {"P136": [_item("Q1"), _item("Q3", rank="deprecated")]}}
    assert aw.item_ids(ent, "P136") == ["Q1"]


def test_time_keeps_wikidatas_precision():
    assert aw.format_time({"time": "+1970-12-24T00:00:00Z", "precision": 11}) == "1970-12-24"
    assert aw.format_time({"time": "+1999-06-00T00:00:00Z", "precision": 10}) == "1999-06"
    assert aw.format_time({"time": "+1999-00-00T00:00:00Z", "precision": 9}) == "1999"


def test_a_person_without_country_of_origin_falls_back_to_citizenship():
    person = {"id": "Q709464", "claims": {"P27": [_item("Q30")],
                                          "P569": [_time("+1970-12-24T00:00:00Z", 11)]}}
    row = aw.parse(person, {"Q30": "United States"})
    assert row["countries"] == [{"qid": "Q30", "label": "United States"}]
    assert row["began"] == "1970-12-24"
    assert "Q30" in aw.referenced_items(person)


class FakeWD:
    def __init__(self, by_mbid=None, ents=None, names=None):
        self.by_mbid, self.ents, self.names = by_mbid or {}, ents or {}, names or {}
        self.asked_mbids = []

    def qids_for_mbids(self, mbids):
        self.asked_mbids += list(mbids)
        return {m: q for m, q in self.by_mbid.items() if m in mbids}

    def entities(self, qids):
        return {q: self.ents[q] for q in qids if q in self.ents}

    def labels(self, qids):
        return {q: self.names.get(q, "") for q in qids}


def _artist(mbid, name, wikidata_url=None):
    rels = ([{"type": "wikidata", "url": {"resource": wikidata_url}}] if wikidata_url else [])
    return {"id": mbid, "name": name, "sort-name": name, "relations": rels}


def test_run_uses_the_musicbrainz_link_then_p434_and_marks_missing_items():
    with localcache.connect() as conn:
        ai.ensure_schema(conn)
        ai.save_artist(_artist(PHOENIX, "Phoenix", "https://www.wikidata.org/wiki/Q1045752"), conn)
        ai.save_artist(_artist(OLDHAM, "Bonnie “Prince” Billy"), conn)
        ai.save_artist(_artist("nobody", "Romantic Collection"), conn)
        wd = FakeWD(by_mbid={OLDHAM: "Q709464"},
                    ents={"Q1045752": BAND, "Q709464": {"id": "Q709464", "claims": {}}},
                    names=NAMES)
        totals = aw.run(conn, wd, out=lambda line: None)
        assert totals == {"enriched": 2, "no item": 1, "linked by MBID": 1}
        assert sorted(wd.asked_mbids) == sorted([OLDHAM, "nobody"])
        rows = {r["mbid"]: r for r in conn.execute("SELECT * FROM artist_wikidata").fetchall()}
        assert rows[PHOENIX]["genres"] == [{"qid": "Q11366", "label": "alternative rock"}]
        assert rows["nobody"]["qid"] == ""
        assert conn.execute("SELECT wikidata FROM artists WHERE mbid = %s",
                            (OLDHAM,)).fetchone()["wikidata"] == "Q709464"
        # Nothing is pending on a second run, everything again with refresh.
        assert aw.pending(conn) == []
        assert len(aw.pending(conn, refresh=True)) == 3


def test_an_unreachable_wikidata_stores_nothing():
    class Down(FakeWD):
        def entities(self, qids):
            return None

    with localcache.connect() as conn:
        ai.ensure_schema(conn)
        ai.save_artist(_artist(PHOENIX, "Phoenix", "https://www.wikidata.org/wiki/Q1045752"), conn)
        totals = aw.run(conn, Down(), out=lambda line: None)
        assert totals == {"failed": 1}
        assert conn.execute("SELECT count(*) AS n FROM artist_wikidata").fetchone()["n"] == 0
