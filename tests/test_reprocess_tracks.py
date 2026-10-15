import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

from karaoke import key_progression

# reprocess_tracks lives in scripts/, not the karaoke package; load it directly.
_SPEC = importlib.util.spec_from_file_location(
    "reprocess_tracks",
    Path(__file__).resolve().parent.parent / "scripts" / "reprocess_tracks.py",
)
reprocess = importlib.util.module_from_spec(_SPEC)
# Dataclasses resolve their module through sys.modules while the class is built.
sys.modules[_SPEC.name] = reprocess
_SPEC.loader.exec_module(reprocess)


def _decoded(secs, **kw):
    return reprocess.Audit(status="", decoded_s=secs, max_db=-1.0, **kw)


def test_matching_length_is_ok():
    audit = _decoded(240.0)
    reprocess.judge(audit, 242.0)
    assert audit.status == "ok"
    assert audit.issues == []


def test_length_mismatch_is_suspect():
    audit = _decoded(249.0)
    reprocess.judge(audit, 273.0)
    assert audit.status == "suspect"
    assert audit.issues[0].startswith("duration_mismatch")


def test_truncated_file_is_suspect_even_when_db_agrees():
    # The db duration was read off the truncated file, so it agrees with it.
    audit = _decoded(11.0)
    reprocess.judge(audit, 10.96)
    assert audit.status == "suspect"
    assert any(i.startswith("short") for i in audit.issues)


def test_silent_file_is_broken():
    audit = reprocess.Audit(status="", decoded_s=200.0, max_db=-91.0)
    reprocess.judge(audit, 200.0)
    assert audit.status == "broken"


def test_informational_issues_do_not_make_a_file_suspect():
    audit = _decoded(200.0, demuxer="mp3", has_video=True)
    reprocess.judge(audit, None)
    assert audit.status == "ok"
    assert set(audit.issues) == {"no_db_duration", "video_source", "riff_wrapped_mp3"}


def test_counterpart_paths_cross_roots():
    rel = Path("Artist/Album/01 Song.mp3")
    paths = reprocess.counterpart_paths(reprocess.BACKUP_DISK_ROOT / rel)
    assert paths == [reprocess.BACKUP_DISK_ROOT / rel, reprocess.HOME_MUSIC_ROOT / rel]


class _Client:
    def __init__(self):
        self.calls = []
        self.indices = SimpleNamespace(exists=lambda index: True,
                                       get_mapping=lambda index: {})

    def update(self, **kw):
        self.calls.append(("update", kw))

    def index(self, **kw):
        self.calls.append(("index", kw))


def test_store_without_chords_merges_instead_of_replacing(monkeypatch):
    monkeypatch.setattr(key_progression, "ensure_index", lambda c: False)
    monkeypatch.setattr(key_progression, "build_doc",
                        lambda **kw: {"track_id": kw["track_id"], "form": "vamp"})
    client = _Client()
    assert key_progression.store(7, object(), chords=None, os_client=client)
    (op, kw), = client.calls
    # A replace would drop chord_motion found on an earlier run.
    assert op == "update"
    assert kw["body"]["doc_as_upsert"] is True
    assert "chord_motion" not in kw["body"]["doc"]
