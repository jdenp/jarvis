"""Global hotkey listener for pausing transcription.

Registers a single key as a toggle. When pressed, transcription stops;
pressed again, it resumes. Uses the ``keyboard`` library which must be installed
as an optional dependency (``uv sync --extra hotkey``).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

logger = logging.getLogger("jarvis.hotkey")

# Ignore key auto-repeat while the key is held down.
_DEBOUNCE_SECONDS = 0.3


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


class HotkeyListener:
    """Listens for one key and calls back into the service."""

    def __init__(
        self, on_pause: Callable[[], bool], on_resume: Callable[[], None], key: str = "num lock"
    ) -> None:
        self._key = key
        self._on_pause = on_pause
        self._on_resume = on_resume
        self._unhook: Callable | None = None
        self._last_fired = 0.0

    def start(self) -> None:
        """Begin listening for the configured toggle key."""
        if self._unhook is not None or not self._key:
            return
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
            now = time.monotonic()
            if now - self._last_fired < _DEBOUNCE_SECONDS:
                return
            self._last_fired = now
            if self._on_pause():
                logger.info("Transcription paused via the %s key.", self._key)
            else:
                self._on_resume()
                logger.info("Transcription resumed via the %s key.", self._key)

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
        """Stop listening for the toggle key."""
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
