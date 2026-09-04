"""Layout smoke tests that actually boot the Textual app.

This is the only test in the suite that runs an event loop. It exists for one
reason: the auto-focus trap is invisible to unit tests. Textual's default
``AUTO_FOCUS = "*"`` focuses the first focusable widget at mount, which with the
browse overlay hidden is the ``Select`` *inside it* — every keypress would then
be swallowed by an invisible dropdown, and nothing short of a real mount can
detect that.

Uses ``asyncio.run`` directly rather than pytest-asyncio, which is not a
dependency.
"""
from __future__ import annotations

import asyncio

import pytest

from karaoke import localcache, tui


@pytest.fixture
def app(tmp_path, monkeypatch):
    """A KaraokeTui backed by an empty temp DB, so no real library is touched.

    Hands out a FRESH connection per call, as the real ``localcache.connect``
    does: the app's background workers run on other threads, and SQLite objects
    cannot cross threads.
    """
    real_connect = localcache.connect
    db = tmp_path / "karaoke.db"
    monkeypatch.setattr(localcache, "connect", lambda *a, **k: real_connect(db))
    monkeypatch.setattr(tui, "stream_logs", lambda *a, **k: iter(()))
    return tui.KaraokeTui()


def _run(coro):
    return asyncio.run(coro)


def test_nothing_is_focused_at_mount(app):
    """Regression for the auto-focus trap: focus must not land in the overlay."""
    async def go():
        async with app.run_test() as pilot:
            await pilot.pause()
            assert app.focused is None
    _run(go())


def test_app_bindings_fire_at_mount(app):
    """With focus loose, an app-level binding must still reach its action."""
    async def go():
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("H")
            await pilot.pause()
            assert app.query_one("#browse-overlay").has_class("-visible")
    _run(go())


def test_overlay_does_not_resize_the_workspace(app):
    """The whole point of the layer: revealing browse must not shrink lyrics."""
    async def go():
        async with app.run_test() as pilot:
            await pilot.pause()
            before = app.query_one("#workspace").region
            lyrics_before = app.query_one("#lyrics").region
            await pilot.press("H")
            await pilot.pause()
            assert app.query_one("#workspace").region == before
            assert app.query_one("#lyrics").region == lyrics_before
    _run(go())


def test_browse_toggles_and_focuses_the_table(app):
    async def go():
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("H")
            await pilot.pause()
            assert app.focused is app.query_one("#library")
            await pilot.press("H")
            await pilot.pause()
            assert not app.query_one("#browse-overlay").has_class("-visible")
            assert app.focused is None
    _run(go())


def test_escape_closes_the_overlay(app):
    async def go():
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("H")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert not app.query_one("#browse-overlay").has_class("-visible")
    _run(go())


def test_question_mark_opens_help(app):
    async def go():
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            assert isinstance(app.screen, tui.HelpScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, tui.HelpScreen)
    _run(go())


def test_settings_panel_is_gone(app):
    """#settings was deleted; its contents moved to the overlay and statusbar."""
    async def go():
        async with app.run_test() as pilot:
            await pilot.pause()
            assert not app.query("#settings")
            assert app.query_one("#mode-label")      # statusbar
            assert app.query_one("#worker-load")     # statusbar
            assert app.query_one("#filter-select")   # overlay
    _run(go())


# --- responsive chrome -----------------------------------------------------
#
# The way to get bigger lyrics in a terminal is a bigger terminal font, which
# costs cells. At 80x24 the fixed panels took 72% of the screen and the lyrics
# 28%, so the chrome has to yield rather than squeeze them into a corner.

def _lyric_share(app, w, h):
    region = app.query_one("#lyrics").region
    return round(100 * region.width * region.height / (w * h))


def test_narrow_terminal_hides_the_visuals_column(app):
    """34 fixed columns is 42% of an 80-wide screen."""
    async def go():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert not app.query_one("#visuals").display
            assert _lyric_share(app, 80, 24) > 70
    _run(go())


def test_wide_terminal_keeps_the_visuals_column(app):
    async def go():
        async with app.run_test(size=(150, 38)) as pilot:
            await pilot.pause()
            assert app.query_one("#visuals").display
    _run(go())


def test_short_terminal_compacts_the_now_playing_panel(app):
    async def go():
        async with app.run_test(size=(150, 24)) as pilot:
            await pilot.pause()
            assert app.query_one("#now-playing").region.height <= 3
    _run(go())


def test_resizing_reapplies_the_breakpoints(app):
    """Classes must follow a live resize, not just the initial mount."""
    async def go():
        async with app.run_test(size=(150, 38)) as pilot:
            await pilot.pause()
            assert app.query_one("#visuals").display
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            assert not app.query_one("#visuals").display
            await pilot.resize_terminal(150, 38)
            await pilot.pause()
            assert app.query_one("#visuals").display
    _run(go())


def test_focus_mode_leaves_only_the_lyrics(app):
    async def go():
        async with app.run_test(size=(150, 38)) as pilot:
            await pilot.pause()
            await pilot.press("F")
            await pilot.pause()
            for sel in ("#visuals", "#now-playing", "#statusbar"):
                assert not app.query_one(sel).display, sel
            assert _lyric_share(app, 150, 38) > 90
            await pilot.press("F")
            await pilot.pause()
            assert app.query_one("#now-playing").display
    _run(go())


def test_left_sidebar_balances_the_layout(app):
    """Two equal side columns put the lyrics in the middle of the screen."""
    async def go():
        async with app.run_test(size=(190, 45)) as pilot:
            await pilot.pause()
            left = app.query_one("#sidebar").region
            right = app.query_one("#visuals").region
            lyrics = app.query_one("#lyrics").region
            assert left.width == right.width
            gap_left = lyrics.x - left.right
            gap_right = right.x - lyrics.right
            assert abs(gap_left - gap_right) <= 2       # visually centred
    _run(go())


def test_worker_panel_lives_in_the_left_sidebar(app):
    async def go():
        async with app.run_test(size=(190, 45)) as pilot:
            await pilot.pause()
            panel = app.query_one("#worker-panel")
            assert panel in app.query_one("#sidebar").walk_children()
    _run(go())


def test_beat_art_space_is_reserved(app):
    """Held empty on purpose, for beat-driven visuals later."""
    async def go():
        async with app.run_test(size=(190, 45)) as pilot:
            await pilot.pause()
            art = app.query_one("#beat-art").region
            assert art.height > 5      # real space, not a collapsed widget
    _run(go())


def test_sidebars_both_hide_on_a_narrow_terminal(app):
    async def go():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert not app.query_one("#sidebar").display
            assert not app.query_one("#visuals").display
    _run(go())
