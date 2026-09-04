"""Audio feature vectors for "sounds like" search.

The lyric index answers what a song is about; nothing answered what it sounds
like. These cover the parts that need no audio: the contract, the normalisation
and the index mapping.
"""
import pytest

from karaoke import audio_vector as av


def test_dimension_matches_its_parts():
    """The mapping is built from this constant, so a drift would corrupt the index."""
    assert av.AUDIO_VECTOR_DIM == (av.N_MFCC * 2) + av.N_CHROMA + av.N_CONTRAST + 3
    assert av.AUDIO_VECTOR_DIM == 62


def test_normalise_gives_unit_length():
    out = av._l2_normalise([3.0, 4.0])
    assert sum(v * v for v in out) == pytest.approx(1.0)


def test_normalise_removes_loudness():
    """Two takes at different volumes must be neighbours, not distant points."""
    quiet = av._l2_normalise([1.0, 2.0, 3.0])
    loud = av._l2_normalise([10.0, 20.0, 30.0])
    assert av.similarity(quiet, loud) == pytest.approx(1.0)


def test_normalise_leaves_a_zero_vector_alone():
    """Silence must not divide by zero."""
    assert av._l2_normalise([0.0, 0.0]) == [0.0, 0.0]


# -- balancing the feature families ----------------------------------------

def test_balance_gives_every_family_an_equal_say():
    """Raw MFCCs run to hundreds while chroma sits in 0..1.

    A single normalisation over the concatenation left the musical components
    at ~0.002 -- arithmetically present, practically absent -- so the vector was
    timbre and nothing else.
    """
    huge = [200.0, -150.0, 90.0]
    tiny = [0.01, 0.02, 0.015]
    out = av._balance([huge, tiny])
    first = sum(v * v for v in out[:3]) ** 0.5
    second = sum(v * v for v in out[3:]) ** 0.5
    assert first == pytest.approx(second)


def test_balance_yields_a_unit_vector():
    out = av._balance([[3.0, 4.0], [1.0, 0.0], [0.0, 2.0]])
    assert sum(v * v for v in out) == pytest.approx(1.0)


def test_balance_survives_a_silent_family():
    """An all-zero block (silence) must not produce NaNs."""
    out = av._balance([[1.0, 2.0], [0.0, 0.0]])
    assert all(v == v for v in out)          # no NaN
    assert out[2:] == [0.0, 0.0]


def test_a_harmonic_change_actually_moves_the_vector():
    """The point of balancing: chroma must be able to change the result."""
    mfcc = [50.0, -30.0, 10.0]
    a = av._balance([mfcc, [1.0, 0.0, 0.0, 0.0]])
    b = av._balance([mfcc, [0.0, 0.0, 0.0, 1.0]])
    assert av.similarity(a, b) < 0.9         # same timbre, different harmony


def test_similarity_is_one_for_identical_vectors():
    v = av._l2_normalise([1.0, 0.0, 1.0])
    assert av.similarity(v, v) == pytest.approx(1.0)


def test_similarity_is_zero_for_orthogonal_vectors():
    assert av.similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_similarity_rejects_a_dimension_mismatch():
    """Silently comparing different shapes would return a meaningless number."""
    with pytest.raises(ValueError, match="dimension mismatch"):
        av.similarity([1.0, 0.0], [1.0, 0.0, 0.0])


def test_extract_returns_none_without_librosa(monkeypatch):
    """The audio stack is optional; absence is not an error."""
    import builtins
    real_import = builtins.__import__

    def no_librosa(name, *args, **kwargs):
        if name == "librosa":
            raise ImportError("no librosa")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_librosa)
    assert av.extract("/nonexistent.wav") is None


def test_extract_returns_none_on_an_unreadable_file():
    assert av.extract("/nonexistent/definitely-not-audio.wav") is None


# -- the index --------------------------------------------------------------

class _FakeIndices:
    def __init__(self, exists=False):
        self._exists = exists
        self.created = None

    def exists(self, index):
        return self._exists

    def create(self, index, body):
        self.created = (index, body)


class _FakeClient:
    def __init__(self, exists=False):
        self.indices = _FakeIndices(exists)


def test_ensure_audio_index_creates_a_knn_mapping():
    client = _FakeClient(exists=False)
    assert av.ensure_audio_index(client) is True
    index, body = client.indices.created
    assert index == av.AUDIO_INDEX
    assert body["settings"]["index"]["knn"] is True
    vec = body["mappings"]["properties"]["audio_vector"]
    assert vec["type"] == "knn_vector"
    assert vec["dimension"] == av.AUDIO_VECTOR_DIM
    assert vec["method"]["space_type"] == "cosinesimil"


def test_ensure_audio_index_is_idempotent():
    client = _FakeClient(exists=True)
    assert av.ensure_audio_index(client) is False
    assert client.indices.created is None


def test_doc_id_keeps_repeat_recordings_apart():
    """The same song recorded twice is two observations, and both are kept."""
    first = av.audio_doc_id(7, 1000.0)
    second = av.audio_doc_id(7, 2000.0)
    assert first != second
    assert av.audio_doc_id(7, 1000.4) == first    # same second -> same doc


def test_build_audio_doc_carries_the_vector_and_metadata():
    doc = av.build_audio_doc(
        track_id=7, recording_id=3, artist="Portishead", title="Glory Box",
        vector=[0.1] * av.AUDIO_VECTOR_DIM, recorded_at="2026-09-04T20:00:00Z",
        duration_s=150.0, detected_key="D minor", bpm=129.2)
    assert len(doc["audio_vector"]) == av.AUDIO_VECTOR_DIM
    assert doc["track_id"] == 7 and doc["recording_id"] == 3
    assert doc["source"] == "recording"
