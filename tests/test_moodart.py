"""Scoring images against a feeling, and computing one when none fits.

Colour arithmetic is where this goes wrong quietly: hue is circular, and grey
has no hue at all. Both are pinned here, because a wrong average produces a
plausible-looking image that is simply the wrong colour.
"""
import pytest

from karaoke import moodart


def _grid(colour, cols=4, rows=3):
    return [[colour] * cols for _ in range(rows)]


def _checker(a, b, cols=4, rows=4):
    return [[a if (x + y) % 2 == 0 else b for x in range(cols)] for y in range(rows)]


# -- hue is circular ----------------------------------------------------

def test_hue_distance_wraps_around_the_wheel():
    """0.99 and 0.01 are both red, two hundredths apart -- not almost a turn."""
    assert moodart.hue_distance(0.99, 0.01) == pytest.approx(0.02)


def test_hue_distance_is_symmetric():
    assert moodart.hue_distance(0.1, 0.7) == moodart.hue_distance(0.7, 0.1)


def test_hue_distance_maxes_at_a_half_turn():
    assert moodart.hue_distance(0.0, 0.5) == pytest.approx(0.5)


def test_mean_hue_wraps_too():
    """Averaging a red at 0.02 and a red at 0.98 arithmetically gives cyan."""
    import colorsys

    def rgb(h):
        r, g, b = colorsys.hsv_to_rgb(h, 1.0, 1.0)
        return (int(r * 255), int(g * 255), int(b * 255))

    stats = moodart.colour_stats([[rgb(0.02), rgb(0.98)]])
    assert moodart.hue_distance(stats.hue, 0.0) < 0.05    # red, not 0.5


def test_grey_pixels_do_not_vote_on_hue():
    """An unsaturated pixel's hue is arbitrary and must not drown out real colour."""
    red = (255, 0, 0)
    greys = [(128, 128, 128)] * 20
    stats = moodart.colour_stats([[red, *greys]])
    assert moodart.hue_distance(stats.hue, 0.0) < 0.05
    assert stats.saturation < 0.2       # still reported as a washed-out image


def test_stats_of_an_empty_image_are_zero():
    assert moodart.colour_stats([]).value == 0.0


# -- scoring ------------------------------------------------------------

def test_a_warm_bright_image_suits_happy_over_sad():
    warm = _grid((255, 190, 60))
    assert moodart.mood_score(warm, "happy") > moodart.mood_score(warm, "sad")


def test_a_dim_blue_image_suits_sad_over_happy():
    blue = _grid((40, 60, 110))
    assert moodart.mood_score(blue, "sad") > moodart.mood_score(blue, "happy")


def test_contrast_separates_images_of_the_same_average_colour():
    """Angry wants a blazing cover, not a flat maroon one of the same mean."""
    flat = _grid((128, 20, 20))
    punchy = _checker((255, 40, 40), (10, 0, 0))
    assert moodart.mood_score(punchy, "angry") > moodart.mood_score(flat, "angry")


def test_tender_prefers_the_softer_of_two_pinks():
    soft = _grid((240, 190, 205))
    harsh = _checker((255, 0, 120), (20, 0, 10))
    assert moodart.mood_score(soft, "tender") > moodart.mood_score(harsh, "tender")


def test_scores_stay_in_range():
    for mood in moodart.MOOD_TARGETS:
        for colour in ((0, 0, 0), (255, 255, 255), (255, 0, 0), (0, 128, 200)):
            assert 0.0 <= moodart.mood_score(_grid(colour), mood) <= 1.0


def test_an_unknown_mood_falls_back_to_neutral():
    grid = _grid((100, 100, 120))
    assert moodart.mood_score(grid, "ecstatic") == moodart.mood_score(grid, "neutral")


# -- generation ---------------------------------------------------------

class _Analysis:
    def __init__(self, key=None, bpm=120.0, energy=0.4, brightness=0.3):
        self.detected_key = key
        self.resolved_key = None
        self.bpm = bpm
        self.energy = energy
        self.brightness = brightness


class _Key:
    def __init__(self, name, pitch_class):
        self.name = name
        self.pitch_class = pitch_class


def test_generate_fills_the_requested_grid():
    px = moodart.generate(_Analysis(), "happy", 6, 4)
    assert len(px) == 4 and all(len(row) == 6 for row in px)
    assert all(0 <= c <= 255 for row in px for cell in row for c in cell)


def test_generate_is_deterministic():
    """The panel must not shimmer between refreshes of the same track."""
    a = _Analysis(key=_Key("D minor", 2))
    assert moodart.generate(a, "sad", 5, 3) == moodart.generate(a, "sad", 5, 3)


def test_different_keys_give_different_images():
    one = moodart.generate(_Analysis(key=_Key("C major", 0)), "happy", 5, 3)
    two = moodart.generate(_Analysis(key=_Key("F# major", 6)), "happy", 5, 3)
    assert one != two


def test_minor_reads_darker_than_major():
    """Major and minor should be distinguishable at a glance."""
    major = moodart.generate(_Analysis(key=_Key("C major", 0)), "neutral", 6, 4)
    minor = moodart.generate(_Analysis(key=_Key("C minor", 0)), "neutral", 6, 4)
    brightness = lambda px: sum(sum(c) for row in px for c in row)
    assert brightness(minor) < brightness(major)


def test_generate_without_an_analysis_still_works():
    """A track with no key or BPM must still get a picture, not an exception."""
    px = moodart.generate(None, "tender", 4, 3)
    assert len(px) == 3 and len(px[0]) == 4


def test_generate_handles_a_key_with_no_pitch_class():
    """An unfamiliar key representation yields a stable colour, not a crash."""
    class Odd:
        name = "Bb dorian"

    px = moodart.generate(_Analysis(key=Odd()), "neutral", 4, 3)
    assert len(px) == 3


def test_generate_copes_with_a_zero_sized_panel():
    assert moodart.generate(_Analysis(), "happy", 0, 0) == []
