"""Render a lyric line as large figlet-style type, keeping word positions.

NOT CURRENTLY WIRED IN — kept deliberately, not dead code left behind. The TUI
renders the active line bold at normal width because every "bigger" option was
worse in practice: this module's block type was unreadable at the sizes that fit
the panel, Unicode fullwidth forms drew tofu in the terminal font, and letter
spacing broke the reading rhythm. Keep it for a future display with more room
(a projector, a wider pane), where block type would come into its own.

Fully tested, so re-wiring it is a one-line call to `render()` from
`player._render_body`. Requires the `pyfiglet` dependency.


The active line deserves the screen, but a big renderer that loses the
word-level highlight would be a downgrade — knowing *which word* is being sung
is the point of a karaoke display. So this module renders **word by word** and
records the column range each word occupies, letting the caller style just
those columns as the singer reaches them.

Two details that are easy to get wrong:

- **Blank rows are trimmed across the whole line, never per word.** In the
  ``small`` font "Little" fills row 0 while a lowercase "a" leaves it empty;
  trimming each word independently would sit them at different heights.
- **A line that does not fit is refused, not squeezed.** ``render`` returns
  ``None`` and the caller falls back to the ordinary renderer, which is always
  legible. Big type is an enhancement, never a downgrade.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

# Chosen by eye over mini/cybermedium/straight/thin: `small` is both narrower
# (~3.5 cells per character) and more legible than the alternatives.
FONT = "small"

# A shorter font for a second line. "threepoint" is three rows against small's
# five, which is what makes an artist read as a byline under the title rather
# than as a second heading -- and it is what keeps the header from growing when
# the artist joined it.
SMALL_FONT = "threepoint"

# Below this many cells big type is not worth attempting — the wrap would be
# so aggressive that the plain renderer reads better.
MIN_WIDTH = 60

# Fraction of characters the font cannot draw before we give up on the line.
# A stray symbol is fine; a Cyrillic or CJK lyric is not something to fake.
UNKNOWN_RATIO = 0.2

# Blank cells between rendered words.
GAP = 2

# figlet fonts cover printable ASCII only.
_RENDERABLE = re.compile(r"[\x20-\x7e]")

# Typographic characters that have a plain ASCII equivalent the font can draw.
_TRANSLATE = {
    ord("’"): "'", ord("‘"): "'",      # ’ ‘
    ord("“"): '"', ord("”"): '"',      # “ ”
    ord("–"): "-", ord("—"): "-",      # – —
    ord("…"): "...",                        # …
    ord(" "): " ",                          # nbsp
}


@dataclass(frozen=True)
class Span:
    """The column range one source word occupies in every rendered row."""

    word: int      # index into text.split(); -1 for the gap between words
    start: int     # inclusive
    end: int       # exclusive


@dataclass(frozen=True)
class BigLine:
    """One rendered row-group: all rows are the same length."""

    rows: tuple[str, ...]
    spans: tuple[Span, ...]
    width: int
    text: str


def fold(text: str) -> str:
    """Reduce text to characters a figlet font can draw.

    Strips accents (``café`` -> ``cafe``) and normalises curly quotes and
    dashes, so ordinary lyrics render instead of being refused for characters
    that have a perfectly good ASCII equivalent.
    """
    text = text.translate(_TRANSLATE)
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _renderable_ratio(text: str) -> float:
    """Fraction of non-space characters the font can actually draw."""
    chars = [c for c in fold(text) if not c.isspace()]
    if not chars:
        return 0.0
    return sum(1 for c in chars if _RENDERABLE.match(c)) / len(chars)


def words_of(text: str) -> list[str]:
    """Fold, drop characters the font cannot draw, and split into words.

    Dropping rather than refusing is safe because ``render`` has already gated
    on :func:`_renderable_ratio`: by the time we get here the line is known to
    be overwhelmingly renderable, so a stray "♥" should cost that symbol, not
    the whole line. pyfiglet renders such a character as nothing at all, which
    would otherwise abort the render.
    """
    kept = "".join(c for c in fold(text)
                   if c.isspace() or _RENDERABLE.match(c))
    return kept.split()


@lru_cache(maxsize=512)
def _figlet_rows(word: str, font: str = FONT) -> tuple[str, ...]:
    """Rows for one word, padded to equal length. Cached: fonts are slow.

    ``font`` exists so a second line can be set smaller than the first: the
    title carries the panel and the artist sits under it, and at the same size
    they compete rather than reading as a heading and its byline.
    """
    from pyfiglet import Figlet

    rendered = Figlet(font=font).renderText(word)
    rows = rendered.split("\n")
    while rows and not rows[-1]:
        rows.pop()                       # trailing newline artefact
    if not rows:
        return ()
    pad = max(len(r) for r in rows)
    return tuple(r.ljust(pad) for r in rows)


def _row_range(blocks: list[tuple[str, ...]]) -> Optional[tuple[int, int]]:
    """Rows that are non-blank in at least one block, as a [lo, hi) range.

    Computed over EVERY block that will be shown together — all words, and all
    fragments of a wrapped line. Trimming per word would put letters at
    different heights; trimming per fragment would give the wrapped rows of one
    sentence different heights from each other.
    """
    if not blocks:
        return None
    height = max(len(b) for b in blocks)
    padded = [b + ("",) * (height - len(b)) for b in blocks]
    keep = [i for i in range(height) if any(b[i].strip() for b in padded)]
    if not keep:
        return None
    return keep[0], keep[-1] + 1


def _assemble(words: list[str], rng: tuple[int, int]) -> Optional[BigLine]:
    """Join per-word blocks side by side over the shared row range."""
    lo, hi = rng
    blocks = []
    for w in words:
        block = _figlet_rows(w)
        if not block:
            return None
        # _figlet_rows already pads every row to the word's width; pad the row
        # COUNT so the requested range exists, then slice it.
        block_w = len(block[0])
        block = block + (" " * block_w,) * max(0, hi - len(block))
        blocks.append(tuple(r.ljust(block_w) for r in block[lo:hi]))
    if not blocks:
        return None

    height = hi - lo
    rows = [""] * height
    spans: list[Span] = []
    col = 0
    for i, block in enumerate(blocks):
        if i:
            for r in range(height):
                rows[r] += " " * GAP
            spans.append(Span(-1, col, col + GAP))
            col += GAP
        block_w = len(block[0])
        for r in range(height):
            rows[r] += block[r]
        spans.append(Span(i, col, col + block_w))
        col += block_w

    return BigLine(tuple(rows), tuple(spans), col, " ".join(words))


def render_line(text: str, font: str = FONT) -> Optional[BigLine]:
    """Render one line unconditionally, ignoring width. None if unrenderable."""
    words = words_of(text)
    if not words:
        return None
    blocks = [_figlet_rows(w, font) for w in words]
    if any(not b for b in blocks):
        return None
    rng = _row_range(blocks)
    return _assemble(words, rng) if rng else None


def wrap(text: str, width: int) -> Optional[list[str]]:
    """Greedy word wrap measured in RENDERED cells, not characters.

    Returns None when a single word cannot fit — a word is never split
    mid-glyph, which would be unreadable.
    """
    words = words_of(text)
    if not words:
        return None

    widths = []
    for w in words:
        block = _figlet_rows(w)
        if not block:
            return None
        widths.append(len(block[0]))
    if any(w > width for w in widths):
        return None

    lines: list[str] = []
    current: list[str] = []
    used = 0
    for word, w in zip(words, widths):
        extra = w if not current else w + GAP
        if current and used + extra > width:
            lines.append(" ".join(current))
            current, used = [word], w
        else:
            current.append(word)
            used += extra
    if current:
        lines.append(" ".join(current))
    return lines


@lru_cache(maxsize=256)
def render(text: str, width: int, *,
           max_rows: int = 2) -> Optional[tuple[BigLine, ...]]:
    """Render ``text`` big enough to fit ``width``, or None to fall back.

    ``None`` is a normal outcome, not an error: a narrow terminal, a line too
    long to wrap within ``max_rows``, or lyrics in a script the font cannot
    draw all mean "use the plain renderer".
    """
    if width < MIN_WIDTH or not text.strip():
        return None
    if _renderable_ratio(text) < 1.0 - UNKNOWN_RATIO:
        return None

    whole = render_line(text)
    if whole is not None and whole.width <= width:
        return (whole,)

    fragments = wrap(text, width)
    if not fragments or len(fragments) > max_rows:
        return None

    # One row range across EVERY fragment, so the wrapped rows of a sentence
    # share a height and a baseline. Trimming each fragment on its own would
    # give a line with descenders more rows than one without.
    all_words = [w for f in fragments for w in f.split()]
    blocks = [_figlet_rows(w) for w in all_words]
    if any(not b for b in blocks):
        return None
    rng = _row_range(blocks)
    if rng is None:
        return None

    out = []
    word_offset = 0
    for fragment in fragments:
        words = fragment.split()
        line = _assemble(words, rng)
        if line is None or line.width > width:
            return None
        # Word numbering stays continuous across the wrap, so the highlight
        # crosses it correctly.
        out.append(BigLine(
            line.rows,
            tuple(Span(s.word + word_offset if s.word >= 0 else -1,
                       s.start, s.end) for s in line.spans),
            line.width, line.text,
        ))
        word_offset += len(words)
    return tuple(out)


def context_window(rows: int, big_rows: int, *,
                   caption: bool = True) -> tuple[int, int]:
    """How many normal context lines fit above/below the big line.

    Weighted towards "after": the lines a singer needs to see are the ones
    coming up.
    """
    spare = max(0, rows - big_rows - (1 if caption else 0))
    before = spare // 3
    return before, spare - before
