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


def test_publishing_claims_the_song_so_a_second_publish_is_not_queued(db, monkeypatch):
    sent = []
    monkeypatch.setattr(q, "orchestrator", lambda: "celery")
    monkeypatch.setattr(tasks, "enqueue_postprocess", lambda p: sent.append(p) or "id")
    assert q.publish_postprocess_task("808 State", "Flow Coma")
    assert q.publish_postprocess_task("808 State", "Flow Coma")    # in flight: still "queued"
    assert len(sent) == 1 and sent[0]["claimed"] is True


def test_a_claimed_payload_starts_its_chain(db, monkeypatch):
    """The producer's claim is the chain's own; the task must not refuse it."""
    started = []

    class FakeChain:
        def __init__(self, *sigs):
            started.append(sigs)

        def apply_async(self):
            return None

    monkeypatch.setattr(tasks, "chain", FakeChain)
    with localcache.connect() as c:
        assert q.claim("A", "B", c)
    tasks.postprocess_track.run({"artist": "A", "title": "B", "url": "", "claimed": True})
    assert len(started) == 1


def test_a_failed_publish_releases_its_claim(db, monkeypatch):
    def broken(_payload):
        raise ConnectionError("broker down")

    monkeypatch.setattr(q, "orchestrator", lambda: "celery")
    monkeypatch.setattr(tasks, "enqueue_postprocess", broken)
    assert not q.publish_postprocess_task("A", "B")
    with localcache.connect() as c:
        assert q.claim("A", "B", c)


def test_explicit_asks_are_not_claimed_at_publish(db, monkeypatch):
    """Named steps and the timing upgrade claim in the task, as before."""
    sent = []
    monkeypatch.setattr(q, "orchestrator", lambda: "celery")
    monkeypatch.setattr(tasks, "enqueue_postprocess", lambda p: sent.append(p) or "id")
    q.publish_postprocess_task("A", "B", steps=["chords"])
    q.publish_postprocess_task("A", "B", include_timings=True)
    assert len(sent) == 2 and not any(p.get("claimed") for p in sent)
