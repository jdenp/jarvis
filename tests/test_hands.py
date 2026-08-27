"""Shortcut parsing and unicode typing.

Only the decisions are tested. Everything past `keys_for` is a SendInput call
that either reaches the desktop or does not, and neither outcome can be asserted
on from here.
"""

from __future__ import annotations

import pytest

from jarvis.hands import EXTENDED, KEYS, _utf16_units, keys_for


def test_a_shortcut_comes_back_in_the_order_it_is_pressed():
    assert keys_for("ctrl+shift+s") == (KEYS["ctrl"], KEYS["shift"], KEYS["s"])


def test_case_and_spacing_do_not_matter():
    assert keys_for(" Ctrl + S ") == keys_for("ctrl+s")


def test_control_and_ctrl_are_the_same_key():
    assert keys_for("control+c") == keys_for("ctrl+c")


def test_an_unknown_key_names_the_part_it_did_not_recognise():
    """A shortcut silently half pressed is worse than one that refused."""
    with pytest.raises(ValueError, match="'nope'"):
        keys_for("ctrl+nope")


def test_nothing_at_all_is_refused():
    with pytest.raises(ValueError, match="No key"):
        keys_for("  ")


@pytest.mark.parametrize("name", ["up", "down", "left", "right", "home", "end", "delete"])
def test_the_extended_keys_are_flagged_as_such(name):
    """Without the extended flag some applications read these as their numeric
    keypad twins, so left arrow becomes a 4."""
    assert KEYS[name] in EXTENDED


def test_the_function_keys_are_where_they_should_be():
    assert (keys_for("f1"), keys_for("f12")) == ((0x70,), (0x7B,))


def test_a_plain_character_is_one_utf16_unit():
    assert _utf16_units("a") == (97,)


def test_a_character_outside_the_basic_plane_is_two():
    """One event per code unit, or an emoji arrives as a pair of question marks."""
    assert len(_utf16_units(chr(0x1F600))) == 2


@pytest.mark.parametrize(
    ("name", "code"),
    [("playpause", 0xB3), ("play", 0xB3), ("nexttrack", 0xB0), ("volumeup", 0xAF), ("mute", 0xAD)],
)
def test_the_media_keys_are_there(name, code):
    """"Play my music" needs no window, no scan and no target - Windows routes a
    media key to whatever it considers the current media session."""
    assert keys_for(name) == (code,)
    assert code in EXTENDED, "sent the way a real keyboard sends them"
