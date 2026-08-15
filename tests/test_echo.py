"""JARVIS must not hear itself. Timing is the first defence (see
test_microphone); this is the text comparison that catches what slips past."""

from __future__ import annotations

from jarvis.echo import EchoGuard, normalise, sounds_like


def test_normalise_strips_punctuation_and_case():
    assert normalise("Ready when you are, sir.") == "ready when you are sir"
    assert normalise("  !!  ") == ""


def test_exact_repeat_is_recognised():
    guard = EchoGuard()
    guard.remember("Ready when you are, sir.")
    assert guard.is_echo("ready when you are sir") is True


def test_a_clipped_start_is_recognised():
    """Hearing your own speakers usually loses the first word or two."""
    guard = EchoGuard()
    guard.remember("Systems ready. Ready when you are, sir.")
    assert guard.is_echo("ready when you are sir") is True


def test_a_garbled_transcript_is_recognised():
    guard = EchoGuard()
    guard.remember("Disk is at sixty-seven percent and holding.")
    assert guard.is_echo("disc is at sixty seven percent and holdings") is True


def test_a_genuine_request_gets_through():
    guard = EchoGuard()
    guard.remember("Ready when you are, sir.")
    assert guard.is_echo("open the config file") is False


def test_nothing_is_an_echo_before_anything_is_spoken():
    assert EchoGuard().is_echo("open the config file") is False


def test_empty_input_is_not_an_echo():
    guard = EchoGuard()
    guard.remember("something")
    assert guard.is_echo("   ") is False


def test_old_lines_are_forgotten(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("jarvis.echo.time.monotonic", lambda: clock[0])
    guard = EchoGuard(memory_seconds=20)
    guard.remember("Memory is at seventy percent.")
    assert guard.is_echo("memory is at seventy percent") is True
    clock[0] += 60
    # Long enough ago that repeating it is the user talking, not an echo.
    assert guard.is_echo("memory is at seventy percent") is False


def test_only_the_last_few_lines_are_kept():
    guard = EchoGuard(keep=2)
    for line in ("opening the configuration", "battery at forty percent", "disk nearly full"):
        guard.remember(line)
    assert guard.is_echo("opening the configuration") is False, "should have fallen out"
    assert guard.is_echo("disk nearly full") is True


def test_sounds_like_ignores_empty_sides():
    assert sounds_like("", "something") is False
    assert sounds_like("something", "") is False


def test_unrelated_text_of_similar_length_is_not_a_match():
    assert sounds_like("open the config file", "close the browser tab") is False
