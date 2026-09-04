"""Tests for the expanded Textual TUI helper functions."""

from karaoke.tui import (
    _default_sync_offset,
    caption_is_synced,
    first_nonempty_line,
    lyric_preview,
    mood_for_preview,
)


def test_lyric_preview_prefers_synced_lyrics():
    song = {
        "synced_lyrics": "[00:01.00] I love this\n[00:02.00] ignored later",
        "plain_lyrics": "plain fallback",
    }

    assert lyric_preview(song, max_lines=1) == "[00:01.00] I love this"


def test_lyric_preview_falls_back_to_plain_lyrics():
    song = {"synced_lyrics": "", "plain_lyrics": "\nhello\nworld\n"}

    assert lyric_preview(song) == "hello\nworld"


def test_mood_for_preview_uses_first_non_neutral_line():
    assert mood_for_preview("la la la\nI love you") == "tender"


def test_first_nonempty_line():
    assert first_nonempty_line("\n  \n  seed text  \nnext") == "seed text"


def test_caption_is_synced_gates_autoload():
    assert caption_is_synced("youtube_caption_manual_en-US_enhanced")
    assert caption_is_synced("youtube_caption_automatic_en_synced")
    assert not caption_is_synced("youtube_caption_manual_en_plain")
    assert not caption_is_synced("")


def test_default_sync_offset_from_env(monkeypatch):
    monkeypatch.setenv("KARAOKE_SYNC_OFFSET", "0.7")
    assert _default_sync_offset() == 0.7


def test_default_sync_offset_fallback_on_bad_value(monkeypatch):
    monkeypatch.setenv("KARAOKE_SYNC_OFFSET", "not-a-number")
    assert _default_sync_offset() == 0.0


def test_default_sync_offset_default(monkeypatch):
    monkeypatch.delenv("KARAOKE_SYNC_OFFSET", raising=False)
    assert _default_sync_offset() == 0.0


def test_default_sync_offset_spotify_is_zero(monkeypatch):
    # Spotify reports an accurate native position, so no browser-lag offset.
    monkeypatch.delenv("KARAOKE_SYNC_OFFSET_SPOTIFY", raising=False)
    assert _default_sync_offset("spotify") == 0.0


def test_default_sync_offset_spotify_env_override(monkeypatch):
    monkeypatch.setenv("KARAOKE_SYNC_OFFSET_SPOTIFY", "0.3")
    assert _default_sync_offset("spotify") == 0.3


def test_default_sync_offset_scan_unaffected_by_spotify_env(monkeypatch):
    monkeypatch.setenv("KARAOKE_SYNC_OFFSET_SPOTIFY", "0.3")
    monkeypatch.delenv("KARAOKE_SYNC_OFFSET", raising=False)
    assert _default_sync_offset("scan") == 0.0


def test_sync_offset_get_set_roundtrip(tmp_path):
    from karaoke import localcache
    from karaoke.lyrics import Lyrics

    c = localcache.connect(tmp_path / "karaoke.db")
    localcache.add_track_and_lyrics("A", "B", Lyrics(plain="x", source="lrclib"), conn=c)
    tid = localcache.find_track_id("A", "B", c)
    assert tid is not None

    # No offset saved yet.
    assert localcache.get_sync_offset(tid, c) is None

    localcache.set_sync_offset(tid, 1.4, c)
    assert localcache.get_sync_offset(tid, c) == 1.4

    # Upsert replaces, does not duplicate.
    localcache.set_sync_offset(tid, -0.5, c)
    assert localcache.get_sync_offset(tid, c) == -0.5
    assert c.execute("SELECT count(*) FROM track_sync_offsets").fetchone()[0] == 1


def test_sync_offset_none_track_is_safe(tmp_path):
    from karaoke import localcache

    c = localcache.connect(tmp_path / "karaoke.db")
    assert localcache.get_sync_offset(None, c) is None


def test_mood_square_glyph_rows_are_uniform_width():
    """All five squares must be the same visual width.

    "☔" and "🔥" are 2 cells while "☀ ♡ ◇" are 1, so the unpadded squares
    rendered lopsided under `content-align: center`.
    """
    from karaoke import visuals
    from karaoke.tui import MOOD_GLYPHS

    for mood, art in MOOD_GLYPHS.items():
        rows = art.splitlines()
        assert len(rows) == 3, mood
        assert {visuals.cell_width(r) for r in rows} == {2}, mood


# --- help screen / bindings ------------------------------------------------

def test_binding_rows_are_generated_from_bindings():
    from karaoke.tui import KaraokeTui, binding_rows

    rows = dict(binding_rows(KaraokeTui.BINDINGS))
    assert rows["H"] == "Browse"
    assert rows["?"] == "Keys"
    assert rows[","] == "Lyrics -0.1s"
    assert rows["["] == "Seek -5s"


def test_binding_rows_omit_hidden_bindings():
    """escape is bound but show=False, so it stays out of the footer and help."""
    from karaoke.tui import KaraokeTui, binding_rows

    assert "Close browse" not in dict(binding_rows(KaraokeTui.BINDINGS)).values()


def test_every_binding_has_a_matching_action():
    """Catches a typo'd action name, which would otherwise fail only at runtime."""
    from textual.binding import Binding
    from karaoke.tui import KaraokeTui

    for binding in Binding.make_bindings(KaraokeTui.BINDINGS):
        action = binding.action.split("(")[0]
        assert hasattr(KaraokeTui, f"action_{action}") or hasattr(
            KaraokeTui, action), action


def test_binding_keys_are_unique():
    from textual.binding import Binding
    from karaoke.tui import KaraokeTui

    keys = [b.key for b in Binding.make_bindings(KaraokeTui.BINDINGS)]
    assert len(keys) == len(set(keys)), f"duplicate key: {keys}"


def test_help_table_has_a_row_per_binding():
    from karaoke.tui import KaraokeTui, binding_rows, help_table

    rows = binding_rows(KaraokeTui.BINDINGS)
    assert help_table(rows).row_count == len(rows)


def test_confirm_screen_can_be_escaped():
    """escape must resolve to an explicit False, not a bare dismiss()."""
    from textual.binding import Binding
    from karaoke.tui import ConfirmScreen

    keys = {b.key: b.action for b in Binding.make_bindings(ConfirmScreen.BINDINGS)}
    assert keys.get("escape") == "cancel"
    assert hasattr(ConfirmScreen, "action_cancel")


# --- browse overlay --------------------------------------------------------

class _FakeOverlay:
    def __init__(self):
        self.classes = set()
        self.calls = []

    def add_class(self, name):
        self.calls.append(("add_class", name))
        self.classes.add(name)

    def remove_class(self, name):
        self.calls.append(("remove_class", name))
        self.classes.discard(name)

    def has_class(self, name):
        return name in self.classes


class _FakeTable:
    def __init__(self, log):
        self._log = log

    def focus(self):
        self._log.append(("focus", "library"))


def _app_with_fakes(monkeypatch, *, open_=False):
    """A KaraokeTui whose query_one returns fakes, so no event loop is needed."""
    from karaoke.tui import KaraokeTui

    app = KaraokeTui.__new__(KaraokeTui)          # skip App.__init__
    overlay = _FakeOverlay()
    if open_:
        overlay.classes.add(KaraokeTui._BROWSE_OPEN)
    calls = overlay.calls
    table = _FakeTable(calls)

    def fake_query_one(selector, *args, **kwargs):
        if "browse-overlay" in selector:
            return overlay
        if "library" in selector:
            return table
        raise AssertionError(f"unexpected query_one({selector!r})")

    monkeypatch.setattr(app, "query_one", fake_query_one, raising=False)
    monkeypatch.setattr(app, "set_focus",
                        lambda w: calls.append(("set_focus", w)), raising=False)
    return app, overlay, calls


def test_show_browse_reveals_then_focuses(monkeypatch):
    """Order matters: a hidden widget silently refuses focus."""
    from karaoke.tui import KaraokeTui

    app, overlay, calls = _app_with_fakes(monkeypatch)
    app._show_browse()

    assert overlay.has_class(KaraokeTui._BROWSE_OPEN)
    assert calls == [("add_class", "-visible"), ("focus", "library")]


def test_hide_browse_releases_focus(monkeypatch):
    app, overlay, calls = _app_with_fakes(monkeypatch, open_=True)
    app._hide_browse()

    assert not overlay.has_class("-visible")
    assert ("set_focus", None) in calls


def test_toggle_browse_round_trips(monkeypatch):
    app, overlay, _ = _app_with_fakes(monkeypatch)
    app.action_toggle_browse()
    assert overlay.has_class("-visible")
    app.action_toggle_browse()
    assert not overlay.has_class("-visible")


def test_escape_is_a_noop_when_browse_is_closed(monkeypatch):
    app, overlay, calls = _app_with_fakes(monkeypatch)
    app.action_hide_browse()
    assert calls == []


def test_open_selected_hides_overlay_on_success(monkeypatch):
    from karaoke import tui

    app, overlay, _ = _app_with_fakes(monkeypatch, open_=True)
    monkeypatch.setattr(app, "_selected_song",
                        lambda: {"url": "https://youtu.be/x", "kind": "youtube",
                                 "artist": "A", "title": "B"}, raising=False)
    monkeypatch.setattr(tui, "open_song_url", lambda url, kind: 123)
    monkeypatch.setattr(app, "notify", lambda *a, **k: None, raising=False)

    app._open_selected()
    assert not overlay.has_class("-visible")


def test_open_selected_keeps_overlay_when_opening_fails(monkeypatch):
    """The failure reason lands in #now-playing, which sits behind the overlay."""
    from karaoke import tui

    app, overlay, _ = _app_with_fakes(monkeypatch, open_=True)
    monkeypatch.setattr(app, "_selected_song",
                        lambda: {"url": "https://youtu.be/x", "kind": "youtube",
                                 "artist": "A", "title": "B"}, raising=False)
    monkeypatch.setattr(tui, "open_song_url",
                        lambda url, kind: (_ for _ in ()).throw(RuntimeError("nope")))
    monkeypatch.setattr(app, "notify", lambda *a, **k: None, raising=False)

    class _NP:
        def update(self, _): pass
    monkeypatch.setattr(app, "query_one",
                        lambda sel, *a, **k: _NP() if "now-playing" in sel
                        else (overlay if "browse-overlay" in sel else _NP()),
                        raising=False)

    app._open_selected()
    assert overlay.has_class("-visible")


# --- A: approve for post-processing ---------------------------------------

def _approver(monkeypatch, tmp_path):
    """A KaraokeTui wired to a temp DB, without booting the app."""
    from karaoke import localcache
    from karaoke.tui import KaraokeTui

    conn = localcache.connect(tmp_path / "k.db")
    monkeypatch.setattr(localcache, "connect", lambda *a, **k: conn)
    app = KaraokeTui.__new__(KaraokeTui)
    return app, conn


def _add_track(conn, artist="A", title="B", *, synced=True, plain=True):
    from karaoke import localcache
    from karaoke.lyrics import Lyrics
    localcache.add_track_and_lyrics(
        artist, title,
        Lyrics(plain="w" if plain else "",
               synced_raw="[00:01.00] w" if synced else "", source="lrclib"),
        url="https://youtu.be/x", conn=conn)


def test_approve_refuses_when_nothing_is_playing(monkeypatch, tmp_path):
    app, _ = _approver(monkeypatch, tmp_path)
    ok, msg = app.approve_postprocess("", "")
    assert not ok and "Nothing playing" in msg


def test_approve_refuses_a_track_not_in_the_library(monkeypatch, tmp_path):
    app, _ = _approver(monkeypatch, tmp_path)
    ok, msg = app.approve_postprocess("Ghost", "Missing")
    assert not ok and "not in the library" in msg


def test_approve_refuses_when_there_are_no_lyrics(monkeypatch, tmp_path):
    """"if text found" — with none, the gap queue is the right path, not this."""
    app, conn = _approver(monkeypatch, tmp_path)
    _add_track(conn, synced=False, plain=False)
    ok, msg = app.approve_postprocess("A", "B")
    assert not ok and "No lyrics" in msg


def test_approve_queues_when_lyrics_exist(monkeypatch, tmp_path):
    from karaoke import postprocess_queue as pq
    app, conn = _approver(monkeypatch, tmp_path)
    _add_track(conn)
    published = []
    monkeypatch.setattr(pq, "publish_postprocess_task",
                        lambda a, t, u="": published.append((a, t, u)) or True)

    ok, msg = app.approve_postprocess("A", "B", "https://youtu.be/x")
    assert ok, msg
    assert published == [("A", "B", "https://youtu.be/x")]
    assert "analysis" in msg          # what it actually queued


def test_approve_reports_an_unreachable_broker(monkeypatch, tmp_path):
    from karaoke import postprocess_queue as pq
    app, conn = _approver(monkeypatch, tmp_path)
    _add_track(conn)
    monkeypatch.setattr(pq, "publish_postprocess_task", lambda *a, **k: False)

    ok, msg = app.approve_postprocess("A", "B")
    assert not ok and "Broker unreachable" in msg


def test_approve_clears_the_session_guard(monkeypatch, tmp_path):
    """So a track can be re-queued after the broker comes back."""
    from karaoke import postprocess_queue as pq
    app, conn = _approver(monkeypatch, tmp_path)
    _add_track(conn)
    monkeypatch.setattr(pq, "publish_postprocess_task", lambda *a, **k: True)
    app._current_song = ("A", "B", "")
    app._postprocess_enqueued = {("a", "b")}
    monkeypatch.setattr(app, "notify", lambda *a, **k: None, raising=False)

    app.action_approve_postprocess()
    assert ("a", "b") not in app._postprocess_enqueued


# --- background lyric fetch ------------------------------------------------

def test_background_fetch_caches_lyrics_and_forces_a_resync(monkeypatch, tmp_path):
    """The cache-only detection path could never gain lyrics on its own."""
    from karaoke import localcache, tui
    from karaoke.lyrics import Lyrics
    from karaoke.tui import KaraokeTui

    conn = localcache.connect(tmp_path / "k.db")
    monkeypatch.setattr(localcache, "connect", lambda *a, **k: conn)
    monkeypatch.setattr(tui, "fetch_lrclib", None, raising=False)
    monkeypatch.setattr("karaoke.lyrics.fetch_lrclib",
                        lambda a, t, *args, **kw: Lyrics(
                            plain="w", synced_raw="[00:01.00] w", source="lrclib"))

    app = KaraokeTui.__new__(KaraokeTui)
    app._sync_key = ("a", "b")
    app._background_fetch_lyrics("A", "B")

    assert localcache.get_cached_lyrics("A", "B", conn=conn) is not None
    assert app._sync_key is None          # next poll re-resolves


def test_background_fetch_is_quiet_on_a_miss(monkeypatch, tmp_path):
    from karaoke import localcache
    from karaoke.lyrics import Lyrics
    from karaoke.tui import KaraokeTui

    conn = localcache.connect(tmp_path / "k.db")
    monkeypatch.setattr(localcache, "connect", lambda *a, **k: conn)
    monkeypatch.setattr("karaoke.lyrics.fetch_lrclib",
                        lambda a, t, *args, **kw: Lyrics())

    app = KaraokeTui.__new__(KaraokeTui)
    app._sync_key = ("a", "b")
    app._background_fetch_lyrics("A", "B")

    assert localcache.get_cached_lyrics("A", "B", conn=conn) is None
    assert app._sync_key == ("a", "b")    # nothing changed, no needless resync


def test_background_fetch_survives_a_network_error(monkeypatch, tmp_path):
    from karaoke import localcache
    from karaoke.tui import KaraokeTui

    conn = localcache.connect(tmp_path / "k.db")
    monkeypatch.setattr(localcache, "connect", lambda *a, **k: conn)

    def boom(*a, **k):
        raise OSError("no network")
    monkeypatch.setattr("karaoke.lyrics.fetch_lrclib", boom)

    app = KaraokeTui.__new__(KaraokeTui)
    app._sync_key = ("a", "b")
    app._background_fetch_lyrics("A", "B")     # must not raise


def test_background_fetch_ignores_a_blank_track(monkeypatch, tmp_path):
    from karaoke.tui import KaraokeTui
    app = KaraokeTui.__new__(KaraokeTui)
    app._background_fetch_lyrics("", "")       # must not raise or hit the net


# --- mic / radio mode ------------------------------------------------------

def _mic_app(ref=None, stopped=True):
    from karaoke.tui import KaraokeTui
    app = KaraokeTui.__new__(KaraokeTui)
    app._mic_ref = ref
    app._mic_stop = None if stopped else __import__("threading").Event()
    app._mode_override = None
    return app


def _ref(artist="A", title="B", offset=30.0, mono=100.0):
    from karaoke.identify import SongRef
    return SongRef(artist=artist, title=title, source="songrec",
                   offset=offset, offset_mono=mono)


def test_mic_elapsed_dead_reckons_from_the_anchor():
    """No MPRIS position exists in radio mode; it is offset + time since."""
    app = _mic_app(_ref(offset=30.0, mono=100.0))
    assert app.mic_elapsed(now=100.0) == 30.0
    assert app.mic_elapsed(now=112.5) == 42.5


def test_mic_elapsed_is_none_without_an_identification():
    assert _mic_app(None).mic_elapsed() is None


def test_mic_elapsed_is_none_when_songrec_gave_no_offset():
    """songrec can name a track without locating the playhead in it."""
    app = _mic_app(_ref(offset=None, mono=None))
    assert app.mic_elapsed() is None


def test_mic_identification_wins_over_mpris(monkeypatch):
    """The mic hears the room — that is the point of switching it on."""
    from karaoke import detect

    app = _mic_app(_ref("Otis Redding", "Dock of the Bay"))
    monkeypatch.setattr(detect, "detect_active",
                        lambda: detect.Detection(mode="scan", artist="Other",
                                                 title="Wrong Song"))
    det = app._effective_detection()
    assert det.mode == "radio"
    assert (det.artist, det.title) == ("Otis Redding", "Dock of the Bay")


def test_mpris_is_used_when_the_mic_is_off(monkeypatch):
    from karaoke import detect

    app = _mic_app(None)
    monkeypatch.setattr(detect, "detect_active",
                        lambda: detect.Detection(mode="scan", artist="X", title="Y"))
    assert app._effective_detection().mode == "scan"


def test_radio_detection_counts_as_active():
    """There is no player to control, but there is a song to follow."""
    from karaoke import detect
    assert detect.Detection(mode="radio", artist="A", title="B").is_active


def test_toggle_mic_refuses_when_the_cli_radio_holds_it(monkeypatch):
    from karaoke import tui

    app = _mic_app(None)
    monkeypatch.setattr(tui.shutil if hasattr(tui, "shutil") else tui,
                        "__name__", "tui", raising=False)
    monkeypatch.setattr(tui, "_radio_cli_running", lambda: True)
    notes = []
    monkeypatch.setattr(app, "notify",
                        lambda m, **k: notes.append((m, k.get("severity"))),
                        raising=False)
    started = []
    monkeypatch.setattr(app, "run_worker",
                        lambda *a, **k: started.append(a), raising=False)

    app.action_toggle_mic()
    assert not started
    assert "already using the mic" in notes[0][0]


def test_toggle_mic_off_clears_state(monkeypatch):
    import threading
    app = _mic_app(_ref(), stopped=False)
    notes = []
    monkeypatch.setattr(app, "notify", lambda m, **k: notes.append(m), raising=False)
    app._sync_key = ("a", "b")

    app.action_toggle_mic()
    assert app._mic_stop.is_set()
    assert app._mic_ref is None
    assert app._sync_key is None          # forces a re-resolve back to MPRIS
    assert notes == ["Mic off"]


def test_context_window_scales_to_the_panel_height():
    """A full-height pane showed 8 lines and left the rest empty."""
    from rich.text import Text
    from karaoke.player import LyricTimeline, _render_body

    lines = [(float(i * 4), f"lyric line {i}") for i in range(40)]
    tl = LyricTimeline(lines)

    def shown(rows):
        before, after = (3, 5) if rows < 10 else (rows // 3, rows - rows // 3)
        body = Text()
        _render_body(body, tl, 60.0, before=before, after=after)
        return len(body.plain.rstrip().splitlines())

    assert shown(12) > 8            # more than the old hard-coded window
    assert shown(40) > shown(12)    # and it keeps scaling


# --- figlet title banner ---------------------------------------------------

def _banner(title, width, height=99, artist="Gotye"):
    from karaoke.tui import KaraokeTui
    return KaraokeTui.__new__(KaraokeTui).title_banner(artist, title, width, height)


def test_title_renders_as_block_type_when_it_fits():
    """A header is one short string with space around it — block type suits it."""
    out = _banner("Golden Brown", 150)
    assert "♪" not in out
    assert out.count("\n") >= 3          # several block rows
    assert out.strip().endswith("Gotye")  # artist under the title


def test_title_falls_back_to_plain_when_too_narrow():
    assert _banner("Somebody That I Used To Know", 60) == \
        "♪ Gotye - Somebody That I Used To Know"


def test_title_falls_back_when_the_header_is_compacted():
    """On a short terminal now-playing shrinks to 3 rows; a banner cannot fit."""
    assert _banner("Golden Brown", 150, height=3).startswith("♪ ")


def test_title_falls_back_for_a_title_too_long_to_fit_one_line():
    long_title = "A Really Very Extremely Long Song Title That Cannot Possibly Fit"
    assert _banner(long_title, 120).startswith("♪ ")


def test_title_banner_never_wraps_to_a_second_block():
    """max_rows=1: a header that wrapped would push the status line out."""
    out = _banner("Disappearer", 150)
    rows = out.splitlines()
    assert len({len(r) for r in rows[:-1]}) == 1     # block rows equal width
