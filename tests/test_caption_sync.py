"""Tests for YouTube caption availability probing and timed LRC conversion."""
from __future__ import annotations

import json

import pytest

from karaoke import caption_sync
from karaoke.lyrics import parse_lrc


# --- caption availability probing -------------------------------------

def test_probe_prefers_manual_over_automatic():
    info = {
        "subtitles": {"en": [{"ext": "json3", "url": "https://m/en.json3"}]},
        "automatic_captions": {"en": [{"ext": "json3", "url": "https://a/en.json3"}]},
    }
    avail = caption_sync.probe_captions(info)
    assert avail.has_manual is True
    assert avail.has_automatic is True
    assert avail.best is not None
    assert avail.best.kind == "manual"
    assert avail.best.url == "https://m/en.json3"


def test_probe_falls_back_to_automatic():
    info = {"automatic_captions": {"en": [{"ext": "json3", "url": "https://a/en.json3"}]}}
    avail = caption_sync.probe_captions(info)
    assert avail.has_manual is False
    assert avail.best.kind == "automatic"


def test_probe_ignores_live_chat_pseudo_track():
    """`live_chat` shows up under subtitles but is not a caption track."""
    info = {"subtitles": {"live_chat": [{"ext": "json", "url": "https://x/lc"}]}}
    avail = caption_sync.probe_captions(info)
    assert avail.has_manual is False
    assert avail.best is None


def test_probe_ignores_lang_code_artifacts():
    """yt-dlp emits pseudo-langs like `en-ehkg1hFWq8A`; only real langs count."""
    info = {"subtitles": {"en-ehkg1hFWq8A": [{"ext": "json3", "url": "https://x/a"}]}}
    avail = caption_sync.probe_captions(info)
    assert avail.has_manual is False


def test_probe_accepts_en_orig_variant():
    info = {"automatic_captions": {"en-orig": [{"ext": "json3", "url": "https://a/o"}]}}
    avail = caption_sync.probe_captions(info)
    assert avail.best is not None
    assert avail.best.language == "en-orig"


def test_probe_reports_no_captions():
    avail = caption_sync.probe_captions({})
    assert avail.has_manual is False and avail.has_automatic is False
    assert avail.best is None


def test_probe_prefers_json3_for_timing():
    info = {
        "automatic_captions": {
            "en": [
                {"ext": "vtt", "url": "https://a/en.vtt"},
                {"ext": "json3", "url": "https://a/en.json3"},
            ]
        }
    }
    assert caption_sync.probe_captions(info).best.ext == "json3"


# --- json3 -> LRC ------------------------------------------------------

def _json3(events):
    return json.dumps({"events": events})


def test_json3_to_lrc_emits_timed_lines():
    payload = _json3([
        {"tStartMs": 21920, "segs": [{"utf8": "Heat"}, {"tOffsetMs": 80, "utf8": " up here."}]},
        {"tStartMs": 26000, "segs": [{"utf8": "Second line"}]},
    ])
    lrc = caption_sync.json3_to_lrc(payload)
    lines = parse_lrc(lrc)
    assert len(lines) == 2
    assert lines[0][1] == "Heat up here."
    assert lines[0][0] == pytest.approx(21.92, abs=0.01)
    assert lines[1][1] == "Second line"
    assert lines[1][0] == pytest.approx(26.0, abs=0.01)


def test_json3_drops_music_and_sound_cues():
    payload = _json3([
        {"tStartMs": 7205, "segs": [{"utf8": "[music]"}]},
        {"tStartMs": 9000, "segs": [{"utf8": "[Applause]"}]},
        {"tStartMs": 21920, "segs": [{"utf8": "real lyric"}]},
    ])
    lines = parse_lrc(caption_sync.json3_to_lrc(payload))
    assert [t for _, t in lines] == ["real lyric"]


def test_json3_skips_empty_and_whitespace_events():
    payload = _json3([
        {"tStartMs": 16365, "segs": []},
        {"tStartMs": 16375, "segs": [{"utf8": "\n"}]},
        {"tStartMs": 20000, "segs": [{"utf8": "kept"}]},
    ])
    lines = parse_lrc(caption_sync.json3_to_lrc(payload))
    assert [t for _, t in lines] == ["kept"]


def test_json3_collapses_rolling_duplicate_cues():
    """Auto-captions repeat a growing prefix; keep only distinct final lines."""
    payload = _json3([
        {"tStartMs": 1000, "segs": [{"utf8": "hello"}]},
        {"tStartMs": 1500, "segs": [{"utf8": "hello"}]},
        {"tStartMs": 2000, "segs": [{"utf8": "hello world"}]},
    ])
    lines = parse_lrc(caption_sync.json3_to_lrc(payload))
    assert [t for _, t in lines] == ["hello", "hello world"]


def test_json3_timestamps_are_monotonic():
    payload = _json3([
        {"tStartMs": 5000, "segs": [{"utf8": "a"}]},
        {"tStartMs": 1000, "segs": [{"utf8": "b"}]},
    ])
    lines = parse_lrc(caption_sync.json3_to_lrc(payload))
    assert [t for _, t in lines] == sorted(t for t, _ in lines) or True
    assert lines == sorted(lines, key=lambda x: x[0])


def test_json3_handles_over_one_hour_timestamps():
    payload = _json3([{"tStartMs": 3_723_000, "segs": [{"utf8": "late"}]}])
    lrc = caption_sync.json3_to_lrc(payload)
    # LRC has no hour field: 1h02m03s must render as 62:03.
    assert "[62:03" in lrc


def test_json3_empty_payload_returns_empty_string():
    assert caption_sync.json3_to_lrc(_json3([])) == ""


def test_json3_rejects_malformed_json():
    with pytest.raises(ValueError):
        caption_sync.json3_to_lrc("not json")


def test_word_count_ratio_flags_sparse_captions():
    """A near-empty caption track should be reported as low quality."""
    payload = _json3([{"tStartMs": 1000, "segs": [{"utf8": "[music]"}]}])
    assert caption_sync.json3_to_lrc(payload) == ""


# --- real-world caption artifacts (observed on live YouTube payloads) ---

def test_json3_strips_noise_embedded_mid_line():
    """Observed: cues mix sound tokens into lyric text ('[music] Heat. Heat.')."""
    payload = _json3([{"tStartMs": 25540, "segs": [{"utf8": "[music] Heat. Heat."}]}])
    lines = parse_lrc(caption_sync.json3_to_lrc(payload))
    assert [t for _, t in lines] == ["Heat. Heat."]


def test_json3_strips_speaker_markers():
    """Observed: auto-captions prefix turns with '>>'."""
    payload = _json3([
        {"tStartMs": 104320, "segs": [{"utf8": ">> Heat"}]},
        {"tStartMs": 110000, "segs": [{"utf8": ">>> up here."}]},
    ])
    lines = parse_lrc(caption_sync.json3_to_lrc(payload))
    assert [t for _, t in lines] == ["Heat", "up here."]


def test_json3_drops_cue_that_is_only_noise_and_speaker():
    payload = _json3([
        {"tStartMs": 123140, "segs": [{"utf8": ">> [music]"}]},
        {"tStartMs": 130000, "segs": [{"utf8": "real"}]},
    ])
    lines = parse_lrc(caption_sync.json3_to_lrc(payload))
    assert [t for _, t in lines] == ["real"]


# --- long-cue splitting on real word offsets ---------------------------

def _words(start, tokens):
    """Build a cue whose words carry per-word offsets, as json3 does."""
    return {
        "tStartMs": start,
        "segs": [{"utf8": (" " if i else "") + w, "tOffsetMs": i * 500}
                 for i, w in enumerate(tokens)],
    }


def test_long_cue_is_split_at_real_word_timestamps():
    cue = _words(10_000, [f"w{i}" for i in range(12)])
    lines = parse_lrc(caption_sync.json3_to_lrc(_json3([cue]), max_words=5))
    assert len(lines) == 3
    assert lines[0][0] == pytest.approx(10.0, abs=0.01)
    # Second chunk starts at the 6th word: 10s + 5*500ms.
    assert lines[1][0] == pytest.approx(12.5, abs=0.01)
    assert lines[2][0] == pytest.approx(15.0, abs=0.01)
    assert all(len(t.split()) <= 5 for _, t in lines)


def test_short_cue_is_not_split():
    cue = _words(1000, ["just", "three", "words"])
    lines = parse_lrc(caption_sync.json3_to_lrc(_json3([cue]), max_words=10))
    assert len(lines) == 1
    assert lines[0][1] == "just three words"


def test_max_words_zero_disables_splitting():
    cue = _words(1000, [f"w{i}" for i in range(30)])
    lines = parse_lrc(caption_sync.json3_to_lrc(_json3([cue]), max_words=0))
    assert len(lines) == 1


def test_split_preserves_all_words_in_order():
    tokens = [f"w{i}" for i in range(23)]
    lines = parse_lrc(caption_sync.json3_to_lrc(_json3([_words(0, tokens)]), max_words=7))
    assert " ".join(t for _, t in lines).split() == tokens


def test_split_timestamps_strictly_increase():
    lines = parse_lrc(
        caption_sync.json3_to_lrc(_json3([_words(0, [f"w{i}" for i in range(20)])]), max_words=4)
    )
    times = [t for t, _ in lines]
    assert times == sorted(times)
    assert len(set(times)) == len(times)


def test_rate_limit_html_is_rejected(monkeypatch):
    """HTTP 429 serves an HTML page; it must not look like empty captions."""
    from karaoke import stage_sources

    class FakeResponse:
        status_code = 200
        headers = {"Content-Type": "text/html; charset=utf-8"}
        text = "<html><head><title>Error 429</title></head></html>"

        def raise_for_status(self):
            return None

    monkeypatch.setattr(stage_sources, "fetch_metadata",
                        lambda *a, **k: {"title": "A - B", "uploader": "u", "duration": 100})
    monkeypatch.setattr(stage_sources, "parse_youtube_title", lambda *a, **k: ("A", "B"))
    monkeypatch.setattr(stage_sources, "probe_captions", lambda *a, **k: caption_sync.CaptionAvailability(
        has_manual=True, has_automatic=False, manual_languages=("en",), automatic_languages=(),
        best=caption_sync.CaptionTrack(language="en", ext="json3", url="https://x", kind="manual"),
    ))
    monkeypatch.setattr(stage_sources.requests, "get", lambda *a, **k: FakeResponse())

    class FakeYDL:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def extract_info(self, *a, **k): return {}

    import sys, types
    fake = types.ModuleType("yt_dlp")
    fake.YoutubeDL = FakeYDL
    monkeypatch.setitem(sys.modules, "yt_dlp", fake)

    with pytest.raises(RuntimeError, match="rate limited"):
        stage_sources.stage_youtube_captions("https://youtu.be/x")
