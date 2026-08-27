"""Getting a screen coordinate into the right pixel of a cropped screenshot.

Two monitors is where this goes wrong: the left one starts at a negative x, a
grab of everything starts at its own zero, and a window rectangle is measured
from neither.
"""

from __future__ import annotations

from jarvis.marks import crop_box, place


def test_one_monitor_starting_at_zero_needs_no_shifting():
    assert crop_box((100, 50, 900, 650), (0, 0)) == (100, 50, 900, 650)


def test_a_window_on_the_left_hand_monitor_is_found_in_the_grab():
    """The second screen sits at x=-1920, so the virtual screen starts there and
    the grab's own origin is that corner."""
    assert crop_box((-1920, 0, 0, 1080), (-1920, 0)) == (0, 0, 1920, 1080)


def test_a_negative_vertical_origin_is_handled_too():
    assert crop_box((0, -200, 800, 400), (-1920, -200)) == (1920, 0, 2720, 600)


def test_a_target_lands_relative_to_the_window_not_the_screen():
    assert place(632, 20, (-8, -8, 1928, 1040)) == (640, 28)


def test_the_top_left_of_a_window_is_the_top_left_of_the_crop():
    bounds = (100, 50, 900, 650)
    assert place(100, 50, bounds) == (0, 0)
