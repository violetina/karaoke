"""Tests for scanner doc assembly and helpers (network + embed mocked)."""
import karaoke.scanner as scanner
from karaoke.scanner import doc_id, _embed_source, build_doc
from karaoke.tags import TrackTags
from karaoke.lyrics import Lyrics


def test_doc_id_stable_and_unique():
    a = doc_id("/music/a.mp3")
    assert a == doc_id("/music/a.mp3")
    assert a != doc_id("/music/b.mp3")
    assert len(a) == 40  # sha1 hex


def test_embed_source_prefers_lyrics():
    t = TrackTags(path="p", title="T", artist="A", album="Al")
    assert _embed_source(t, "some lyrics here") == "some lyrics here"


def test_embed_source_falls_back_to_metadata():
    t = TrackTags(path="p", title="Song", artist="Band", album="Rec")
    assert _embed_source(t, "   ") == "Song Band Rec"


def test_build_doc_with_synced(monkeypatch):
    tags = TrackTags(path="/m/x.mp3", title="Young Folks",
                     artist="Peter Bjorn and John", album="Writer's Block",
                     year=2006, duration=279.0)

    monkeypatch.setattr(
        "karaoke.lyrics.fetch_lrclib",
        lambda *a, **k: Lyrics(
            plain="if I told you", synced_raw="[00:36.70] if I told you",
            source="lrclib", lines=[(36.70, "if I told you")],
        ),
    )
    monkeypatch.setattr("karaoke.embed.embed_text", lambda s: [0.1] * 384)

    doc = build_doc(tags)
    assert doc["title"] == "Young Folks"
    assert doc["has_synced"] is True
    assert doc["lyrics_source"] == "lrclib"
    assert doc["synced_lyrics"].startswith("[00:36.70]")
    assert len(doc["lyrics_vector"]) == 384
    assert doc["source"] == "local"
    assert "indexed_at" in doc


def test_build_doc_no_lyrics(monkeypatch):
    tags = TrackTags(path="/m/y.mp3", title="Obscure", artist="Nobody")
    monkeypatch.setattr("karaoke.lyrics.fetch_lrclib", lambda *a, **k: Lyrics())
    monkeypatch.setattr("karaoke.embed.embed_text", lambda s: [0.0] * 384)

    doc = build_doc(tags)
    assert doc["has_synced"] is False
    assert doc["lyrics_source"] == "none"
    assert doc["plain_lyrics"] == ""
    # vector still built from metadata fallback
    assert len(doc["lyrics_vector"]) == 384


def test_build_doc_skips_lyrics_when_no_artist(monkeypatch):
    called = {"n": 0}

    def _fake(*a, **k):
        called["n"] += 1
        return Lyrics()

    monkeypatch.setattr("karaoke.lyrics.fetch_lrclib", _fake)
    monkeypatch.setattr("karaoke.embed.embed_text", lambda s: [0.0] * 384)
    tags = TrackTags(path="/m/z.mp3", title="NoArtist", artist="")
    build_doc(tags)
    assert called["n"] == 0  # no artist -> no LRCLIB call
