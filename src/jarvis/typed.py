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

from .ui import paint

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

    def __init__(
        self,
        ui,
        on_line: Callable[[str], None],
        keyboard=None,
        prompt="you > ",
        on_cancel: Callable[[], bool] | None = None,
    ) -> None:
        self.ui = ui
        self.on_line = on_line
        self.keyboard = keyboard or Keyboard()
        self.prompt = prompt
        # Answers whether there was anything to stop, which decides whether the
        # prompt comes back. Nothing happening means escape did nothing.
        self.on_cancel = on_cancel or (lambda: False)
        # Set when escape was pressed on an empty line.
        self.cancelled = False
        # What is on the row right now. Held here rather than in read_line
        # because something permanent can land on top of it at any moment, and
        # the terminal then asks for it back.
        self._typed: list[str] = []
        self._shown = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self.run, name="jarvis-typing", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        """Wait for somebody to start typing, read what they type, repeat.

        Nothing appears until a key that would put a character on the line. Num
        lock is why: it pauses transcription, the console sees the press as well
        as the hotkey listener does, and an empty `you >` was left sitting under
        the status line every time anybody used it.
        """
        reopen = False
        while not self._stop.is_set():
            first = ""
            if not reopen:
                if self._stop.wait(POLL_SECONDS) or not self.keyboard.waiting():
                    continue
                try:
                    first = self._opening_key()
                except Exception:
                    logger.exception("Reading a keypress failed; carrying on without it.")
                    continue
                if first == CANCEL:
                    reopen = self.on_cancel()
                    continue
                if not first:
                    continue
            reopen = False
            try:
                line = self.read_line(first)
            except Exception:
                logger.exception("Reading a typed line failed; carrying on without it.")
                self.ui.release()
                continue
            if self.cancelled:
                # Straight back into a prompt, without waiting for a keypress
                # this time. Stopping something is nearly always followed by
                # saying what you wanted instead.
                reopen = self.on_cancel()
                continue
            if line:
                logger.info("Typed: %s", line)
                self.on_line(line)

    def _opening_key(self) -> str:
        """The character a line starts with, escape, or "" for neither.

        Read before anything is drawn, so a key that types nothing costs
        nothing. Function and arrow keys arrive as a prefix and then a code,
        and the toggles come through as something the console has no character
        for - all of them go, silently, with no prompt opened for them.
        """
        key = self.keyboard.read()
        if key in PREFIXES:
            self.keyboard.read()
            return ""
        if key == CANCEL:
            return CANCEL
        return key if key >= " " else ""

    def read_line(self, first: str = "") -> str:
        """One line, echoed as it is typed. Empty if it was abandoned.

        `first` is the character that opened it, already read.

        Escape throws the line away, which is the way out for somebody who
        pressed a key by accident - and pressing a key by accident is the whole
        reason this waits for one rather than showing a prompt. On an empty line
        there is nothing to throw away, so it means the other thing you would
        want from that key: stop what you are doing.
        """
        self.ui.hold(self._repaint)
        self.cancelled = False
        self._typed = list(first)
        # Counted apart from what was typed, because escape empties the line
        # without unprinting it - and then the wipe below comes up short and
        # leaves half an abandoned sentence on screen.
        self._shown = len(self._typed)
        try:
            # The same blue as every other `you >`, because it is the same
            # thing: a line from them, on its way in.
            self.ui.raw(paint("user", self.prompt, self.ui.colour) + first)
            while not self._stop.is_set():
                key = self.keyboard.read()
                if key in PREFIXES:
                    self.keyboard.read()
                elif key in ENTER:
                    break
                elif key == CANCEL:
                    self.cancelled = not self._typed
                    self._typed = []
                    break
                elif key in BACKSPACE:
                    if self._typed:
                        self._typed.pop()
                        self._shown -= 1
                        # Back over the character, paint a space on it, back again.
                        self.ui.raw("\b \b")
                elif key >= " ":
                    self._typed.append(key)
                    self._shown += 1
                    self.ui.raw(key)
        finally:
            # The whole line goes, whether it was sent or abandoned. What was
            # sent is redrawn properly a moment later as `you > ...`, and what
            # was abandoned should leave nothing behind at all.
            self.ui.raw("\r" + " " * (len(self.prompt) + self._shown + 1) + "\r")
            self.ui.release()
        return "".join(self._typed).strip()

    def _repaint(self) -> None:
        """Draw the prompt and the half written line again, where the cursor is.

        Called by the terminal when something permanent has taken the row out
        from under it - a reply, a tool line, the note that says a turn was
        cancelled. Without it, anything typed while JARVIS was working vanished
        off the screen while still counting towards what gets sent.
        """
        self.ui.raw(paint("user", self.prompt, self.ui.colour) + "".join(self._typed))
        self._shown = len(self._typed)
