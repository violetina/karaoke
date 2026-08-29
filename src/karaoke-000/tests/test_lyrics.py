"""Tests for LRC parsing and the LRCLIB client (network mocked)."""
from karaoke.lyrics import parse_lrc, fetch_lrclib, Lyrics


def test_parse_lrc_basic():
    lrc = "[00:36.70] first line\n[00:43.51] second line"
    lines = parse_lrc(lrc)
    assert lines == [(36.70, "first line"), (43.51, "second line")]


def test_parse_lrc_multiple_timestamps_per_line():
    lrc = "[00:10.00][01:10.00] chorus"
    lines = parse_lrc(lrc)
    assert lines == [(10.0, "chorus"), (70.0, "chorus")]


def test_parse_lrc_sorts_by_time():
    lrc = "[01:00.00] later\n[00:30.00] earlier"
    lines = parse_lrc(lrc)
    assert [t for t, _ in lines] == [30.0, 60.0]


def test_parse_lrc_skips_metadata_and_empty():
    lrc = "[ar: Someone]\n[00:05.00]\n[00:06.00] real line"
    lines = parse_lrc(lrc)
    assert lines == [(6.0, "real line")]


def test_parse_lrc_millisecond_normalization():
    # two-digit fraction is centiseconds -> .5s ; three-digit is ms
    assert parse_lrc("[00:01.5] a") == [(1.5, "a")]
    assert parse_lrc("[00:01.500] a") == [(1.5, "a")]


class _FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


class _FakeSession:
    """Returns queued responses in order, ignoring URL/params."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return self._responses.pop(0)


def test_fetch_lrclib_get_hit():
    payload = {
        "syncedLyrics": "[00:01.00] hi",
        "plainLyrics": "hi",
    }
    sess = _FakeSession([_FakeResp(200, payload)])
    ly = fetch_lrclib("A", "B", session=sess)
    assert isinstance(ly, Lyrics)
    assert ly.source == "lrclib"
    assert ly.has_synced
    assert ly.lines == [(1.0, "hi")]
    assert sess.calls[0][0].endswith("/api/get")


def test_fetch_lrclib_falls_back_to_search():
    # /api/get 404, then /api/search returns a synced hit
    sess = _FakeSession([
        _FakeResp(404, {}),
        _FakeResp(200, [
            {"syncedLyrics": "", "plainLyrics": "plain only"},
            {"syncedLyrics": "[00:02.00] synced", "plainLyrics": "x"},
        ]),
    ])
    ly = fetch_lrclib("A", "B", session=sess)
    assert ly.has_synced
    assert ly.lines == [(2.0, "synced")]
    assert sess.calls[1][0].endswith("/api/search")


def test_fetch_lrclib_total_miss():
    sess = _FakeSession([_FakeResp(404, {}), _FakeResp(200, [])])
    ly = fetch_lrclib("A", "B", session=sess)
    assert ly.source == "none"
    assert not ly.has_synced
