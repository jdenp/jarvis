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

# 256 colour rather than the basic sixteen, because orange has no equivalent
# there - yellow is as close as that palette gets, and it is not close. Every
# terminal that takes escape codes at all takes these, and the palette entries
# are the same shade on every machine.
#
# `jarvis` and `art` are deliberately the same 208: the name at startup and the
# name in front of every reply are one thing, and two oranges a shade apart read
# as a mistake rather than as a distinction.
COLOUR = {
    "reset": "\033[0m",
    "dim": "\033[2m",
    "user": "\033[36m",
    "jarvis": "\033[1;38;5;208m",
    "art": "\033[38;5;208m",
    "loud": "\033[1m",
    "warn": "\033[33m",
    "tool": "\033[35m",
}

# A tool line has to be findable as it scrolls past at speaking speed, so the
# name is coloured and the arguments are not. Dim on its own was invisible on a
# dark terminal, which is the whole reason this is here.
RUNS = ">"
GAVE = " "

# Up one row, leaving the column alone. What gives the live line back its place
# after somebody has typed on the row beneath it.
UP = "\033[A"
# Save and restore the cursor, DEC style. How the live line is redrawn a row
# above somebody who is part way through typing without moving their caret.
SAVE = "\0337"
RESTORE = "\0338"


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
    def hold(self, repaint=None) -> None: ...
    def release(self) -> None: ...
    def raw(self, text: str) -> None: ...
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
        # Re-entrant: a permanent line landing mid typing asks whoever is typing
        # to draw their row again, and that comes back through raw().
        self._lock = threading.RLock()
        self._status = ""
        self._meter = ""
        self._width = 0
        self._tick = 0
        self._stop = threading.Event()
        self._spinner: threading.Thread | None = None
        # Set while somebody is typing on the bottom line. Everything permanent
        # still scrolls above; only the live line stands aside, because it and a
        # half typed sentence want the same row.
        self._held = False
        # Whether hold() pushed a row, and so whether release() owes one back.
        self._stepped = False
        # How whoever is typing puts their row back after something permanent
        # has been written over it.
        self._repaint = None

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
            if self._held:
                self._over_the_typing(text)
                return
            # Something permanent arriving mid-sentence moves every row down,
            # so the way back up is no longer where it was. Forgetting the debt
            # costs the status one redraw; paying it wrongly costs a line of
            # whatever it lands on.
            self._stepped = False
            self._erase()
            self.stream.write(text + "\n")
            self.stream.flush()
            self._draw()

    def _over_the_typing(self, text: str) -> None:
        """Write a permanent line while somebody is part way through one.

        The cursor is on the row they borrowed rather than on the live line, so
        the ordinary erase would wipe their sentence and print on top of it -
        which happened every time escape landed, the cancel note arriving at the
        moment the prompt came back. Their row is cleared, the live line above
        is spent on the permanent text, and they are asked to draw themselves
        again underneath: one row given up and one taken, so release() still
        knows the way back.
        """
        self._wipe_row(self.width() - 1)
        if self._stepped:
            # Up onto the live line, which is spent on the permanent text - and
            # then a fresh row is pushed for the live line to come back on, so
            # the next tick does not draw the status over what was just said.
            self.stream.write(UP)
            self._wipe_row(self._width)
            self._width = 0
            self.stream.write(text + "\n\n")
        else:
            self.stream.write(text + "\n")
        self.stream.flush()
        if self._repaint is not None:
            self._repaint()

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
            # Room for the numbers on the right, which do not move for it.
            self._status = tail(text, self.width() - len(self._meter) - 8)

    def width(self) -> int:
        try:
            return max(30, shutil.get_terminal_size().columns)
        except OSError:
            return 80

    def meter(self, text: str) -> None:
        """The numbers on the end of the live line: what the session has cost.

        Set apart from the status because it changes on a different clock - the
        status every few seconds, this once a turn - and because it is the thing
        worth having in the corner of your eye rather than in the log.
        """
        with self._lock:
            self._meter = text

    def resting(self) -> None:
        """Nothing is happening. Clears the live line without printing."""
        with self._lock:
            self._status = ""
            if self._held and self._stepped:
                self.stream.write(SAVE + UP)
                self._wipe_row(self._width)
                self.stream.write(RESTORE)
                self._width = 0
            else:
                self._erase()
            self.stream.flush()

    def hold(self, repaint=None) -> None:
        """Take the row beneath the live line, for somebody to type on.

        Beneath rather than over: the first version erased the status, so the
        moment you started typing the terminal stopped saying whether JARVIS was
        listening or thinking, and looked dead for as long as you took. It stays
        where it is and typing happens on a new row under it.
        """
        with self._lock:
            self._held = True
            self._repaint = repaint
            # Only if there is something to keep. With nothing drawn there is
            # no reason to push a blank row in front of whoever is typing.
            self._stepped = self.live and self._width > 0
            if self._stepped:
                self.stream.write("\n")
                self.stream.flush()

    def release(self) -> None:
        """Give the row back and carry on saying what is happening."""
        with self._lock:
            self._held = False
            self._repaint = None
            if self._stepped:
                self._stepped = False
                self.stream.write(UP)
            self._draw()

    def raw(self, text: str) -> None:
        """Write exactly this, now. For echoing keystrokes while held."""
        with self._lock:
            self.stream.write(text)
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
        """Repaint the live line. Caller holds the lock.

        The whole thing sits at the right hand end - what is happening, then
        what it has cost - so it reads as one status line in the corner rather
        than two things at opposite ends of an empty row.
        """
        if not self.live or not self._status:
            return
        # Held, but with no row of its own to draw on: hold() found nothing
        # drawn and pushed nothing, so the row above belongs to the
        # conversation and writing there would take a line of it.
        if self._held and not self._stepped:
            return
        frame = self.frames[self._tick % len(self.frames)]
        text = f"{frame} {self._status}"
        if self._meter:
            text += f" - {self._meter}"
        # Cut from the left rather than wrapped. A status line that wraps is two
        # rows and only one of them can be erased; and the numbers on the right
        # are the part worth keeping when the window is narrow.
        room = self.width() - 1
        text = text[-room:] if len(text) > room else text.rjust(room)
        painted = paint("dim", text, self.colour)
        if self._held:
            # A row up, and back to where their caret was. Without this the
            # line stopped the moment a prompt opened and stayed stopped, which
            # after escape - where the prompt reopens by itself and nobody is
            # necessarily about to type - read as the terminal having died.
            self.stream.write(SAVE + UP)
            self._wipe_row(self._width)
            self.stream.write(painted + RESTORE)
        else:
            self._erase()
            self.stream.write(painted)
        self.stream.flush()
        self._width = len(text)

    def _wipe_row(self, width: int) -> None:
        """Blank the row the cursor is on, leaving it at the start."""
        self.stream.write("\r" + " " * max(0, width) + "\r")

    def _erase(self) -> None:
        """Wipe the live line. Caller holds the lock.

        Only when the cursor is actually on it. Held, it is a row up and the
        row under the cursor belongs to whoever is typing.
        """
        if not self._width or self._held:
            return
        self._wipe_row(self._width)
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
