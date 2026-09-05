"""Keeping cover art after its URL stops working.

Spotify image URLs expire and YouTube thumbnails change with re-uploads, so art
fetched today may be unfetchable next month. That is why this lives in SQLite
rather than only in OpenSearch: vector_index is explicit that the index holds
*rebuildable* documents, and art whose source has expired cannot be rebuilt.
"""
from __future__ import annotations

import sqlite3

import pytest

from karaoke import cover_store, localcache


def _grid(cols: int, rows: int, base: int = 0):
    """A gradient, so a rescale that scrambles positions is visible."""
    return [[((base + x * 4) % 256, (base + y * 4) % 256, 128)
             for x in range(cols)] for y in range(rows)]


@pytest.fixture()
def conn(tmp_path):
    c = localcache.connect(tmp_path / "art.db")
    cover_store.ensure_table(c)
    for artist, title in (("Portishead", "Glory Box"),
                          ("Portishead", "Sour Times"),
                          ("Sleep", "Dragonaut")):
        c.execute("INSERT INTO tracks (artist, title) VALUES (?, ?)",
                  (artist, title))
    c.commit()
    yield c
    c.close()


# --- packing ---------------------------------------------------------------

def test_a_grid_survives_a_round_trip():
    grid = _grid(8, 4)
    assert cover_store.unpack(cover_store.pack(grid), 8, 4) == grid


def test_compression_actually_saves_space():
    """3.7 KB per cover at the stored size is what makes this affordable."""
    grid = _grid(cover_store.STORE_COLS, cover_store.STORE_ROWS)
    raw = cover_store.STORE_COLS * cover_store.STORE_ROWS * 3
    assert len(cover_store.pack(grid)) < raw


def test_corrupt_bytes_unpack_to_nothing_rather_than_raising():
    assert cover_store.unpack(b"not zlib", 4, 4) is None


def test_a_truncated_blob_is_refused():
    """Half a picture rendered as a whole one would look like working art."""
    short = cover_store.pack(_grid(2, 2))
    assert cover_store.unpack(short, 64, 32) is None


# --- identity --------------------------------------------------------------

def test_the_same_image_gets_one_key_through_two_urls():
    """An album cover reached via Spotify and via YouTube is one cover."""
    grid = _grid(8, 4)
    a = cover_store.art_key("https://spotify.example/abc", grid)
    b = cover_store.art_key("https://youtube.example/xyz", grid)
    assert a == b


def test_different_images_get_different_keys():
    assert (cover_store.art_key("u", _grid(8, 4, base=0))
            != cover_store.art_key("u", _grid(8, 4, base=64)))


def test_without_pixels_the_url_identifies_the_art():
    assert cover_store.art_key("https://a.example/1").startswith("url:")
    assert cover_store.art_key("u", _grid(4, 4)).startswith("px:")


# --- storing and sharing ---------------------------------------------------

def test_one_cover_serves_a_whole_album(conn):
    """Storing per track would multiply an album cover by its track count."""
    grid = _grid(16, 8)
    key = cover_store.store(grid, conn, source_url="https://a.example/cover")
    cover_store.link(1, key, conn)
    cover_store.link(2, key, conn)

    info = cover_store.stats(conn)
    assert info["covers"] == 1
    assert info["tracks"] == 2


def test_art_comes_back_for_a_linked_track(conn):
    grid = _grid(16, 8)
    cover_store.link(1, cover_store.store(grid, conn), conn)
    assert cover_store.grid_for_track(1, conn) == grid


def test_an_unlinked_track_has_no_art(conn):
    assert cover_store.grid_for_track(3, conn) is None


def test_relinking_a_track_replaces_rather_than_duplicates(conn):
    first = cover_store.store(_grid(8, 4, base=0), conn)
    second = cover_store.store(_grid(8, 4, base=64), conn)
    cover_store.link(1, first, conn)
    cover_store.link(1, second, conn)
    assert cover_store.stats(conn)["tracks"] == 1
    assert cover_store.grid_for_track(1, conn) == _grid(8, 4, base=64)


def test_an_empty_grid_is_not_stored(conn):
    assert cover_store.store([], conn) is None


def test_re_storing_keeps_the_art_and_fills_in_a_missing_url(conn):
    grid = _grid(8, 4)
    key = cover_store.store(grid, conn, source_url="")
    assert cover_store.store(grid, conn, source_url="https://a.example/x") == key
    row = conn.execute("SELECT source_url FROM cover_art WHERE art_key = ?",
                       (key,)).fetchone()
    assert row["source_url"] == "https://a.example/x"


def test_the_tables_are_added_to_a_database_predating_them(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(str(path))
    old.execute("CREATE TABLE tracks (track_id INTEGER PRIMARY KEY)")
    old.commit()
    old.close()

    c = localcache.connect(path)
    try:
        cover_store.ensure_table(c)
        names = {r["name"] for r in
                 c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"cover_art", "track_art"} <= names
    finally:
        c.close()


# --- fitting to a panel ----------------------------------------------------

def test_a_smaller_panel_gets_a_downscaled_grid():
    """One stored copy serves any panel smaller than it."""
    out = cover_store.rescale(_grid(64, 32), 16, 8)
    assert out is not None
    assert len(out) == 8 and len(out[0]) == 16


def test_the_same_size_is_returned_unchanged():
    grid = _grid(16, 8)
    assert cover_store.rescale(grid, 16, 8) is grid


def test_upscaling_is_refused_rather_than_invented():
    """Enlarging invents detail; re-sampling the source is better while it exists."""
    assert cover_store.rescale(_grid(16, 8), 32, 16) is None


def test_a_rescale_preserves_orientation():
    """A flipped or transposed image would still 'render', and look wrong."""
    grid = [[(0, 0, 0), (255, 0, 0)],
            [(0, 255, 0), (0, 0, 255)]]
    out = cover_store.rescale(grid, 1, 1)
    assert out == [[(0, 0, 0)]]      # top-left, not any other corner


def test_a_zero_sized_panel_is_refused():
    assert cover_store.rescale(_grid(8, 4), 0, 4) is None


def test_rendering_returns_something_drawable(conn):
    cover_store.link(1, cover_store.store(_grid(64, 32), conn), conn)
    text = cover_store.render_for_track(1, 16, 8, conn)
    assert text is not None
    assert len(text.plain.splitlines()) == 8


def test_rendering_an_unknown_track_is_none_not_an_error(conn):
    assert cover_store.render_for_track(3, 16, 8, conn) is None
