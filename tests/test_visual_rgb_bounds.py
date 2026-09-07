"""Regression tests for RGB bounds in mood/cover rendering."""
from __future__ import annotations



def test_animate_mood_pixels_clamps_negative_boost_channels() -> None:
    from karaoke import visuals

    pixels = [[(55, 28, 0), (103, 58, 0), (62, 34, 0)]]
    seen_negative_boost = False
    for step in range(400):
        animated = visuals.animate_mood_pixels(pixels, elapsed=step / 25.0, bpm=120.0)
        channels = [channel for row in animated for cell in row for channel in cell]
        assert all(0 <= channel <= 255 for channel in channels)
        # Before the fix, the blue channel for this source data could become
        # negative (e.g. rgb(62,34,-18)) when boost dipped below zero.
        if any(cell[2] == 0 for row in animated for cell in row):
            seen_negative_boost = True
    assert seen_negative_boost


def test_coverart_to_text_clamps_out_of_range_channels() -> None:
    from karaoke import coverart

    # This used to raise from Rich style parsing: rgb(62,34,-18) is invalid.
    text = coverart.to_text([[(62, 34, -18), (999, 260, 255)]])
    assert text.plain == "  "
