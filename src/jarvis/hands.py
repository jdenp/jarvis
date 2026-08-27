"""Synthetic mouse and keyboard input, through SendInput.

Real input events rather than UI Automation's Invoke pattern. Invoke is tidier
where a control supports it, but coverage is patchy - half of what a browser or
an Electron app draws has no pattern at all - and a control that quietly does
nothing is worse than one that behaves exactly as it does under a real hand.

Windows refuses input from a process to any window running at a higher
privilege, silently. An elevated app reads perfectly and ignores every click.
"""

from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes
from typing import ClassVar

logger = logging.getLogger("jarvis.screen")

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
WHEEL_DELTA = 120

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

BUTTONS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}

# Everything a keyboard shortcut is likely to name. Letters and digits are
# added below rather than written out.
KEYS: dict[str, int] = {
    "alt": 0x12,
    "backspace": 0x08,
    "capslock": 0x14,
    "ctrl": 0x11,
    "control": 0x11,
    "delete": 0x2E,
    "del": 0x2E,
    "down": 0x28,
    "end": 0x23,
    "enter": 0x0D,
    "return": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "home": 0x24,
    "insert": 0x2D,
    "left": 0x25,
    "pagedown": 0x22,
    "pageup": 0x21,
    "right": 0x27,
    "shift": 0x10,
    "space": 0x20,
    "tab": 0x09,
    "up": 0x26,
    "win": 0x5B,
}
# Media and volume keys go to whichever application Windows considers the
# current media session, with no window to find and nothing to scan. "play my
# music" is one of these, not a taskbar hunt.
KEYS.update(
    {
        "mute": 0xAD,
        "volumedown": 0xAE,
        "volumeup": 0xAF,
        "next": 0xB0,
        "nexttrack": 0xB0,
        "previous": 0xB1,
        "prevtrack": 0xB1,
        "stop": 0xB2,
        "play": 0xB3,
        "pause": 0xB3,
        "playpause": 0xB3,
    }
)
KEYS.update({chr(code): code for code in range(ord("A"), ord("Z") + 1)})
KEYS.update({chr(code).lower(): code for code in range(ord("A"), ord("Z") + 1)})
KEYS.update({str(digit): 0x30 + digit for digit in range(10)})
KEYS.update({f"f{number}": 0x6F + number for number in range(1, 13)})

# Arrow keys, home/end and the rest sit on the extended half of the keyboard.
# Without the flag some applications read them as their numeric keypad twins.
EXTENDED = frozenset(
    {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B}
    | {0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3}  # media keys, as a real keyboard sends them
)

MODIFIERS = frozenset({"alt", "ctrl", "control", "shift", "win"})


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _EVENT(ctypes.Union):
    _fields_: ClassVar = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("event",)
    _fields_ = [("type", wintypes.DWORD), ("event", _EVENT)]


def keys_for(combination: str) -> tuple[int, ...]:
    """Virtual key codes for "ctrl+shift+s", in the order they are pressed.

    Raises ValueError naming the part it did not recognise, because a shortcut
    silently half-pressed is worse than one that refused.
    """
    parts = [part.strip().lower() for part in combination.split("+") if part.strip()]
    if not parts:
        raise ValueError("No key given.")
    codes = []
    for part in parts:
        code = KEYS.get(part)
        if code is None:
            raise ValueError(f"Unknown key {part!r} in {combination!r}.")
        codes.append(code)
    return tuple(codes)


def move(x: int, y: int) -> None:
    ctypes.windll.user32.SetCursorPos(int(x), int(y))


def click(x: int, y: int, *, button: str = "left", count: int = 1, settle: float = 0.05) -> None:
    """Move the pointer and press, as a hand would."""
    down, up = BUTTONS[button]
    move(x, y)
    # Hover states, tooltips and focus follow the pointer, and some controls
    # only become clickable once they have seen it arrive.
    time.sleep(settle)
    for _ in range(max(1, count)):
        _send(_mouse(down), _mouse(up))


def scroll(x: int, y: int, clicks: int, *, settle: float = 0.05) -> None:
    """Wheel notches at a point. Positive scrolls up, negative down."""
    move(x, y)
    time.sleep(settle)
    _send(_mouse(MOUSEEVENTF_WHEEL, data=clicks * WHEEL_DELTA))


def type_text(text: str) -> None:
    """Type a string as unicode, so it does not depend on the keyboard layout.

    Newlines are sent as the enter key. A literal line feed is not what a text
    box acts on, and passing one through gets a blank line where a form was
    meant to be submitted.
    """
    events: list[_INPUT] = []
    for character in text.replace("\r\n", "\n"):
        if character in "\r\n":
            events += [_key(KEYS["enter"]), _key(KEYS["enter"], up=True)]
            continue
        # Anything outside the basic plane is two UTF-16 units and has to be
        # sent as two events, or it arrives as a pair of replacement marks.
        for unit in _utf16_units(character):
            events += [_unicode(unit), _unicode(unit, up=True)]
    if events:
        _send(*events)


def press(combination: str) -> tuple[int, ...]:
    """Press a shortcut, holding the modifiers and releasing in reverse."""
    codes = keys_for(combination)
    events = [_key(code) for code in codes]
    events += [_key(code, up=True) for code in reversed(codes)]
    _send(*events)
    return codes


def _utf16_units(character: str) -> tuple[int, ...]:
    encoded = character.encode("utf-16-le")
    return tuple(
        int.from_bytes(encoded[index : index + 2], "little") for index in range(0, len(encoded), 2)
    )


def _mouse(flags: int, data: int = 0) -> _INPUT:
    event = _INPUT(type=INPUT_MOUSE)
    event.mi = _MOUSEINPUT(0, 0, ctypes.c_uint32(data).value, flags, 0, None)
    return event


def _key(code: int, *, up: bool = False) -> _INPUT:
    flags = KEYEVENTF_KEYUP if up else 0
    if code in EXTENDED:
        flags |= KEYEVENTF_EXTENDEDKEY
    event = _INPUT(type=INPUT_KEYBOARD)
    event.ki = _KEYBDINPUT(code, 0, flags, 0, None)
    return event


def _unicode(unit: int, *, up: bool = False) -> _INPUT:
    flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    event = _INPUT(type=INPUT_KEYBOARD)
    event.ki = _KEYBDINPUT(0, unit, flags, 0, None)
    return event


def _send(*events: _INPUT) -> None:
    """One SendInput call for the lot, so nothing interleaves with it."""
    batch = (_INPUT * len(events))(*events)
    sent = ctypes.windll.user32.SendInput(len(events), batch, ctypes.sizeof(_INPUT))
    if sent != len(events):
        logger.warning(
            "SendInput accepted %d of %d events - is a UAC prompt up?", sent, len(events)
        )
