"""The key that shuts the microphone, from anywhere. Two, if it is held.

Two mechanisms with one meaning. A lock key is watched - Windows keeps its lamp
state and any thread can read it - because a low level hook is not delivered
while an elevated window has the foreground, and one press swallowed by Task
Manager inverted the key for the rest of the session. Everything else still
hooks, where the trap is spelling: the library accepts several names for a key
and delivers events under one of them, so comparing against the configured
spelling registers a hotkey that never fires.
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
    """One with a stateful service behind it, because pause() returning False
    when it was already paused is what makes the key a toggle."""
    calls: list[str] = []
    paused = [False]

    def pause() -> bool:
        if paused[0]:
            return False
        paused[0] = True
        calls.append("pause")
        return True

    def resume() -> None:
        paused[0] = False
        calls.append("resume")

    return HotkeyListener(on_pause=pause, on_resume=resume, key=key), calls


def handler_for(key: str = "f13"):
    """The hook callback, without registering anything with the real keyboard.

    A key with no lamp by default, since that is the only kind that hooks now.
    """
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


# ------------------------------------------------------------------ the lamp


@pytest.mark.parametrize("spelling", ["num lock", "numlock", "num_lock", "NUM LOCK", " Num Lock "])
def test_a_lock_key_is_watched_however_it_is_spelled(spelling, monkeypatch):
    """Falling through to the hook here is the bug this replaced: it works until
    somebody opens Task Manager, and then it silently does not."""
    monkeypatch.setattr("jarvis.hotkey._lock_state", lambda code: 0)
    hotkey, _calls = listener(spelling)
    hotkey.start()
    try:
        assert hotkey._watcher is not None, "watched, not hooked"
        assert hotkey._unhook is None
    finally:
        hotkey.stop()


def test_the_lamp_changing_is_a_press(monkeypatch):
    lamp = [0]
    monkeypatch.setattr("jarvis.hotkey._lock_state", lambda code: lamp[0])
    hotkey, calls = listener("num lock")
    hotkey._lamp = 0

    lamp[0] = 1
    assert hotkey._look(0x90) is True
    assert calls == ["pause"]

    hotkey._last_fired = 0.0  # past the debounce
    lamp[0] = 0
    hotkey._look(0x90)
    assert calls == ["pause", "resume"], "on and off again are two presses, not one"


def test_the_lamp_holding_still_is_nothing(monkeypatch):
    """Eight reads a second, and all but a couple of them see no change."""
    monkeypatch.setattr("jarvis.hotkey._lock_state", lambda code: 1)
    hotkey, calls = listener("num lock")
    hotkey._lamp = 1
    for _ in range(10):
        hotkey._look(0x90)
    assert calls == []


def test_a_press_missed_for_a_moment_is_still_seen(monkeypatch):
    """The old hook dropped presses while an elevated window was in front and
    never caught up, so the lamp and JARVIS disagreed from then on. A level read
    cannot drift: whenever it next looks, the lamp is the truth."""
    lamp = [0]
    monkeypatch.setattr("jarvis.hotkey._lock_state", lambda code: lamp[0])
    hotkey, calls = listener("num lock")
    hotkey._lamp = 0
    lamp[0] = 1  # pressed while nothing was looking
    hotkey._look(0x90)
    assert calls == ["pause"]


def test_two_presses_nobody_saw_are_rightly_nothing(monkeypatch):
    """Pressed twice behind a locked screen is back where it started."""
    monkeypatch.setattr("jarvis.hotkey._lock_state", lambda code: 0)
    hotkey, calls = listener("num lock")
    hotkey._lamp = 0
    hotkey._look(0x90)
    assert calls == []


def test_a_read_that_fails_stops_the_watch(monkeypatch):
    def broken(code):
        raise OSError("no user32")

    monkeypatch.setattr("jarvis.hotkey._lock_state", broken)
    hotkey, calls = listener("num lock")
    assert hotkey._look(0x90) is False
    assert calls == []


def test_a_machine_that_cannot_read_the_lamp_falls_back_to_the_hook(monkeypatch):
    def broken(code):
        raise OSError("no user32")

    monkeypatch.setattr("jarvis.hotkey._lock_state", broken)
    hotkey, _calls = listener("num lock")
    captured = {}

    class FakeKeyboard:
        def hook_key(self, _key, callback):
            captured["handler"] = callback
            return object()

    import sys

    sys.modules["keyboard"] = FakeKeyboard()  # type: ignore[assignment]
    try:
        hotkey.start()
    finally:
        del sys.modules["keyboard"]
    assert captured.get("handler") is not None
    assert hotkey._watcher is None


def test_stopping_ends_the_watch(monkeypatch):
    monkeypatch.setattr("jarvis.hotkey._lock_state", lambda code: 0)
    hotkey, _calls = listener("num lock")
    hotkey.start()
    watcher = hotkey._watcher
    assert watcher is not None and watcher.is_alive()
    hotkey.stop()
    assert not watcher.is_alive()


def test_it_can_be_read_on_this_machine():
    """Not a mock. The whole approach rests on a queue independent read working
    off a thread with no message pump, so it is worth asking Windows."""
    from jarvis.hotkey import LOCK_KEYS, _lock_state

    assert _lock_state(LOCK_KEYS["num lock"]) in (0, 1)


# ------------------------------------------------------------------- the hold


def with_hold(key: str = "num lock"):
    """A listener whose hold is wired to something that says so."""
    hotkey, calls = listener(key)
    hotkey._on_hold = lambda: calls.append("headphones")
    return hotkey, calls


def test_holding_a_lock_key_is_the_other_action(monkeypatch):
    """The lamp flips on the way down and says nothing about the way up, so how
    long the key was held has to be asked for separately."""
    monkeypatch.setattr("jarvis.hotkey._key_down", lambda code: True)
    monkeypatch.setattr("jarvis.hotkey._HOLD_SECONDS", 0.01)
    hotkey, calls = with_hold()
    hotkey._pressed(0x90)
    assert calls == ["headphones"], "and not a pause as well"


def test_a_tap_is_still_a_tap_where_a_hold_exists(monkeypatch):
    """Which is the whole cost of it: the tap waits for the key to come up
    before it is called a tap, and on a real one that is a few milliseconds."""
    monkeypatch.setattr("jarvis.hotkey._key_down", lambda code: False)
    hotkey, calls = with_hold()
    hotkey._pressed(0x90)
    assert calls == ["pause"]


def test_a_key_that_cannot_be_read_is_a_tap(monkeypatch):
    """Better the one job it did before than a key that does nothing."""

    def broken(code):
        raise OSError("no user32")

    monkeypatch.setattr("jarvis.hotkey._key_down", broken)
    hotkey, calls = with_hold()
    hotkey._pressed(0x90)
    assert calls == ["pause"]


def test_nothing_waits_around_when_no_hold_was_asked_for(monkeypatch):
    reads = []
    monkeypatch.setattr("jarvis.hotkey._key_down", lambda code: reads.append(code))
    hotkey, calls = listener("num lock")
    hotkey._pressed(0x90)
    assert calls == ["pause"] and reads == [], "not even one read"


def test_only_a_lock_key_can_be_held():
    """A hooked key has already fired by the time it comes back up."""
    from jarvis.hotkey import lock_code

    assert lock_code("num lock") == 0x90
    assert lock_code("num_lock") == 0x90
    assert lock_code("f13") is None
    assert lock_code("") is None


def test_whether_it_is_down_can_be_read_on_this_machine():
    """The same argument as the lamp: it has to answer off a thread with no
    message pump, so it is worth asking Windows rather than a mock."""
    from jarvis.hotkey import LOCK_KEYS, _key_down

    assert _key_down(LOCK_KEYS["num lock"]) in (True, False)


# ------------------------------------------------------------------- the hook


def test_any_accepted_spelling_of_the_key_still_fires():
    """hook_key takes several spellings; the events it delivers are named one
    way. Comparing against the configured spelling would register and never
    fire."""
    handler, calls = handler_for("F13")
    assert handler is not None
    handler(Event("f13"))
    assert calls == ["pause"]


def test_a_different_key_is_ignored():
    handler, calls = handler_for()
    handler(Event("end"))
    assert calls == []


def test_key_up_is_ignored():
    """Otherwise one press pauses and immediately resumes."""
    handler, calls = handler_for()
    handler(Event("f13", event_type="up"))
    assert calls == []


def test_auto_repeat_while_held_only_fires_once():
    handler, calls = handler_for()
    for _ in range(5):
        handler(Event("f13"))
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
