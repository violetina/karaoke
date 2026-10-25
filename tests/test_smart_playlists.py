"""Smart playlists: shaping, flow order, and in-place syncing."""
from unittest.mock import MagicMock

import pytest

from karaoke import dj_suggest, smart_playlists as sp


def _row(tid, artist, title="Song"):
    return {"track_id": tid, "artist": artist, "title": f"{title} {tid}" if title == "Song" else title,
            "url": f"https://music.youtube.com/watch?v=vid{tid:08d}", "kind": "youtube_music"}


def test_song_key_drops_version_tags():
    assert sp.song_key("Call Me (12'' Version)") == "call me"
    assert sp.song_key("Call Me - 2001 Remaster") == "call me"
    assert sp.song_key("Heroes [feat. Tove Lo]") == "heroes"


def test_cap_allows_two_per_act_and_no_second_version_of_a_song():
    rows = [_row(1, "Blondie", "Call Me"), _row(2, "Blondie", "Call Me (12'' Version)"),
            _row(3, "Blondie", "Atomic"), _row(4, "Blondie", "Maria"), _row(5, "Bowie", "Heroes")]
    out = sp.cap_per_act(rows)
    assert [r["track_id"] for r in out] == [1, 3, 5]


def test_cap_keeps_one_version_of_a_song_across_acts():
    rows = [_row(1, "Muddy Waters", "Who's Gonna Be Your Sweet Man"),
            _row(2, "Rory Gallagher & Muddy Waters", "Who's Gonna Be Your Sweet Man"),
            _row(3, "Rory Gallagher", "Bullfrog Blues")]
    assert [r["track_id"] for r in sp.cap_per_act(rows)] == [1, 3]


def test_cap_stops_at_the_limit():
    rows = [_row(i, f"Act {i}") for i in range(10)]
    assert len(sp.cap_per_act(rows, limit=4)) == 4


def test_flow_order_follows_the_best_next_song(monkeypatch):
    rows = [_row(1, "A"), _row(2, "B"), _row(3, "C"), _row(4, "D")]
    tracks = {r["track_id"]: dj_suggest.Track(r["track_id"]) for r in rows}
    monkeypatch.setattr(dj_suggest, "_load", lambda ids, conn, osc: tracks)
    # Each song is followed best by the next id up, except 1 -> 3.
    best_next = {1: 3, 3: 2, 2: 4}
    ids = {id(t): tid for tid, t in tracks.items()}

    def score(seed, cand, strategy):
        return 1.0 if best_next.get(ids[id(seed)]) == ids[id(cand)] else 0.1
    monkeypatch.setattr(dj_suggest, "score", score)
    out = sp.flow_order(rows, MagicMock(), None)
    assert [r["track_id"] for r in out] == [1, 3, 2, 4]


def test_flow_order_keeps_one_act_apart_when_it_can(monkeypatch):
    rows = [_row(1, "A"), _row(2, "A"), _row(3, "B")]
    tracks = {r["track_id"]: dj_suggest.Track(r["track_id"]) for r in rows}
    monkeypatch.setattr(dj_suggest, "_load", lambda ids, conn, osc: tracks)
    monkeypatch.setattr(dj_suggest, "score", lambda s, c, st: 0.9 if c is tracks[2] else 0.5)
    out = sp.flow_order(rows, MagicMock(), None)
    assert [r["track_id"] for r in out] == [1, 3, 2]


def test_push_creates_a_missing_playlist():
    yt = MagicMock()
    yt.create_playlist.return_value = "PLnew"
    rule = sp.Rule("Smart: Test", "desc", lambda c, o: [])
    pid = sp.push_ytmusic(rule, [_row(1, "A"), _row(2, "B")], yt, existing={})
    assert pid == "PLnew"
    yt.create_playlist.assert_called_once_with(
        "Smart: Test", description="desc", video_ids=["vid00000001", "vid00000002"])


def test_push_replaces_contents_in_place():
    yt = MagicMock()
    yt.get_playlist.return_value = {"tracks": [
        {"videoId": "vid00000001", "setVideoId": "s1"},
        {"videoId": "old00000009", "setVideoId": "s9"}]}
    rule = sp.Rule("Smart: Test", "desc", lambda c, o: [])
    pid = sp.push_ytmusic(rule, [_row(1, "A"), _row(2, "B")], yt,
                          existing={"smart: test": "PLold"})
    assert pid == "PLold"                       # same id: links keep working
    yt.create_playlist.assert_not_called()
    yt.remove_playlist_items.assert_called_once_with(
        "PLold", [{"videoId": "old00000009", "setVideoId": "s9"}])
    yt.add_playlist_items.assert_called_once_with("PLold", ["vid00000002"])


def test_save_local_keeps_a_known_youtube_link(monkeypatch):
    saved = {}
    monkeypatch.setattr("karaoke.localcache.delete_saved_playlist", lambda pid, conn=None: None)
    monkeypatch.setattr("karaoke.localcache.save_playlist",
                        lambda pid, name, **kw: saved.update(pid=pid, name=name, **kw))
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = {"url": "https://music.youtube.com/playlist?list=PLx"}
    rule = sp.Rule("Smart: Dreamy", "floating", lambda c, o: [])
    assert sp.save_local(rule, [_row(1, "A")], conn) == "smart-smart-dreamy"
    assert saved["url"].endswith("PLx")
    assert saved["source_kind"] == "smart"
    assert saved["tracks"][0]["video_id"] == "vid00000001"


def test_refresh_dry_run_saves_nothing(monkeypatch):
    rule = sp.Rule("Smart: Test", "desc", lambda c, o: [1, 2])
    monkeypatch.setattr(sp, "RULES", (rule,))
    monkeypatch.setattr(sp, "build", lambda r, c, o: [_row(1, "A"), _row(2, "B")])
    monkeypatch.setattr(sp, "save_local", lambda *a, **k: pytest.fail("saved on a dry run"))
    out = sp.refresh(dry_run=True, conn=MagicMock(), os_client=MagicMock())
    assert out == [{"name": "Smart: Test", "count": 2, "first": ["A - Song 1", "B - Song 2"]}]


def test_every_rule_has_a_distinct_name():
    names = [r.name for r in sp.RULES]
    assert len(names) == len(set(names))
    assert all(n.startswith(("Genre: ", "Smart: ")) for n in names)


def test_genre_penalty_grades_same_related_unrelated_unknown():
    assert sp.genre_penalty("rock", "rock") == sp.GENRE_PENALTY["same"]
    assert sp.genre_penalty("rock", "punk") == sp.GENRE_PENALTY["related"]
    assert sp.genre_penalty("punk", "country") == sp.GENRE_PENALTY["unrelated"]
    assert sp.genre_penalty(None, "jazz") == sp.GENRE_PENALTY["unknown"]


def test_each_song_takes_the_family_its_list_leans_to():
    options = {1: ["electronic", "pop", "rock"], 2: ["rock"], 3: ["rock", "punk"], 4: ["pop"]}
    fam = sp.assign_families(options, {})
    assert fam == {1: "rock", 2: "rock", 3: "rock", 4: "pop"}


def test_a_genre_less_song_borrows_its_sound_alikes_family():
    tracks = {1: dj_suggest.Track(1, clap=[1.0, 0.0]), 2: dj_suggest.Track(2, clap=[0.99, 0.1]),
              3: dj_suggest.Track(3, clap=[0.0, 1.0]), 4: dj_suggest.Track(4, clap=[0.1, 1.0])}
    fam = sp.assign_families({1: [], 2: ["jazz"], 3: [], 4: ["metal"]}, tracks)
    assert fam[1] == "jazz" and fam[3] == "metal"


def test_a_genre_less_song_with_no_close_sound_stays_unknown():
    tracks = {1: dj_suggest.Track(1, clap=[1.0, 0.0]), 2: dj_suggest.Track(2, clap=[0.0, 1.0])}
    assert sp.assign_families({1: [], 2: ["jazz"]}, tracks)[1] is None


def test_odd_one_out_genres_only_fill():
    rows = [_row(i, f"Act {i}") for i in range(1, 7)]
    fam = {1: "rock", 2: "country", 3: "rock", 4: "rock", 5: None, 6: "rock"}
    kept = sp.prefer_common_families(rows, fam, limit=5)
    assert [r["track_id"] for r in kept] == [1, 3, 4, 5, 6]
    assert len(sp.prefer_common_families(rows, fam, limit=10)) == 6


def test_a_family_of_more_than_five_plays_as_one_block(monkeypatch):
    rows = [_row(i, f"Act {i}") for i in range(1, 10)]
    tracks = {r["track_id"]: dj_suggest.Track(r["track_id"]) for r in rows}
    monkeypatch.setattr(dj_suggest, "score", lambda s, c, st: 0.5)
    fam = {1: "rock", 2: "jazz", 3: "rock", 4: "jazz", 5: "rock", 6: "rock", 7: "rock",
           8: "rock", 9: "jazz"}
    out = [r["track_id"] for r in sp.flow_order(rows, MagicMock(), None, families=fam, tracks=tracks)]
    rock = [i for i, t in enumerate(out) if fam[t] == "rock"]
    assert rock == list(range(rock[0], rock[0] + 6))


def test_flow_prefers_the_smaller_emotional_jump(monkeypatch):
    rows = [_row(1, "A"), _row(2, "B"), _row(3, "C")]
    tracks = {1: dj_suggest.Track(1, end=(0.8, 0.5, "joyful")),
              2: dj_suggest.Track(2, start=(-0.8, -1.0, "sad")),
              3: dj_suggest.Track(3, start=(0.7, 0.4, "joyful"))}
    monkeypatch.setattr(dj_suggest, "score", lambda s, c, st: 0.5)
    out = sp.flow_order(rows, MagicMock(), None, tracks=tracks)
    assert [r["track_id"] for r in out] == [1, 3, 2]


def test_genre_less_songs_can_sit_inside_a_block(monkeypatch):
    rows = [_row(i, f"Act {i}") for i in range(1, 10)]
    tracks = {r["track_id"]: dj_suggest.Track(r["track_id"]) for r in rows}
    # Song 9 (no genre) follows song 1 best; everything else is level.
    monkeypatch.setattr(dj_suggest, "score",
                        lambda s, c, st: 0.9 if (s.track_id, c.track_id) == (1, 9) else 0.5)
    fam = {i: "rock" for i in range(1, 8)} | {8: "jazz", 9: None}
    out = [r["track_id"] for r in sp.flow_order(rows, MagicMock(), None, families=fam, tracks=tracks)]
    assert out[1] == 9
    rock = [i for i, t in enumerate(out) if fam[t] == "rock"]
    assert all(fam[out[i]] in ("rock", None) for i in range(rock[0], rock[-1] + 1))


def test_two_spellings_of_one_artist_are_one_act():
    rows = [_row(1, 'Bonnie "Prince" Billy'), _row(2, "Bonnie Prince Billy"),
            _row(3, "Bonnie 'Prince' Bill"), _row(4, "Bowie")]
    acts = {1: "bpb", 2: "bpb", 3: "bpb"}
    assert [r["track_id"] for r in sp.cap_per_act(rows, acts=acts)] == [1, 2, 4]
    assert len(sp.cap_per_act(rows)) == 4


def test_act_keys_prefer_the_first_credited_mbid(monkeypatch):
    from karaoke import localcache

    rows = [_row(1, "Gorillaz, Fatoumata Diawara"), _row(2, "The Slits")]
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = [{"track_id": 1, "mbid": "gorillaz-mbid"}]
    monkeypatch.setattr(localcache, "table_exists", lambda c, t: True)
    assert sp.act_keys(rows, conn) == {1: "gorillaz-mbid", 2: "slits"}


def test_flow_order_keeps_one_act_apart_across_spellings(monkeypatch):
    rows = [_row(1, "Bonnie Prince Billy"), _row(2, 'Bonnie "Prince" Billy'), _row(3, "Bowie")]
    tracks = {r["track_id"]: dj_suggest.Track(r["track_id"]) for r in rows}
    monkeypatch.setattr(dj_suggest, "score", lambda s, c, st: 0.9 if c.track_id == 2 else 0.5)
    out = sp.flow_order(rows, MagicMock(), None, tracks=tracks, acts={1: "bpb", 2: "bpb"})
    assert [r["track_id"] for r in out] == [1, 3, 2]


def test_a_weak_family_is_not_an_option():
    from karaoke import localcache

    with localcache.connect() as conn:
        localcache.ensure_artist_genres_table(conn)
        for genre, broad, w in (("progressive rock", "rock", 13), ("art rock", "rock", 11),
                                ("jazz fusion", "jazz", 9), ("modern classical", "classical", 4)):
            conn.execute("INSERT INTO artist_genres VALUES ('frank zappa', %s, %s, %s, 'musicbrainz', 0)",
                         (genre, broad, w))
        conn.commit()
        assert sp.genre_options([_row(1, "Frank Zappa")], conn) == {1: ["rock"]}
