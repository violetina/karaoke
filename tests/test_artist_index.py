"""The karaoke-artists index: documents from Postgres, rebuilds, search queries."""
from unittest.mock import MagicMock

from karaoke import artist_identity as ai, artist_index as ix, artist_wikidata as aw, localcache


def test_genre_profile_weights_votes_and_keeps_strong_families():
    raw = {"genres": [{"name": "progressive rock", "count": 13}, {"name": "art rock", "count": 11},
                      {"name": "jazz fusion", "count": 9}, {"name": "jazz", "count": 4}]}
    genres, broad, main = ix.genre_profile(raw)
    assert genres[:2] == ["progressive rock", "art rock"]
    assert main == "rock" and "jazz" not in broad


def test_genre_profile_falls_back_to_wikidata_genres():
    assert ix.genre_profile({}, ["folk music", "country music"])[1:] == (["country", "folk"], "country")
    assert ix.genre_profile({}, []) == ([], [], "")


def _artist(mbid, name, kind="Group", begin="", genres=(), aliases=(), qid=""):
    rels = [{"type": "wikidata", "url": {"resource": f"https://www.wikidata.org/wiki/{qid}"}}] if qid else []
    return {"id": mbid, "name": name, "sort-name": name, "type": kind,
            "life-span": {"begin": begin}, "aliases": [{"name": a} for a in aliases],
            "genres": [{"name": g, "count": c} for g, c in genres], "relations": rels}


def test_documents_carry_names_facets_counts_and_links():
    with localcache.connect() as conn:
        ai.ensure_schema(conn)
        aw.ensure_schema(conn)
        ai.save_artist(_artist("phx", "Phoenix", begin="1999", genres=[("indie pop", 6)],
                               aliases=["Phoenix (FR)"], qid="Q1045752"), conn)
        ai.save_artist(_artist("mars", "Thomas Mars", kind="Person", begin="1976",
                               qid="Q3524475"), conn)
        ai.save_artist(_artist("guest", "A Guest", kind="Person"), conn)
        for artist, title in (("Phoenix", "1901"), ("Phoenix", "Lisztomania"),
                              ("Phoenix feat. A Guest", "Duet")):
            conn.execute("INSERT INTO tracks (artist, title) VALUES (%s, %s)", (artist, title))
        ids = [r["track_id"] for r in conn.execute("SELECT track_id FROM tracks ORDER BY track_id").fetchall()]
        for tid, credits in zip(ids, ([("phx", "main")], [("phx", "main")],
                                      [("phx", "main"), ("guest", "featured")])):
            for pos, (mbid, role) in enumerate(credits):
                conn.execute("INSERT INTO track_artists VALUES (%s, %s, %s, %s, '', '', '', 'recording', 0)",
                             (tid, pos, mbid, role))
        aw.save("phx", {"qid": "Q1045752", "description": "French indie pop band",
                        "countries": [{"qid": "Q142", "label": "France"}],
                        "members": [{"qid": "Q3524475", "label": "Thomas Mars"},
                                    {"qid": "Q9", "label": "Not In Library"}],
                        "record_labels": [{"qid": "Q1", "label": "Glassnote Records"}],
                        "external_ids": {"spotify": "1xU8"}}, conn)
        conn.commit()
        docs = {d["mbid"]: d for d in ix.build_documents(conn)}
    phx = docs["phx"]
    assert phx["suggest"] == ["Phoenix", "Phoenix (FR)"]
    assert (phx["main_genre"], phx["began_decade"], phx["kind"]) == ("pop", "1990s", "Group")
    assert phx["countries"] == ["France"] and phx["record_labels"] == ["Glassnote Records"]
    # Members are named, and linked by MBID only when they are in the library.
    assert phx["member_names"] == ["Thomas Mars", "Not In Library"]
    assert phx["member_mbids"] == ["mars"]
    assert (phx["track_count"], phx["main_track_count"], phx["featured_track_count"]) == (3, 3, 0)
    assert phx["collaborator_mbids"] == ["guest"]
    assert phx["library_names"] == ["Phoenix", "Phoenix feat. A Guest"]
    assert phx["links"]["external_ids"] == {"spotify": "1xU8"}
    assert docs["guest"]["featured_track_count"] == 1
    assert docs["mars"]["began_decade"] == "1970s"


def test_rebuild_fills_a_fresh_index_then_moves_the_alias(monkeypatch):
    from opensearchpy import helpers

    osc = MagicMock()
    osc.indices.exists_alias.return_value = True
    osc.indices.get_alias.return_value = {"karaoke-artists-old": {}}
    sent = []
    monkeypatch.setattr(ix, "build_documents", lambda conn: [{"mbid": "a"}, {"mbid": "b"}])
    monkeypatch.setattr(helpers, "bulk", lambda client, actions, **kw: (sent.extend(actions), (2, []))[1])
    result = ix.rebuild(MagicMock(), osc)
    new = result["index"]
    assert new.startswith("karaoke-artists-") and new != "karaoke-artists-old"
    osc.indices.create.assert_called_once_with(index=new, body=ix.MAPPING)
    assert [a["_id"] for a in sent] == ["a", "b"] and {a["_index"] for a in sent} == {new}
    actions = osc.indices.update_aliases.call_args.kwargs["body"]["actions"]
    assert actions == [{"remove": {"index": "karaoke-artists-old", "alias": "karaoke-artists"}},
                       {"add": {"index": new, "alias": "karaoke-artists"}}]
    osc.indices.delete.assert_called_once_with(index="karaoke-artists-old")
    assert result["documents"] == 2 and result["failed"] == 0


def test_search_combines_as_you_type_and_fuzzy_and_drops_empty_filters():
    osc = MagicMock()
    osc.search.return_value = {"hits": {"hits": [{"_source": {"name": "Phoenix"}, "_score": 3.0}]}}
    hits = ix.search("phenix", osc, main_genre="pop", country=None)
    body = osc.search.call_args.kwargs["body"]
    should = body["query"]["bool"]["must"][0]["bool"]["should"]
    assert should[0]["multi_match"]["type"] == "bool_prefix"
    assert should[1]["multi_match"]["fuzziness"] == "AUTO"
    assert body["query"]["bool"]["filter"] == [{"term": {"main_genre": "pop"}}]
    assert hits == [{"name": "Phoenix", "score": 3.0}]


def test_an_empty_query_lists_by_track_count():
    osc = MagicMock()
    osc.search.return_value = {"hits": {"hits": []}}
    ix.search("", osc, main_genre="jazz")
    body = osc.search.call_args.kwargs["body"]
    assert body["query"]["bool"]["must"] == [{"match_all": {}}]
    assert body["sort"] == [{"track_count": "desc"}]
