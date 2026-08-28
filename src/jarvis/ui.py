"""The terminal JARVIS runs in.

One renderer for both front ends, because there is one conversation whether it
arrived by microphone or by keyboard. Voice mode and `jarvis chat` look the same
and the difference is only where the words came from.

Not a full screen application. Permanent lines scroll normally and a single live
line sits under them showing what is happening now - listening, thinking, running
something - which is redrawn in place and erased before anything permanent is
written. That is the whole trick, and it is what makes a terminal look alive
without an alternate buffer, a layout engine or a dependency. `rich` and
`textual` would both do more; neither is worth the install for a status line and
five colours.

Everything is guarded on the output actually being a terminal. Piped or
redirected, it degrades to plain lines with no escape codes and no animation, so
`jarvis chat < script.txt` produces something readable rather than a file full of
brackets.
"""

from __future__ import annotations

import logging
import shutil
import sys
import threading

# Braille where the console can encode it, because it is smoother, and the plain
# spinner where it cannot - a row of boxes is worse than a rotating bar.
FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
PLAIN_FRAMES = "|/-\\"
TICK_SECONDS = 0.12

COLOUR = {
    "reset": "\033[0m",
    "dim": "\033[2m",
    "user": "\033[36m",
    "jarvis": "\033[1;33m",
    "loud": "\033[1m",
    "warn": "\033[33m",
    "tool": "\033[35m",
}

# A tool line has to be findable as it scrolls past at speaking speed, so the
# name is coloured and the arguments are not. Dim on its own was invisible on a
# dark terminal, which is the whole reason this is here.
RUNS = ">"
GAVE = " "


def tail(text: str, width: int) -> str:
    """The last line of something, short enough to fit on one.

    Reasoning arrives as paragraphs and only the end of it is current, so the
    live line shows where the model has got to rather than where it started.
    """
    last = " ".join(text.split())
    if width <= 1 or len(last) <= width:
        return last
    return "..." + last[-(width - 3) :]


def paint(name: str, text: str, colour: bool = True) -> str:
    if not colour or name not in COLOUR:
        return text
    return f"{COLOUR[name]}{text}{COLOUR['reset']}"


def usable(stream) -> bool:
    """Whether this stream is a terminal that can take escape codes.

    Windows Terminal turns VT processing on itself; conhost does not, and without
    it every colour comes out as visible bracket noise.
    """
    if not hasattr(stream, "isatty") or not stream.isatty():
        return False
    try:
        import ctypes

        handle = ctypes.windll.kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        if ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            ctypes.windll.kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except (AttributeError, OSError):
        return False
    return True


class Silent:
    """A terminal that says nothing.

    The default everywhere, so nothing has to check whether it has a screen -
    the MCP server has stdout reserved for JSON-RPC and the tests have no
    terminal at all.
    """

    colour = False

    def heard(self, text: str) -> None: ...
    def spoke(self, text: str) -> None: ...
    def tool(self, name: str, arguments: str = "") -> None: ...
    def result(self, text: str) -> None: ...
    def note(self, text: str) -> None: ...
    def warn(self, text: str) -> None: ...
    def status(self, text: str) -> None: ...
    def thinking(self, text: str) -> None: ...
    def resting(self) -> None: ...
    def ask(self, prompt: str) -> str:
        return input(prompt)

    def close(self) -> None: ...


class Ui:
    """Scrolling conversation with one live line under it."""

    def __init__(self, stream=None, colour: bool | None = None) -> None:
        self.stream = stream or sys.stdout
        self.colour = usable(self.stream) if colour is None else colour
        # Animation needs a terminal to erase on. Colour is the same question, so
        # one answer covers both.
        self.live = self.colour
        self.frames = self._frames()
        self._lock = threading.Lock()
        self._status = ""
        self._meter = ""
        self._width = 0
        self._tick = 0
        self._stop = threading.Event()
        self._spinner: threading.Thread | None = None

    def _frames(self) -> str:
        encoding = getattr(self.stream, "encoding", None) or "ascii"
        try:
            FRAMES.encode(encoding)
        except (UnicodeEncodeError, LookupError):
            return PLAIN_FRAMES
        return FRAMES

    # ------------------------------------------------------------- permanent

    def line(self, text: str = "") -> None:
        """Write something that stays, above the live line."""
        with self._lock:
            self._erase()
            self.stream.write(text + "\n")
            self.stream.flush()
            self._draw()

    def banner(self, version: str, notes: list[str]) -> None:
        self.line(paint("loud", f"JARVIS {version}", self.colour))
        for note in notes:
            self.line(paint("dim", f"  {note}", self.colour))
        self.line()

    def heard(self, text: str) -> None:
        self.line(f"{paint('user', 'you >', self.colour)} {text}")

    def spoke(self, text: str) -> None:
        self.line(f"{paint('jarvis', 'jarvis >', self.colour)} {text}")

    def tool(self, name: str, arguments: str = "") -> None:
        marker = paint("tool", f"  {RUNS} {name}", self.colour)
        self.line(f"{marker}{paint('dim', arguments, self.colour)}")

    def result(self, text: str) -> None:
        """The first line of what a tool gave back.

        Without it a call is only a claim: you can see that look_at_screen ran
        and not that it found nothing, which is the moment you most want to know.
        """
        lines = text.splitlines()
        first = tail(lines[0] if lines else "", self.width() - 8)
        self.line(paint("dim", f"  {GAVE} {first}", self.colour))

    def note(self, text: str) -> None:
        self.line(paint("dim", text, self.colour))

    def warn(self, text: str) -> None:
        self.line(paint("warn", text, self.colour))

    # ------------------------------------------------------------------ live

    def status(self, text: str) -> None:
        """Say what is happening now, on the line that redraws in place."""
        with self._lock:
            self._status = text
            if self.live and text and self._spinner is None:
                self._stop.clear()
                self._spinner = threading.Thread(
                    target=self._animate, name="jarvis-ui", daemon=True
                )
                self._spinner.start()
            self._draw()

    def thinking(self, text: str) -> None:
        """Show the model's reasoning on the live line as it arrives.

        The tail of it, on one line, and then it is gone - the same shape as
        every other agent terminal, because thinking is worth watching and not
        worth keeping. Set without redrawing: tokens arrive far faster than
        anybody can read, so the animation thread picks it up at its own pace
        and the terminal is written to eight times a second instead of hundreds.
        """
        with self._lock:
            self._status = tail(text, self.width() - 4)

    def width(self) -> int:
        try:
            return max(30, shutil.get_terminal_size().columns)
        except OSError:
            return 80

    def meter(self, text: str) -> None:
        """The right hand end of the live line: how full the context is.

        Separate from the status because it changes on a different clock - the
        status every few seconds, this once a turn - and because it is the one
        number worth having in the corner of your eye rather than in the log.
        """
        with self._lock:
            self._meter = text

    def resting(self) -> None:
        """Nothing is happening. Clears the live line without printing."""
        with self._lock:
            self._status = ""
            self._erase()
            self.stream.flush()

    def ask(self, prompt: str) -> str:
        """Read a line, with the live line out of the way first.

        `input()` writes its own prompt and echoes what is typed, so anything
        already on that line would be typed over.
        """
        self.resting()
        return input(paint("user", prompt, self.colour))

    def close(self) -> None:
        self._stop.set()
        self.resting()

    # --------------------------------------------------------------- drawing

    def _animate(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            with self._lock:
                if not self._status:
                    continue
                self._tick += 1
                self._draw()

    def _draw(self) -> None:
        """Repaint the live line. Caller holds the lock."""
        if not self.live or not self._status:
            return
        frame = self.frames[self._tick % len(self.frames)]
        text = f"{frame} {self._status}"
        # Right aligned on the same line, and dropped rather than wrapped when
        # the window is too narrow - a status line that wraps is two lines that
        # cannot be erased.
        room = self.width() - 1
        if self._meter and len(text) + len(self._meter) + 2 <= room:
            text = text.ljust(room - len(self._meter)) + self._meter
        text = text[:room]
        self._erase()
        self.stream.write(paint("dim", text, self.colour))
        self.stream.flush()
        self._width = len(text)

    def _erase(self) -> None:
        """Wipe the live line. Caller holds the lock."""
        if not self._width:
            return
        self.stream.write("\r" + " " * self._width + "\r")
        self._width = 0


class LogToUi(logging.Handler):
    """Warnings and errors, rendered as part of the conversation.

    The plain console handler writes straight to stdout, which tramples the live
    line and interleaves badly with everything above it. This routes the ones
    worth seeing through the same renderer; everything at INFO and below is still
    in the file, which is where the detail belongs.
    """

    def __init__(self, ui: Ui, level: int = logging.WARNING) -> None:
        super().__init__(level)
        self.ui = ui

    def emit(self, record: logging.LogRecord) -> None:
        try:
            where = record.name.removeprefix("jarvis.")
            self.ui.warn(f"  ! {where}: {record.getMessage()}")
        except Exception:  # a broken log line must not take the process with it
            self.handleError(record)
