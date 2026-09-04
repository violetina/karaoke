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
