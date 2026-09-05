"""Query-by-example over audio vectors.

There is no way to type into this: a 62-dimension timbre and harmony vector
shares no space with a sentence embedding. It answers the question the lyric
index cannot -- what does this *sound* like -- which for an instrumental is the
only question there is.

The number it reports needs care. Measured over 3000 random pairs of unrelated
library tracks, cosine similarity runs min 0.777, median 0.965, p95 0.986. So
0.99 is not "nearly identical", it is the top few percent, and printing a bare
score invites exactly the wrong conclusion.
"""
from __future__ import annotations

import pytest

from karaoke import search


# --- describing a compressed score ----------------------------------------

def test_the_median_between_unrelated_tracks_is_not_called_a_match():
    assert search.describe_similarity(search.SIMILARITY_TYPICAL) == "somewhat"


def test_below_typical_is_unremarkable():
    assert search.describe_similarity(0.90) == "unremarkable"


def test_the_top_five_percent_reads_as_close():
    assert search.describe_similarity(search.SIMILARITY_NOTABLE) == "close"


def test_the_rare_tail_reads_as_very_close():
    assert search.describe_similarity(0.995) == "very close"


def test_the_thresholds_reflect_the_measured_distribution():
    """They are not round numbers; they came from the corpus."""
    assert 0.9 < search.SIMILARITY_TYPICAL < search.SIMILARITY_NOTABLE
    assert search.SIMILARITY_NOTABLE < search.SIMILARITY_STRIKING < 1.0


# --- retrieval -------------------------------------------------------------

def _vec(*values) -> list[float]:
    """A unit vector padded to the stored dimension."""
    from karaoke.audio_vector import AUDIO_VECTOR_DIM

    raw = list(values) + [0.0] * (AUDIO_VECTOR_DIM - len(values))
    norm = sum(v * v for v in raw) ** 0.5 or 1.0
    return [v / norm for v in raw]


class _FakeClient:
    """Stands in for OpenSearch, returning whatever the test sets up."""

    def __init__(self, own, neighbours):
        self._own = own
        self._neighbours = neighbours
        self.queries: list[dict] = []

    def search(self, index, body):
        self.queries.append(body)
        if "knn" in body.get("query", {}):
            return {"hits": {"hits": [{"_source": n} for n in self._neighbours]}}
        return {"hits": {"hits": [{"_source": self._own}] if self._own else []}}


def _doc(track_id, artist, title, vector, source="library"):
    return {"track_id": track_id, "artist": artist, "title": title,
            "audio_vector": vector, "source": source,
            "detected_key": "D minor", "bpm": 120.0}


def test_a_track_without_a_vector_returns_nothing():
    """Most of the library is in this state; it must not look like a result."""
    client = _FakeClient(own=None, neighbours=[])
    assert search.similar_sounding(1, os_client=client) == []


def test_the_query_track_is_not_its_own_neighbour():
    own = _doc(1, "A", "One", _vec(1.0, 0.0))
    client = _FakeClient(own=own, neighbours=[own, _doc(2, "B", "Two", _vec(0.9, 0.1))])
    hits = search.similar_sounding(1, os_client=client)
    assert [h.track_id for h in hits] == [2]


def test_a_track_present_twice_takes_one_slot():
    """A song held as both a release and a capture is one answer, not two."""
    own = _doc(1, "A", "One", _vec(1.0, 0.0))
    twice = [_doc(2, "B", "Two", _vec(0.9, 0.1), source="library"),
             _doc(2, "B", "Two", _vec(0.8, 0.2), source="recording"),
             _doc(3, "C", "Three", _vec(0.7, 0.3))]
    client = _FakeClient(own=own, neighbours=twice)
    hits = search.similar_sounding(1, k=5, os_client=client)
    assert [h.track_id for h in hits] == [2, 3]


def test_similarity_is_the_cosine_not_the_engine_score():
    """OpenSearch returns 1/(1+distance); everything else here uses cosine."""
    own = _doc(1, "A", "One", _vec(1.0, 0.0))
    client = _FakeClient(own=own, neighbours=[_doc(2, "B", "Two", _vec(1.0, 0.0))])
    hits = search.similar_sounding(1, os_client=client)
    assert hits[0].similarity == pytest.approx(1.0)


def test_the_source_is_carried_so_a_caller_can_see_it():
    """A capture and a release are different observations, and comparing
    across them partly measures the capture chain rather than the music."""
    own = _doc(1, "A", "One", _vec(1.0, 0.0))
    client = _FakeClient(own=own,
                         neighbours=[_doc(2, "B", "Two", _vec(0.9, 0.1),
                                          source="recording")])
    assert search.similar_sounding(1, os_client=client)[0].source == "recording"


def test_k_is_respected():
    own = _doc(1, "A", "One", _vec(1.0, 0.0))
    many = [_doc(i, f"A{i}", f"T{i}", _vec(1.0, i / 100)) for i in range(2, 20)]
    client = _FakeClient(own=own, neighbours=many)
    assert len(search.similar_sounding(1, k=3, os_client=client)) == 3


def test_more_than_k_is_fetched_so_duplicates_can_be_dropped():
    """Otherwise the query track's own documents eat the result slots."""
    own = _doc(1, "A", "One", _vec(1.0, 0.0))
    client = _FakeClient(own=own, neighbours=[])
    search.similar_sounding(1, k=4, os_client=client)
    knn = [q for q in client.queries if "knn" in q.get("query", {})][0]
    assert knn["size"] > 4


def test_an_unreachable_index_returns_nothing_rather_than_raising():
    class _Broken:
        def search(self, index, body):
            raise RuntimeError("opensearch down")

    assert search.similar_sounding(1, os_client=_Broken()) == []
