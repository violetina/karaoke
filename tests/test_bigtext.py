"""Tests for the big-type lyric renderer (pure, no event loop)."""
from rich.cells import cell_len

from karaoke import bigtext as bt

LINE = "Little fish in a great big sea"


# --- folding ---------------------------------------------------------------

def test_fold_strips_accents():
    assert bt.fold("café") == "cafe"


def test_fold_normalises_typography():
    assert bt.fold("don’t") == "don't"
    assert bt.fold("“hi”") == '"hi"'
    assert bt.fold("a–b") == "a-b"


def test_fold_leaves_plain_text_alone():
    assert bt.fold("Little Fish") == "Little Fish"


# --- geometry --------------------------------------------------------------

def test_rendered_rows_are_all_the_same_width():
    line = bt.render_line(LINE)
    assert len({len(r) for r in line.rows}) == 1
    assert line.width == len(line.rows[0])


def test_rendered_rows_measure_what_they_claim():
    """Width is in display cells, so the caller can compare it to a panel."""
    line = bt.render_line(LINE)
    for row in line.rows:
        assert cell_len(row) == line.width


def test_words_share_a_baseline():
    """A lowercase word has a blank top row; it must not float upward.

    Trimming blank rows per word instead of per line would sit "a" higher
    than "La".
    """
    both = bt.render_line("La a")
    solo_l = bt.render_line("La")
    assert len(both.rows) == len(solo_l.rows)
    assert both.rows[0].startswith(solo_l.rows[0].rstrip()[:2])


def test_accented_text_renders_like_its_ascii_form():
    assert bt.render_line("café").rows == bt.render_line("cafe").rows


# --- spans (what word highlighting depends on) -----------------------------

def test_spans_partition_the_row_contiguously():
    line = bt.render_line("AB CD")
    assert line.spans[0].start == 0
    for prev, nxt in zip(line.spans, line.spans[1:]):
        assert prev.end == nxt.start
    assert line.spans[-1].end == line.width


def test_spans_alternate_word_gap_word():
    line = bt.render_line("AB CD")
    assert [s.word for s in line.spans] == [0, -1, 1]


def test_span_columns_actually_contain_that_word():
    """Slicing a row by a span must yield that word's own glyphs."""
    line = bt.render_line("AB CD")
    solo = bt.render_line("CD")
    span = next(s for s in line.spans if s.word == 1)
    for row, solo_row in zip(line.rows, solo.rows):
        assert row[span.start:span.end] == solo_row


def test_single_word_has_one_span():
    line = bt.render_line("sea")
    assert [s.word for s in line.spans] == [0]


# --- wrapping --------------------------------------------------------------

def test_wrap_never_splits_or_loses_a_word():
    fragments = bt.wrap(LINE, 110)
    assert fragments
    assert " ".join(fragments).split() == LINE.split()


def test_wrap_fragments_each_fit():
    for fragment in bt.wrap(LINE, 110):
        assert bt.render_line(fragment).width <= 110


def test_wrap_refuses_an_unsplittable_word():
    assert bt.wrap("supercalifragilisticexpialidocious", 40) is None


# --- the fallback ladder ---------------------------------------------------

def test_short_line_renders_as_one_block():
    out = bt.render("sea", 200)
    assert out is not None and len(out) == 1


def test_long_line_wraps_within_max_rows():
    out = bt.render(LINE, 110)
    assert out is not None and len(out) == 2


def test_wrapped_fragments_share_a_height():
    """Descenders in one fragment must not make it taller than the other."""
    out = bt.render(LINE, 110)
    assert len({len(b.rows) for b in out}) == 1


def test_max_rows_is_enforced():
    assert bt.render(LINE, 110, max_rows=1) is None


def test_narrow_width_refuses():
    assert bt.render("sea", 20) is None
    assert bt.render("sea", bt.MIN_WIDTH - 1) is None


def test_blank_text_refuses():
    assert bt.render("   ", 200) is None
    assert bt.render("", 200) is None


def test_unrenderable_script_refuses():
    """Better the plain renderer than a line of blanks and question marks."""
    assert bt.render("日本語のうた", 200) is None
    assert bt.render("Москва", 200) is None


def test_mostly_ascii_with_a_stray_symbol_still_renders():
    out = bt.render("hello world ♥", 200)
    assert out is not None


def test_word_numbering_continues_across_a_wrap():
    """The highlight has to keep counting past the line break."""
    out = bt.render(LINE, 110)
    words = [s.word for block in out for s in block.spans if s.word >= 0]
    assert words == list(range(len(LINE.split())))


# --- context window --------------------------------------------------------

def test_context_window_favours_upcoming_lines():
    before, after = bt.context_window(10, 3)
    assert before + after == 6
    assert after > before


def test_context_window_collapses_when_there_is_no_room():
    assert bt.context_window(4, 3) == (0, 0)
    assert bt.context_window(1, 5) == (0, 0)


# --- caching ---------------------------------------------------------------

def test_render_is_cached():
    a = bt.render(LINE, 110)
    b = bt.render(LINE, 110)
    assert a is b
