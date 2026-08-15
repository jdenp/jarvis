from __future__ import annotations

import pytest

from jarvis.config import WakeConfig
from jarvis.wake import WakeMatcher, split_wake_word, wake_pattern


def split(heard: str, words=("jarvis", "hey jarvis")):
    return split_wake_word(wake_pattern(words), heard)


def matcher() -> WakeMatcher:
    config = WakeConfig()
    return WakeMatcher(config.words, config.fuzzy, config.fuzzy_threshold)


# ------------------------------------------------------------------ exact


def test_wake_word_is_stripped():
    assert split("Jarvis, open the config file") == (True, "open the config file")


def test_wake_word_in_the_middle_is_removed():
    addressed, remainder = split("okay jarvis open the config file")
    assert addressed is True
    assert "jarvis" not in remainder.lower()
    assert remainder.startswith("okay") and remainder.endswith("open the config file")


def test_bare_wake_word_leaves_an_empty_remainder():
    assert split("jarvis") == (True, "")
    assert split("Jarvis!") == (True, "")


def test_unaddressed_speech_is_reported_as_such():
    assert split("just muttering to myself") == (False, "just muttering to myself")


def test_matching_is_case_insensitive_and_word_bounded():
    pattern = wake_pattern(("jarvis", "hey jarvis"))
    assert pattern.search("JARVIS")
    assert pattern.search("Hey Jarvis, hello")
    assert not pattern.search("jarvisian")
    assert not pattern.search("banjarvis")


def test_the_longest_wake_word_wins():
    assert split("hey jarvis what is the time") == (True, "what is the time")


def test_no_wake_words_matches_nothing():
    assert wake_pattern(()).search("jarvis") is None
    assert wake_pattern(("", "  ")).search("jarvis") is None


def test_surrounding_punctuation_is_trimmed():
    assert split("Jarvis... open the config file!")[1] == "open the config file"


# ------------------------------------------------------------------ fuzzy


@pytest.mark.parametrize(
    ("heard", "expected"),
    [
        ("Hey, Jovis", "Hey"),
        ("Jovis", ""),
        ("Darvus, you missed my message", "you missed my message"),
        ("Java's South Korea time.", "South Korea time"),
        ("Jarvus, run the tests", "run the tests"),
        ("Jervis open the file", "open the file"),
    ],
)
def test_mishearings_of_the_name_still_count_as_being_addressed(heard, expected):
    """All of these came off a real transcript. An exact match ignores the user
    and gives them no clue why."""
    addressed, remainder = matcher().split(heard)
    assert addressed is True
    assert remainder == expected


@pytest.mark.parametrize(
    "heard",
    [
        "harvest the data",
        "jars in the cupboard",
        "javascript file",
        "service is down",
        "drive us home",
        "the car is red",
        "give us a minute",
        "trav is here",
    ],
)
def test_ordinary_words_are_not_mistaken_for_the_name(heard):
    """Being loose costs nothing until it starts acting on things you did not
    say to it."""
    assert matcher().split(heard)[0] is False


def test_fuzzy_can_be_turned_off():
    config = WakeConfig()
    strict = WakeMatcher(config.words, fuzzy=False)
    # Not in the literal variant list, so only the fuzzy pass would catch it.
    assert strict.split("Java's, open it")[0] is False
    assert matcher().split("Java's, open it")[0] is True
    assert strict.split("Jarvis, open it")[0] is True


def test_exact_matches_are_preferred_over_approximate_ones():
    addressed, remainder = matcher().split("jarvis check the jars")
    assert (addressed, remainder) == (True, "check the jars")
