"""The global key that stops and starts listening.

The library accepts several spellings of a key name and delivers events under
one of them, so the spelling in the config and the spelling on the event are not
the same string. That mismatch registers a hotkey that never fires - no error,
no log line, just a key that does nothing.
"""

from __future__ import annotations

import pytest

from jarvis.config import Config
from jarvis.hotkey import HotkeyListener, _canonical


class Event:
    def __init__(self, name: str, event_type: str = "down") -> None:
        self.name = name
        self.event_type = event_type


def listener(key: str) -> tuple[HotkeyListener, list[str]]:
    calls: list[str] = []
    return (
        HotkeyListener(
            on_pause=lambda: (calls.append("pause"), True)[1],
            on_resume=lambda: calls.append("resume"),
            key=key,
        ),
        calls,
    )


def handler_for(key: str):
    """The hook callback, without registering anything with the real keyboard."""
    hotkey, calls = listener(key)
    captured = {}

    class FakeKeyboard:
        def hook_key(self, _key, callback):
            captured["handler"] = callback
            return object()

    import sys

    fake = FakeKeyboard()
    sys.modules["keyboard"] = fake  # type: ignore[assignment]
    try:
        hotkey.start()
    finally:
        del sys.modules["keyboard"]
    return captured.get("handler"), calls


@pytest.mark.parametrize("spelling", ["num lock", "numlock", "num_lock", "NUM LOCK"])
def test_any_accepted_spelling_of_the_key_still_fires(spelling):
    """hook_key takes all of these; the events it delivers are named "num lock".
    Comparing against the configured spelling would register and never fire."""
    handler, calls = handler_for(spelling)
    assert handler is not None
    handler(Event("num lock"))
    assert calls == ["pause"]


def test_a_different_key_is_ignored():
    handler, calls = handler_for("num lock")
    handler(Event("end"))
    assert calls == []


def test_key_up_is_ignored():
    """Otherwise one press pauses and immediately resumes."""
    handler, calls = handler_for("num lock")
    handler(Event("num lock", event_type="up"))
    assert calls == []


def test_auto_repeat_while_held_only_fires_once():
    handler, calls = handler_for("num lock")
    for _ in range(5):
        handler(Event("num lock"))
    assert calls == ["pause"], "the debounce swallowed the repeats"


def test_an_empty_key_registers_nothing():
    """Setting it to "" is how the feature is turned off."""
    handler, _calls = handler_for("")
    assert handler is None


def test_the_default_is_num_lock():
    assert Config().service.hotkey == "num lock"


def test_canonical_survives_the_library_being_absent():
    assert _canonical("  Num Lock ") == "num lock"
    assert _canonical(None) == ""
