"""Combined facts on the similarity graph: links scored on every fact, filters during the walk."""
from karaoke import graph_view


def _node(tid, **kw):
    base = {"track_id": tid, "id": str(tid), "camelot": "?", "bpm": None, "genre": "",
            "artist_genres": [], "dominant_mood": None, "is_seed": False}
    base.update(kw)
    return base


class _FakeVectors:
    def __init__(self, cosines):
        self.cosines = cosines  # {(fact, a, b): cosine}

    def load(self, ids):
        pass

    def cosine(self, fact, a, b):
        return self.cosines.get((fact, min(a, b), max(a, b)))


def test_parse_strategies_keeps_known_ones_in_order():
    assert graph_view.parse_strategies("acoustic, structural,bogus,acoustic") == ["acoustic", "structural"]
    assert graph_view.parse_strategies("") == ["structural"]
    assert graph_view.parse_facts("key, tempo,nonsense") == ["key", "tempo"]


def test_edge_facts_scores_every_fact():
    a = _node(1, camelot="8A", bpm=120.0, dominant_mood="sad", artist_genres=["blues", "blues rock"])
    b = _node(2, camelot="8B", bpm=61.0, dominant_mood="sad", artist_genres=["blues"])
    vec = _FakeVectors({("chords", 1, 2): 0.97, ("sound", 1, 2): 0.60})
    facts = graph_view.edge_facts(a, b, vec)
    assert facts["chords"] == 0.97 and facts["sound"] == 0.60 and facts["lyrics"] is None
    assert facts["key"] == "relative key"
    assert facts["tempo"] is True           # 61 is half time of 120, within 6%
    assert facts["mood"] is True
    assert facts["genre"] == "blues"
    # Sound is below its threshold; lyrics unknown does not count.
    assert graph_view.agreeing(facts) == ["chords", "mood", "key", "tempo", "genre"]


def test_unknowns_are_not_disagreements_and_neutral_mood_is_no_match():
    a = _node(1, dominant_mood="neutral", genre="rock")
    b = _node(2, dominant_mood="neutral", camelot="3A")
    facts = graph_view.edge_facts(a, b, None)
    assert facts["mood"] is False
    assert facts["key"] is None and facts["tempo"] is None and facts["genre"] is None
    assert graph_view.agreeing(facts) == []


def test_genre_uses_artist_genres_before_the_clap_guess():
    # Classified as jazz; CLAP hears blues. Same rule as the TUI's H filter.
    django = _node(1, genre="blues", artist_genres=["gypsy jazz", "jazz"])
    assert not graph_view._node_passes(django, None, None, "blues", None, None)
    assert graph_view._node_passes(django, None, None, "jazz", None, None)
    unclassified = _node(2, genre="blues rock")
    assert not graph_view._node_passes(unclassified, None, None, "blues", None, None)
    assert graph_view._node_passes(unclassified, None, None, "blues rock", None, None)


def _fake_library(monkeypatch, nodes, neighbours, cosines):
    """Serve nodes/neighbours from dicts instead of Postgres and OpenSearch."""
    monkeypatch.setattr(graph_view, "_load_nodes",
                        lambda ids, seeds, strategy: {i: dict(nodes[i], is_seed=i in seeds)
                                                      for i in ids if i in nodes})
    monkeypatch.setattr(graph_view, "_neighbours",
                        lambda tid, strategy, k: neighbours.get((strategy, tid), [])[:k])
    monkeypatch.setattr(graph_view, "_Vectors", lambda: _FakeVectors(cosines))


def test_several_strategies_merge_links_and_require_filters_the_walk(monkeypatch):
    nodes = {
        1: _node(1, camelot="8A", bpm=120.0),
        2: _node(2, camelot="8A", bpm=121.0),   # mixes with 1 on key and tempo
        3: _node(3, camelot="2B", bpm=90.0),    # mixes on neither
        4: _node(4, camelot="9A", bpm=119.0),   # reached only through 2
    }
    hit = lambda tid, score=0.9: {"track_id": tid, "score": score}
    neighbours = {
        ("structural", 1): [hit(2), hit(3)],
        ("acoustic", 1): [hit(2)],
        ("structural", 2): [hit(4)],
    }
    cosines = {("chords", 1, 2): 0.98, ("sound", 1, 2): 0.9}
    _fake_library(monkeypatch, nodes, neighbours, cosines)

    data = graph_view.build_similarity_graph(track_id=1, strategy="structural,acoustic", hops=2)
    by_pair = {(e["source"], e["target"]): e for e in data["edges"]}
    assert set(by_pair) == {("1", "2"), ("1", "3"), ("2", "4")}
    both = by_pair[("1", "2")]
    assert both["found_by"] == ["chords", "sound"]       # one link, found by two searches
    assert both["agree"] == ["chords", "sound", "key", "tempo"] and both["strength"] == 4
    assert data["stats"]["strategy"] == "structural,acoustic"

    data = graph_view.build_similarity_graph(track_id=1, strategy="structural", hops=2,
                                             require="key,tempo")
    # 3 is refused; 4 is still reached through 2, which passed.
    assert {n["track_id"] for n in data["nodes"]} == {1, 2, 4}
    assert all({"key", "tempo"} <= set(e["agree"]) for e in data["edges"])


def test_node_filters_apply_during_the_walk(monkeypatch):
    nodes = {
        1: _node(1, dominant_mood="sad"),
        2: _node(2, dominant_mood="sad"),
        3: _node(3, dominant_mood="happy"),
        4: _node(4, dominant_mood="sad"),
    }
    hit = lambda tid: {"track_id": tid, "score": 0.9}
    neighbours = {("structural", 1): [hit(2), hit(3)], ("structural", 3): [hit(4)],
                  ("structural", 2): [hit(4)]}
    _fake_library(monkeypatch, nodes, neighbours, {})
    data = graph_view.build_similarity_graph(track_id=1, hops=3, mood_filter="happy")
    # The seed stays whatever its mood; only happy songs join and grow the graph.
    assert {n["track_id"] for n in data["nodes"]} == {1, 3}


def test_the_drawn_graph_stays_under_the_node_cap(monkeypatch):
    nodes = {i: _node(i) for i in range(1, 400)}
    neighbours = {}
    for n, strat in enumerate(("structural", "acoustic", "context")):
        for i in range(1, 400):
            neighbours[(strat, i)] = [{"track_id": (i * 7 + j * 13 + n * 101) % 399 + 1,
                                       "score": 0.9} for j in range(8)]
    _fake_library(monkeypatch, nodes, neighbours, {})
    data = graph_view.build_similarity_graph(track_id=1, strategy="structural,acoustic,context",
                                             hops=3, limit_per_node=8)
    assert data["stats"]["node_count"] <= 80


def test_the_page_offers_combined_facts():
    page = graph_view.GRAPH_HTML
    assert 'id="strategy-chips"' in page and 'id="require-chips"' in page
    assert 'id="min-facts-select"' in page and 'id="color-select"' in page
    assert "function showWhyLinked" in page and 'id="why-card"' in page
    assert "strategy-select" not in page
