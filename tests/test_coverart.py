"""Tests for cover art as coloured terminal cells (no ffmpeg needed)."""
from pathlib import Path

import pytest

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


# --- aspect ratio ----------------------------------------------------------

def _visual_aspect(cols, rows):
    """Height:width as actually drawn, accounting for the 2:1 cell shape."""
    return (rows * coverart.CELL_ASPECT) / cols


def test_square_source_renders_square():
    """Cells are ~2:1 tall, so a square image needs half as many rows."""
    assert coverart.fit((120, 120), 28, 37) == (28, 14)


def test_landscape_source_keeps_its_shape():
    cols, rows = coverart.fit((1920, 1080), 28, 37)
    assert _visual_aspect(cols, rows) == pytest.approx(1080 / 1920, abs=0.05)


def test_portrait_source_keeps_its_shape():
    cols, rows = coverart.fit((1000, 1500), 28, 37)
    assert _visual_aspect(cols, rows) == pytest.approx(1.5, abs=0.05)


def test_a_tall_image_shrinks_in_both_dimensions_rather_than_stretching():
    """Clamping height alone would squash the image; width follows it down."""
    cols, rows = coverart.fit((1000, 2000), 28, 8)
    assert rows <= 8
    assert cols < 28
    assert _visual_aspect(cols, rows) == pytest.approx(2.0, abs=0.1)


def test_fit_never_exceeds_either_bound():
    for src in ((120, 120), (1920, 1080), (500, 2000), (2000, 500)):
        cols, rows = coverart.fit(src, 28, 12)
        assert 1 <= cols <= 28 and 1 <= rows <= 12, src


def test_fit_rejects_nonsense():
    assert coverart.fit((0, 0), 28, 12) is None
    assert coverart.fit((100, 100), 0, 12) is None


def test_narrow_image_is_centred_in_the_panel():
    text = coverart.to_text([[(255, 0, 0)] * 8], pad_to=28)
    assert text.plain.startswith(" " * 10)          # (28 - 8) // 2


def test_no_padding_when_the_image_fills_the_panel():
    text = coverart.to_text([[(1, 2, 3)] * 8], pad_to=8)
    assert len(text.plain) == 8


# --- render ----------------------------------------------------------------

def test_render_measures_the_source_before_scaling(monkeypatch):
    seen = {}
    monkeypatch.setattr(coverart, "probe_size", lambda *a, **k: (1920, 1080))

    def fake_sample(path, cols, rows, **kw):
        seen.update(cols=cols, rows=rows)
        return [[(0, 0, 0)] * cols for _ in range(rows)]
    monkeypatch.setattr(coverart, "sample", fake_sample)

    coverart.render(Path("x"), 28, 37)
    assert seen["cols"] == 28
    assert seen["rows"] == 8            # 16:9, not a forced half-height box


def test_render_returns_none_when_the_source_cannot_be_measured(monkeypatch):
    """An audio-only file has no video stream to size."""
    monkeypatch.setattr(coverart, "probe_size", lambda *a, **k: None)
    assert coverart.render(Path("x"), 20) is None


def test_render_returns_none_when_decoding_fails(monkeypatch):
    monkeypatch.setattr(coverart, "probe_size", lambda *a, **k: (100, 100))
    monkeypatch.setattr(coverart, "sample", lambda *a, **k: None)
    assert coverart.render(Path("x"), 20) is None


def test_probe_size_survives_a_missing_ffprobe(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("ffprobe")
    monkeypatch.setattr(coverart.subprocess, "run", boom)
    assert coverart.probe_size(Path("x")) is None


def test_probe_size_handles_a_stream_with_no_video(monkeypatch):
    class _P:
        stdout = ""
    monkeypatch.setattr(coverart.subprocess, "run", lambda *a, **k: _P())
    assert coverart.probe_size(Path("x")) is None
