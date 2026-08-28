"""Typing into a voice session.

A line typed into the window JARVIS is running in goes in exactly where speech
does: same transcript, same `you >` on screen, same everything downstream. It is
the answer to a room with somebody else in it, a word Whisper will not get right
however many times you say it, and a request you would rather not say out loud.

Nothing is polled that costs anything. `msvcrt.kbhit()` is a peek at the console
buffer every fiftieth of a second, and only once a key has actually been pressed
does this take the bottom line and start reading. Until then it is invisible -
no prompt, no cursor, nothing to dismiss - because most of the time nobody is
typing and a prompt sitting there unanswered is clutter that means nothing.

The line is read here rather than with `input()` for one reason: `input()` owns
the terminal from the moment it is called, and the live line underneath the
conversation wants the same row. Reading it a character at a time means the
status can be put away first and brought back afterwards.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

logger = logging.getLogger("jarvis.typed")

# How often the console buffer is looked at while nobody is typing. Cheap, and
# nothing perceives a twentieth of a second before the first character appears.
POLL_SECONDS = 0.02

ENTER = ("\r", "\n")
BACKSPACE = ("\b", "\x7f")
CANCEL = "\x1b"
INTERRUPT = "\x03"
# Windows sends a function or arrow key as one of these and then the actual
# code. Both are read and both are dropped: there is nothing here to navigate.
PREFIXES = ("\x00", "\xe0")


class Keyboard:
    """The real console. Separated out so the reading can be tested."""

    def waiting(self) -> bool:
        import msvcrt

        return bool(msvcrt.kbhit())

    def read(self) -> str:
        import msvcrt

        return msvcrt.getwch()


class Typing:
    """Reads one line at a time from the console and hands it over."""

    def __init__(self, ui, on_line: Callable[[str], None], keyboard=None, prompt="you > ") -> None:
        self.ui = ui
        self.on_line = on_line
        self.keyboard = keyboard or Keyboard()
        self.prompt = prompt
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self.run, name="jarvis-typing", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        """Wait for somebody to start typing, read what they type, repeat."""
        while not self._stop.wait(POLL_SECONDS):
            if not self.keyboard.waiting():
                continue
            try:
                line = self.read_line()
            except Exception:
                logger.exception("Reading a typed line failed; carrying on without it.")
                self.ui.release()
                continue
            if line:
                logger.info("Typed: %s", line)
                self.on_line(line)

    def read_line(self) -> str:
        """One line, echoed as it is typed. Empty if it was abandoned.

        Escape throws the line away, which is the way out for somebody who
        pressed a key by accident - and pressing a key by accident is the whole
        reason this waits for one rather than showing a prompt.
        """
        self.ui.hold()
        typed: list[str] = []
        # Counted apart from what was typed, because escape empties the line
        # without unprinting it - and then the wipe below comes up short and
        # leaves half an abandoned sentence on screen.
        shown = 0
        try:
            self.ui.raw(self.prompt)
            while not self._stop.is_set():
                key = self.keyboard.read()
                if key in PREFIXES:
                    self.keyboard.read()
                elif key in ENTER:
                    break
                elif key == CANCEL:
                    typed = []
                    break
                elif key in BACKSPACE:
                    if typed:
                        typed.pop()
                        shown -= 1
                        # Back over the character, paint a space on it, back again.
                        self.ui.raw("\b \b")
                elif key >= " ":
                    typed.append(key)
                    shown += 1
                    self.ui.raw(key)
        finally:
            # The whole line goes, whether it was sent or abandoned. What was
            # sent is redrawn properly a moment later as `you > ...`, and what
            # was abandoned should leave nothing behind at all.
            self.ui.raw("\r" + " " * (len(self.prompt) + shown + 1) + "\r")
            self.ui.release()
        return "".join(typed).strip()
