"""One key that shuts the microphone, from anywhere. Two, if it is held.

A lock key is watched rather than hooked. Windows keeps the on-off state of num
lock, caps lock and scroll lock itself, and any thread can read it, so a press is
seen as a change in that state - no hook, no message pump, and nothing that can
be denied. Everything else falls back to the ``keyboard`` library's low level
hook (``uv sync --extra hotkey``).

The distinction is not academic. A low level hook lives in this process, and
Windows does not deliver input to an unelevated process while an elevated window
has the foreground: Task Manager, an admin terminal, regedit. Presses there were
silently dropped, and one dropped press inverted the key for the rest of the
session - the lamp said one thing and JARVIS believed the other.

The hold is a second read of the same key. A lamp flips on the way down and says
nothing about the way up, so how long the key was held has to be asked for
separately - `GetAsyncKeyState`, which is not queue based either and so survives
the same elevated window. Only lock keys get it: a hooked key fires on the press
and there is nothing left to decide by the time it is released.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from collections.abc import Callable

logger = logging.getLogger("jarvis.hotkey")

# Ignore key auto-repeat while the key is held down.
_DEBOUNCE_SECONDS = 0.3

# A press still down after this is the other action. Deliberately long: at 0.6 an
# ordinary firm press reached it, and shutting the microphone is the job people
# come to this key for.
_HOLD_SECONDS = 1.5

# How often the key is asked whether it is still down, within a hold.
_HOLD_POLL = 0.03

# Keys Windows keeps a state for, and the virtual key code to read it with.
LOCK_KEYS = {"num lock": 0x90, "caps lock": 0x14, "scroll lock": 0x91}

# How often that state is read. A key press is a syscall's worth of work, so
# this is cheap enough to make immediate: eight reads a second, each one an
# integer out of the keyboard state Windows already maintains.
_POLL_SECONDS = 0.12


_user32 = None


def _lock_state(code: int) -> int:
    """Whether that lock key's lamp is on, straight out of Windows.

    Queue independent, unlike most of the input API: any thread can ask and
    there is nothing to pump. That is the whole reason this beats a hook.
    """
    global _user32
    if _user32 is None:
        _user32 = ctypes.WinDLL("user32", use_last_error=True)
    return _user32.GetKeyState(code) & 1


def _key_down(code: int) -> bool:
    """Whether the key is being held at this instant.

    The lamp says a press happened. This says it has not ended yet, which is the
    only difference between the two things one key now does.
    """
    global _user32
    if _user32 is None:
        _user32 = ctypes.WinDLL("user32", use_last_error=True)
    return bool(_user32.GetAsyncKeyState(code) & 0x8000)


def _canonical(name: str | None) -> str:
    """The library's own spelling of a key name, or the name as given."""
    if not name:
        return ""
    # Stripped first: the library's own normalise leaves surrounding spaces
    # alone, and a stray one in a config file should not silently break the key.
    tidied = name.strip().lower()
    try:
        from keyboard._canonical_names import normalize_name

        return normalize_name(tidied)
    except Exception:
        return tidied


def lock_code(key: str | None) -> int | None:
    """The code to read that key's lamp with, or None if it has not got one."""
    return LOCK_KEYS.get(_canonical(key).replace("_", " "))


class HotkeyListener:
    """Listens for one key and calls back into the service."""

    def __init__(
        self,
        on_pause: Callable[[], bool],
        on_resume: Callable[[], None],
        key: str = "num lock",
        on_hold: Callable[[], None] | None = None,
    ) -> None:
        self._key = key
        self._on_pause = on_pause
        self._on_resume = on_resume
        self._on_hold = on_hold
        self._unhook: Callable | None = None
        self._last_fired = 0.0
        self._stop = threading.Event()
        self._watcher: threading.Thread | None = None
        self._lamp = 0

    def start(self) -> None:
        """Begin watching for the configured toggle key."""
        if self._unhook is not None or self._watcher is not None or not self._key:
            return
        if (code := lock_code(self._key)) is not None:
            self._watch(code)
            return
        self._hook()

    def _toggle(self) -> None:
        """What a press does, however it was noticed."""
        now = time.monotonic()
        if now - self._last_fired < _DEBOUNCE_SECONDS:
            return
        self._last_fired = now
        if self._on_pause():
            logger.info("Transcription paused via the %s key.", self._key)
        else:
            self._on_resume()
            logger.info("Transcription resumed via the %s key.", self._key)

    def _watch(self, code: int) -> None:
        """Poll the key's own on-off state, which is nobody's to withhold."""
        try:
            self._lamp = _lock_state(code)
        except (OSError, AttributeError):
            logger.warning("Could not read the %s key - falling back to a hook.", self._key)
            self._hook()
            return

        def run() -> None:
            while not self._stop.wait(_POLL_SECONDS):
                if not self._look(code):
                    return

        self._watcher = threading.Thread(target=run, name="hotkey", daemon=True)
        self._watcher.start()
        logger.info("Watching the %s key to toggle (works whatever has focus).", self._key)

    def _look(self, code: int) -> bool:
        """One read of the lamp. False when there is no point reading again.

        Level rather than edge: a press is a lamp that differs from the one
        before it. Two presses while the screen is locked are no change and
        rightly do nothing, and a press missed for a moment is still seen the
        next time this looks - which is the drift that used to invert the key
        for a whole session.
        """
        try:
            now = _lock_state(code)
        except OSError:
            logger.exception("Could not read the %s key; stopping.", self._key)
            return False
        if now != self._lamp:
            self._lamp = now
            self._pressed(code)
        return True

    def _pressed(self, code: int) -> None:
        """A press on a watched key, which is not known to be a tap yet.

        So the tap waits for the key to come up before it is called a tap. That
        costs nothing on a real one, which is over in a few tens of milliseconds,
        and it is the only way one key does two things without doing both.
        """
        if self._on_hold is not None and self._held(code):
            self._long()
        else:
            self._toggle()

    def _held(self, code: int) -> bool:
        """Whether the key is still down _HOLD_SECONDS after the lamp changed.

        A read that fails is a tap. The worst that can do is leave the key doing
        the one job it did before there was a second one.
        """
        end = time.monotonic() + _HOLD_SECONDS
        try:
            while time.monotonic() < end:
                if not _key_down(code):
                    return False
                time.sleep(_HOLD_POLL)
        except (OSError, AttributeError):
            return False
        return True

    def _long(self) -> None:
        """What holding the key does, as opposed to pressing it."""
        assert self._on_hold is not None
        self._last_fired = time.monotonic()
        logger.info("The %s key was held.", self._key)
        self._on_hold()

    def _hook(self) -> None:
        """The old way, for a key with no state of its own to read."""
        try:
            import keyboard
        except ImportError:
            logger.warning(
                "keyboard module not installed - hotkey disabled. "
                "Install it with 'uv sync --extra hotkey'."
            )
            return

        # hook_key accepts "numlock", "num_lock" and "num lock" alike, but the
        # events it delivers are named canonically - so comparing against the
        # configured spelling registers a hotkey that then never fires. Normalise
        # once here and any accepted spelling works.
        wanted = _canonical(self._key)

        def _handler(event) -> None:
            # Some keys share a scan code, so match on the key name
            if event.event_type != "down" or _canonical(event.name) != wanted:
                return
            self._toggle()

        try:
            self._unhook = keyboard.hook_key(self._key, _handler)
        except ValueError:
            logger.warning("Unknown hotkey %r - hotkey disabled.", self._key)
            return
        except Exception:
            logger.exception("Could not register the %r hotkey.", self._key)
            return
        logger.info("Hotkey listener started (%s to toggle).", self._key)

    def stop(self) -> None:
        """Stop watching for the toggle key."""
        self._stop.set()
        if watcher := self._watcher:
            self._watcher = None
            watcher.join(timeout=_POLL_SECONDS * 4)
        unhook, self._unhook, key = self._unhook, None, self._key
        if unhook is None:
            return
        try:
            import keyboard

            keyboard.unhook(unhook)
        except ImportError:
            pass
        except Exception:
            logger.exception("Could not unhook the %r hotkey.", key)
