"""Tests for cover art as coloured terminal cells (no ffmpeg needed)."""
from pathlib import Path

from karaoke import coverart


# --- source resolution -----------------------------------------------------

def test_file_url_resolves_to_a_local_path(tmp_path):
    art = tmp_path / "cover.png"
    art.write_bytes(b"x")
    assert coverart.art_path_from_url(f"file://{art}") == art


def test_percent_escapes_are_decoded(tmp_path):
    art = tmp_path / "my cover.png"
    art.write_bytes(b"x")
    assert coverart.art_path_from_url(f"file://{art.parent}/my%20cover.png") == art


def test_remote_and_missing_art_are_refused(tmp_path):
    assert coverart.art_path_from_url("https://example.com/a.png") is None
    assert coverart.art_path_from_url(f"file://{tmp_path}/nope.png") is None
    assert coverart.art_path_from_url("") is None


# --- rendering -------------------------------------------------------------

def _fake_ffmpeg(monkeypatch, payload, returncode=0):
    class _P:
        stdout = payload
    monkeypatch.setattr(coverart.subprocess, "run", lambda *a, **k: _P())


def test_sample_reshapes_raw_rgb_into_rows(monkeypatch):
    # 2x2 image: red, green / blue, white
    raw = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255])
    _fake_ffmpeg(monkeypatch, raw)
    assert coverart.sample(Path("x"), 2, 2) == [
        [(255, 0, 0), (0, 255, 0)],
        [(0, 0, 255), (255, 255, 255)],
    ]


def test_sample_refuses_a_short_read(monkeypatch):
    """A truncated decode must not render half an image."""
    _fake_ffmpeg(monkeypatch, bytes([255, 0, 0]))
    assert coverart.sample(Path("x"), 4, 4) is None


def test_sample_survives_a_missing_ffmpeg(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("ffmpeg")
    monkeypatch.setattr(coverart.subprocess, "run", boom)
    assert coverart.sample(Path("x"), 2, 2) is None


def test_sample_rejects_a_zero_sized_panel():
    assert coverart.sample(Path("x"), 0, 5) is None


def test_cells_are_spaces_so_width_is_never_ambiguous():
    """Block glyphs are East-Asian ambiguous; a space is always one cell."""
    text = coverart.to_text([[(1, 2, 3), (4, 5, 6)]])
    assert set(text.plain) == {" "}


def test_each_pixel_becomes_a_background_colour():
    text = coverart.to_text([[(10, 20, 30)]])
    assert "on rgb(10,20,30)" in str(text.spans[0].style)


def test_rows_are_newline_separated_without_a_trailing_blank():
    text = coverart.to_text([[(0, 0, 0)], [(1, 1, 1)]])
    assert text.plain.count("\n") == 1


def test_render_keeps_the_aspect_ratio(monkeypatch):
    """Terminal cells are ~2:1, so a square cover needs half as many rows."""
    seen = {}

    def fake_sample(path, cols, rows, **kw):
        seen.update(cols=cols, rows=rows)
        return [[(0, 0, 0)] * cols for _ in range(rows)]
    monkeypatch.setattr(coverart, "sample", fake_sample)

    coverart.render(Path("x"), 20)
    assert seen == {"cols": 20, "rows": 10}


def test_render_returns_none_when_decoding_fails(monkeypatch):
    monkeypatch.setattr(coverart, "sample", lambda *a, **k: None)
    assert coverart.render(Path("x"), 20) is None
