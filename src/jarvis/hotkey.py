"""Global hotkey listener for pausing transcription.

Registers the Pause/Break key as a toggle. When pressed, transcription stops;
pressed again, it resumes. Uses the ``keyboard`` library which must be installed
as an optional dependency (``uv sync --extra hotkey``).
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger("jarvis.hotkey")

# Virtual key code for Pause/Break.
_PAUSE_KEY = "pause"


class HotkeyListener:
    """Listens for the Pause key and calls back into the service."""

    def __init__(self, on_pause: callable[[], None], on_resume: callable[[], None]) -> None:
        self._on_pause = on_pause
        self._on_resume = on_resume
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self) -> None:
        """Begin listening for the Pause key in a background thread."""
        if self._thread is not None:
            return
        try:
            import keyboard
        except ImportError:
            logger.info("keyboard module not installed - hotkey disabled.")
            return

        def _toggle() -> None:
            # keyboard.on_press_key fires on every keydown. We need to detect
            # the press, toggle state, and debounce so a held key doesn't
            # fire repeatedly.
            pass

        self._running = True
        self._thread = threading.Thread(
            target=self._listen,
            name="jarvis-hotkey",
            daemon=True,
        )
        self._thread.start()
        logger.info("Hotkey listener started (Pause/Break to toggle).")

    def _listen(self) -> None:
        try:
            import keyboard

            def _handler(event) -> None:
                if event.event_type == "down" and event.name == _PAUSE_KEY:
                    if self._on_pause():
                        logger.info("Transcription paused via Pause key.")
                    else:
                        self._on_resume()
                        logger.info("Transcription resumed via Pause key.")

            keyboard.add_hotkey(_PAUSE_KEY, _handler)
            keyboard.hook_key(_PAUSE_KEY, _handler)

            # Keep the thread alive.
            while self._running:
                import time
                time.sleep(1)
        except ImportError:
            logger.info("keyboard module not available - hotkey disabled.")
        except Exception:
            logger.exception("Hotkey listener stopped.")

    def stop(self) -> None:
        """Stop listening for the Pause key."""
        self._running = False
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2)
        try:
            import keyboard
            keyboard.unhook_all()
        except ImportError:
            pass
