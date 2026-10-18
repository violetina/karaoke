"""One post-processing chain per song at a time."""
import pytest

from karaoke import localcache, tasks
from karaoke import postprocess_queue as q


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "claims.db"
    real = localcache.connect
    monkeypatch.setattr(localcache, "connect", lambda *a, **k: real(path))
    return path


def test_a_second_claim_for_the_same_song_is_refused(db):
    with localcache.connect() as c:
        assert q.claim("808 State", "Flow Coma", c, now=1000.0)
        assert not q.claim("808 State", "Flow Coma", c, now=1100.0)
        assert q.claim("808 State", "Cubik", c, now=1100.0)        # other song: free


def test_the_song_key_ignores_case_and_spacing(db):
    with localcache.connect() as c:
        assert q.claim("808 State", "Flow Coma", c, now=1000.0)
        assert not q.claim(" 808 state ", "FLOW COMA", c, now=1001.0)


def test_a_stale_claim_from_a_dead_chain_expires(db):
    with localcache.connect() as c:
        assert q.claim("A", "B", c, now=1000.0)
        assert q.claim("A", "B", c, now=1000.0 + q.CLAIM_TTL_S + 1)


def test_release_frees_the_song(db):
    with localcache.connect() as c:
        assert q.claim("A", "B", c, now=1000.0)
        q.release("A", "B", c)
        assert q.claim("A", "B", c, now=1001.0)


def test_a_duplicate_request_does_not_start_a_chain(db, monkeypatch):
    started = []

    class FakeChain:
        def __init__(self, *sigs):
            started.append(sigs)

        def apply_async(self):
            return None

    monkeypatch.setattr(tasks, "chain", FakeChain)
    payload = {"artist": "808 State", "title": "Flow Coma", "url": ""}
    tasks.postprocess_track.run(payload)
    tasks.postprocess_track.run(payload)
    assert len(started) == 1
    tasks.postprocess_track.run({**payload, "force": True})       # explicit re-run
    assert len(started) == 2


def test_the_last_step_releases_the_claim(db):
    from karaoke.tasks import PostprocessContext

    with localcache.connect() as c:
        assert q.claim("A", "B", c)
    tasks.finish_postprocess.run(PostprocessContext(artist="A", title="B", url="").to_dict())
    with localcache.connect() as c:
        assert q.claim("A", "B", c)
