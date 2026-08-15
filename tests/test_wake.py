from __future__ import annotations

from jarvis.wake import split_wake_word, wake_pattern


def split(heard: str, words=("jarvis", "hey jarvis")):
    return split_wake_word(wake_pattern(words), heard)


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
