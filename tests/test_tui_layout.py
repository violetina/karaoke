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


def test_stats_screen_opens_and_closes(app):
    async def go():
        async with app.run_test(size=(150, 45)) as pilot:
            await pilot.pause()
            await pilot.press("T")
            await pilot.pause()
            assert isinstance(app.screen, tui.StatsScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, tui.StatsScreen)
    _run(go())


def test_sidebar_order_puts_workers_at_the_bottom(app):
    """Art takes the slack; per-track facts then global worker stats below."""
    async def go():
        async with app.run_test(size=(190, 45)) as pilot:
            await pilot.pause()
            art = app.query_one("#beat-art").region
            info = app.query_one("#track-info").region
            workers = app.query_one("#worker-panel").region
            assert art.y < info.y < workers.y
            sidebar = app.query_one("#sidebar").region
            # Pinned to the bottom, not floating in the middle.
            assert sidebar.bottom - workers.bottom <= 1
    _run(go())


# --- the search queue, against real widgets --------------------------------
#
# _set_queue and _render_queue both reach for #queue. When it changed from a
# Static to a DataTable, only one of them was updated and the other raised
# WrongType on the first search. Unit tests missed it entirely because they
# stubbed the render and never touched a widget -- so these drive the real one.

def test_the_queue_widget_is_populated_by_a_search(app):
    async def go():
        async with app.run_test() as pilot:
            await pilot.pause()
            rows = [{"track_id": 1, "artist": "Primus", "title": "Jerry",
                     "score": 0.5, "fields": ("title",), "url": ""}]
            # No source, so nothing is launched; the widget path still runs.
            app._set_queue(rows, "jerry")
            await pilot.pause()
            table = app.query_one("#queue")
            assert table.has_class("-on")
            assert table.row_count == 1
    _run(go())


def test_an_empty_result_clears_and_hides_the_queue(app):
    async def go():
        async with app.run_test() as pilot:
            await pilot.pause()
            app._set_queue([{"track_id": 1, "artist": "A", "title": "B",
                             "score": 0.5, "fields": (), "url": ""}], "x")
            await pilot.pause()
            app._set_queue([], "nothing")
            await pilot.pause()
            table = app.query_one("#queue")
            assert not table.has_class("-on")
            assert table.row_count == 0
    _run(go())


def test_the_search_box_takes_focus_on_slash(app):
    async def go():
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            assert app.focused is not None
            assert app.focused.id == "search-input"
    _run(go())


def test_the_mood_panel_survives_a_render(app):
    """_update_mood reaches for #mood-square; it must match its widget too."""
    async def go():
        async with app.run_test() as pilot:
            await pilot.pause()
            app._update_mood("happy")
            await pilot.pause()
    _run(go())


def test_escape_leaves_the_search_box(app):
    """The trap: nothing else on this screen is focusable.

    With focus in the search Input there was no Tab target and no binding that
    released it, so every key -- including the ones bound to actions -- went
    into the box and the only way out was killing the app.
    """
    async def go():
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            assert app.search_has_focus(), "'/' should focus the search box"

            await pilot.press("escape")
            await pilot.pause()
            assert not app.search_has_focus()
            assert app.focused is None
    _run(go())


def test_bindings_work_again_after_leaving_search(app):
    """Escaping is only useful if the keyboard comes back with it."""
    async def go():
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("H")
            await pilot.pause()
            assert app.query_one("#browse-overlay").has_class("-visible")
    _run(go())


def test_an_empty_search_still_releases_focus(app):
    """The second way to stay stuck: submitting nothing returned early,
    before the line that gives focus back."""
    async def go():
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert not app.search_has_focus()
    _run(go())


def test_the_way_out_is_written_on_the_box(app):
    """There is no other affordance: nothing to Tab to, nothing visible."""
    async def go():
        async with app.run_test() as pilot:
            await pilot.pause()
            placeholder = app.query_one("#search-input").placeholder
            assert "esc" in placeholder.lower()
    _run(go())


def test_the_art_box_does_not_fill_the_column(app):
    """It hugs the art instead.

    Covers are drawn at their own aspect -- 6 rows for a 16:9 thumbnail, 11 for
    a square sleeve -- so `1fr` left a gap under every one of them. A wide
    terminal is needed or the sidebar is hidden entirely.
    """
    async def go():
        async with app.run_test(size=(190, 45)) as pilot:
            await pilot.pause()
            art = app.query_one("#beat-art").region
            sidebar = app.query_one("#sidebar").region
            assert sidebar.height > 0, "sidebar hidden; terminal too narrow"
            assert art.height < sidebar.height
    _run(go())


def test_the_art_box_does_not_collapse_before_anything_is_drawn(app):
    """A zero-height box would make the sidebar jump on every track change."""
    async def go():
        async with app.run_test(size=(190, 45)) as pilot:
            await pilot.pause()
            assert app.query_one("#beat-art").region.height >= 8
    _run(go())


def test_the_art_and_its_facts_share_one_frame(app):
    """They describe one track. Three boxes down a narrow column read as three
    subjects rather than one."""
    async def go():
        async with app.run_test(size=(190, 45)) as pilot:
            await pilot.pause()
            box = app.query_one("#cover-box").region
            art = app.query_one("#beat-art").region
            info = app.query_one("#track-info").region
            # Both inside the frame, art above the facts.
            assert box.y <= art.y < info.y
            assert info.bottom <= box.bottom
    _run(go())


def test_the_frame_hugs_its_contents(app):
    async def go():
        async with app.run_test(size=(190, 45)) as pilot:
            await pilot.pause()
            box = app.query_one("#cover-box").region
            sidebar = app.query_one("#sidebar").region
            assert box.height < sidebar.height
    _run(go())


def test_ctrl_c_stops_a_running_sample_instead_of_quitting(app):
    """A 45-second capture is the one thing here long enough to reach for
    ctrl+c, and before this it did nothing to it."""
    async def go():
        async with app.run_test(size=(190, 45)) as pilot:
            await pilot.pause()
            app._sampling = True
            app._cancel_sample = False
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert app._cancel_sample is True
            assert app.is_running, "the app must not quit mid-sample"
    _run(go())


def test_a_second_ctrl_c_quits_even_while_sampling(app):
    """The binding is priority, so a single-purpose handler would leave no way
    out at all if the flag ever stuck. An unquittable TUI is the worse bug."""
    async def go():
        async with app.run_test(size=(190, 45)) as pilot:
            await pilot.pause()
            app._sampling = True
            app._cancel_sample = True          # already cancelling
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert not app.is_running
    _run(go())


def test_ctrl_c_quits_when_nothing_is_sampling(app):
    async def go():
        async with app.run_test(size=(190, 45)) as pilot:
            await pilot.pause()
            app._sampling = False
            await pilot.press("ctrl+c")
            await pilot.pause()
            assert not app.is_running
    _run(go())


def test_the_block_title_survives_a_slightly_short_terminal(app):
    """The breakpoint used to sit right where people actually work.

    A 148x32 terminal kept its block title and 148x31 lost it, so a tmux status
    line or a one-row nudge silently changed how the app looks.
    """
    async def go():
        async with app.run_test(size=(148, 31)) as pilot:
            await pilot.pause()
            assert not app.screen.has_class("-short")
            cs = app.query_one("#now-playing").content_size
            banner = app.title_banner("Swans", "Screen Shot", cs.width or 0,
                                      (cs.height or 0) - 1)
            assert not banner.startswith("♪"), "block title lost at 31 rows"
    _run(go())


def test_a_genuinely_short_terminal_still_compacts(app):
    """The rule has to keep doing its job; only the threshold moved."""
    async def go():
        async with app.run_test(size=(148, 24)) as pilot:
            await pilot.pause()
            assert app.screen.has_class("-short")
    _run(go())


def test_resizing_redraws_the_header(app):
    """The banner is computed once per track and sized from the panel, so
    widening the window mid-song left the title in the plain text it chose
    when the panel was narrow -- indistinguishable from the figlet breaking."""
    async def go():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app._sync_key = ("swans", "a little god in my hands")
            await pilot.resize_terminal(301, 54)
            await pilot.pause()
            assert app._sync_key is None, "resize must invalidate the header"
    _run(go())
