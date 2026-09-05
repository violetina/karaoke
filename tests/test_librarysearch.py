"""Weighted search across the library.

One box, ranked results. The weights exist so that typing a song name finds
the song rather than every track by an artist whose name shares letters with
it -- title first, then album, artist, and lyrics as the last resort.
"""
import pytest

from karaoke import librarysearch as ls


class _Row(dict):
    """Stands in for a sqlite3.Row, which supports keys() and [] access."""

    def keys(self):
        return list(super().keys())


def _row(title="", artist="", album="", words=""):
    return _Row(track_id=1, title=title, artist=artist, album=album, words=words)


# -- how well one field matches -----------------------------------------

def test_an_exact_field_beats_a_prefix_beats_a_word_beats_a_substring():
    assert (ls.field_score("River", "river")
            > ls.field_score("River Deep", "river")
            > ls.field_score("The River Wild", "river")
            > ls.field_score("Riverside", "river"))


def test_matching_ignores_case_and_padding():
    assert ls.field_score("  The   RIVER ", "the river") == ls.EXACT


def test_no_match_scores_nothing():
    assert ls.field_score("Fearless", "banana") == 0.0
    assert ls.field_score("", "river") == 0.0
    assert ls.field_score("River", "") == 0.0


# -- the field priority the user asked for ------------------------------

def test_title_outranks_album_outranks_artist():
    """Typing a name should find the song, not everything by a similarly
    named artist."""
    by_title = ls.score_row(_row(title="Rain"), "rain")[0]
    by_album = ls.score_row(_row(album="Rain"), "rain")[0]
    by_artist = ls.score_row(_row(artist="Rain"), "rain")[0]
    assert by_title > by_album > by_artist


def test_lyrics_rank_last():
    by_artist = ls.score_row(_row(artist="Rain"), "rain")[0]
    by_lyrics = ls.score_row(_row(title="X", words="the rain falls"), "rain")[0]
    assert by_artist > by_lyrics > 0


def test_matching_several_fields_beats_matching_one():
    """A track matching title and artist is a better answer than either alone."""
    both = ls.score_row(_row(title="Rain", artist="Rain"), "rain")[0]
    one = ls.score_row(_row(title="Rain"), "rain")[0]
    assert both > one


def test_the_contributing_fields_are_reported_best_first():
    _, fields = ls.score_row(_row(title="Rain", artist="Rain Dogs"), "rain")
    assert fields[0] == "title"
    assert set(fields) == {"title", "artist"}


# -- lyrics are matched by word, not substring --------------------------

def test_lyrics_match_whole_words_only():
    """Substring matching would make "love" hit "glove" in every song, so the
    lyric field would match nearly everything instead of nothing."""
    assert ls.lyrics_score("I wear a glove", "love") == 0.0
    assert ls.lyrics_score("I need your love", "love") > 0.0


def test_lyrics_can_be_switched_off():
    row = _row(title="X", words="the rain falls")
    assert ls.score_row(row, "rain", search_lyrics=False)[0] == 0.0


# -- the whole search ---------------------------------------------------

@pytest.fixture()
def conn(tmp_path):
    from karaoke import localcache

    c = localcache.connect(tmp_path / "t.db")
    c.executescript("""
        INSERT INTO tracks (track_id, artist, title, album) VALUES
            (1, 'Pink Floyd', 'Fearless', 'Meddle'),
            (2, 'Blue Oyster Cult', "(Don't Fear) The Reaper", ''),
            (3, 'Fearless Band', 'Something Else', ''),
            (4, 'Someone', 'Unrelated', '');
        INSERT INTO lyrics (track_id, kind, plain_lyrics) VALUES
            (4, 'approved', 'and you go fearless into the night');
        INSERT INTO sources (track_id, kind, url) VALUES
            (1, 'youtube', 'https://youtu.be/abc'),
            (1, 'spotify', 'https://open.spotify.com/track/x');
    """)
    c.commit()
    yield c
    c.close()


def test_the_exact_title_wins(conn):
    hits = ls.search("fearless", conn)
    assert hits[0].title == "Fearless"


def test_an_artist_match_outranks_a_lyric_match(conn):
    names = [h.artist for h in ls.search("fearless", conn)]
    assert names.index("Fearless Band") < names.index("Someone")


def test_a_partial_word_still_finds_the_title(conn):
    assert any(h.track_id == 2 for h in ls.search("reaper", conn))


def test_an_empty_query_returns_nothing(conn):
    assert ls.search("", conn) == []
    assert ls.search("   ", conn) == []


def test_no_matches_is_empty_not_an_error(conn):
    assert ls.search("zzzznothing", conn) == []


def test_results_are_capped(conn):
    assert len(ls.search("e", conn, limit=2)) <= 2


# -- somewhere to play from ---------------------------------------------

def test_a_browser_url_is_preferred_over_spotify(conn):
    """The queue opens URLs, and a YouTube link plays without an app."""
    assert ls.playable_url(1, conn) == "https://youtu.be/abc"


def test_a_track_with_no_source_has_no_url(conn):
    assert ls.playable_url(4, conn) is None
