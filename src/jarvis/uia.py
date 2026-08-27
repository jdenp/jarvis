"""UI Automation, reached through comtypes. The only Windows-specific part.

Nothing here decides anything. It reads the tree, flattens each node to a plain
`Element` and hands it to `screen.py`, which is where the judgement lives and is
the reason that half can be tested without a desktop.

Every property is fetched through a cache request rather than one COM call per
attribute. That is the difference between a scan and a stall: 810 elements cost
0.17s cached against several seconds of round trips read one at a time.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import threading
from ctypes import wintypes

from .screen import Element

logger = logging.getLogger("jarvis.screen")

# DwmGetWindowAttribute: a cloaked window is one Windows is keeping alive but
# not showing. Suspended UWP apps leave several around and they scan as empty.
DWMWA_CLOAKED = 14

# Ancestors walked up from whatever is under a point before giving up looking
# for the element that was aimed at.
CHAIN_DEPTH = 6

# The shell's own windows carry no title at all, so the usual "visible and
# titled" filter drops them - and the taskbar is where half of what anyone would
# ask for lives. Named by class instead. Nothing else untitled gets through.
SHELL_WINDOWS = {
    "Shell_TrayWnd": "Taskbar",
    "Shell_SecondaryTrayWnd": "Taskbar (second screen)",
}

_local = threading.local()


class UiaBackend:
    """Reads the accessibility tree. One automation object per thread.

    COM apartments do not share objects, and an MCP client may well call one
    tool from a different thread than the last. Rather than marshal, each
    thread gets its own - it costs a few milliseconds once, and everything
    handed out of here is plain data that crosses threads freely.
    """

    def windows(self) -> list[tuple[int, str]]:
        """Every visible, titled, uncloaked top level window."""
        user32 = ctypes.windll.user32
        found: list[tuple[int, str]] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def collect(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd) and not _cloaked(hwnd):
                title = _window_text(hwnd) or SHELL_WINDOWS.get(_window_class(hwnd), "")
                if title:
                    found.append((int(hwnd), title))
            return True

        user32.EnumWindows(collect, 0)
        return found

    def foreground(self) -> tuple[int, str]:
        hwnd = int(ctypes.windll.user32.GetForegroundWindow())
        return hwnd, _window_text(hwnd)

    def minimised(self, hwnd: int) -> bool:
        """Whether a window is minimised, which its element rectangles do not say.

        A minimised window still reports a full tree with plausible looking
        coordinates, left over from when it was last drawn. GetWindowRect is the
        only honest witness - it answers -32000 - and IsIconic is the tidy way
        to ask the same question.
        """
        return bool(ctypes.windll.user32.IsIconic(wintypes.HWND(hwnd)))

    def window_rect(self, hwnd: int) -> tuple[int, int, int, int]:
        rect = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect))
        return rect.left, rect.top, rect.right, rect.bottom

    def elements(self, hwnd: int) -> list[Element]:
        """Every descendant of a window, flattened."""
        uia, automation, cache = _automation()
        root = automation.ElementFromHandle(wintypes.HWND(hwnd))
        every = automation.CreateTrueCondition()
        found = root.FindAllBuildCache(uia.TreeScope_Descendants, every, cache)
        return [_flatten(found.GetElement(index)) for index in range(found.Length)]

    def chain_at(self, x: int, y: int) -> list[Element]:
        """What is under a point, then its ancestors, outermost last."""
        _uia, automation, _cache = _automation()
        walker = automation.RawViewWalker
        node = automation.ElementFromPoint(wintypes.POINT(x, y))
        chain: list[Element] = []
        for _ in range(CHAIN_DEPTH):
            if not node:
                break
            chain.append(_flatten(node, cached=False))
            node = walker.GetParentElement(node)
        return chain

    def activate(self, hwnd: int) -> bool:
        """Bring a window to the front, so input goes where it was aimed."""
        user32 = ctypes.windll.user32
        handle = wintypes.HWND(hwnd)
        if user32.IsIconic(handle):
            user32.ShowWindow(handle, 9)  # SW_RESTORE

        # SetForegroundWindow only obeys the process that already owns the
        # foreground. Borrowing that thread's input state is the long-standing
        # way round it, and failing it is not fatal - the click still lands.
        ours = ctypes.windll.kernel32.GetCurrentThreadId()
        theirs = user32.GetWindowThreadProcessId(handle, None)
        attached = bool(user32.AttachThreadInput(theirs, ours, True)) if theirs != ours else False
        try:
            return bool(user32.SetForegroundWindow(handle))
        finally:
            if attached:
                user32.AttachThreadInput(theirs, ours, False)


def _automation():
    """The per-thread UI Automation object, its module and a filled cache request."""
    ready = getattr(_local, "automation", None)
    if ready is not None:
        return ready

    import comtypes
    import comtypes.client

    _set_dpi_aware()
    # Already initialised on this thread is the outcome wanted anyway.
    with contextlib.suppress(OSError):
        comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)

    uia = comtypes.client.GetModule("UIAutomationCore.dll")
    automation = comtypes.client.CreateObject(uia.CUIAutomation, interface=uia.IUIAutomation)

    cache = automation.CreateCacheRequest()
    for prop in _CACHED_PROPERTIES:
        cache.AddProperty(getattr(uia, prop))
    # Element, not Descendants. Descendants is rejected outright here with
    # "the parameter is incorrect", which reads like the scope on FindAll and
    # is not - that one is passed separately and does the actual walking.
    cache.TreeScope = uia.TreeScope_Element

    _local.automation = (uia, automation, cache)
    _local.roles = {
        value: name[4:-13]
        for name, value in vars(uia).items()
        if name.startswith("UIA_") and name.endswith("ControlTypeId")
    }
    return _local.automation


_CACHED_PROPERTIES = (
    "UIA_NamePropertyId",
    "UIA_ControlTypePropertyId",
    "UIA_BoundingRectanglePropertyId",
    "UIA_IsOffscreenPropertyId",
    "UIA_IsEnabledPropertyId",
    "UIA_AutomationIdPropertyId",
    "UIA_RuntimeIdPropertyId",
    "UIA_IsKeyboardFocusablePropertyId",
)


def _flatten(node, *, cached: bool = True) -> Element:
    """One COM element to plain data, tolerating a control that died mid scan."""
    prefix = "Cached" if cached else "Current"

    def read(attribute, fallback):
        try:
            value = getattr(node, prefix + attribute)
            return fallback if value is None else value
        except Exception:  # any COM failure means the element went away mid scan
            return fallback

    rect = read("BoundingRectangle", None)
    left, top, right, bottom = (
        (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
        if rect is not None
        else (0, 0, 0, 0)
    )
    try:
        runtime_id = tuple(node.GetRuntimeId() or ())
    except Exception:
        runtime_id = ()

    return Element(
        name=str(read("Name", "")),
        role=_local.roles.get(read("ControlType", 0), "Unknown"),
        left=left,
        top=top,
        width=max(0, right - left),
        height=max(0, bottom - top),
        enabled=bool(read("IsEnabled", True)),
        offscreen=bool(read("IsOffscreen", False)),
        focusable=bool(read("IsKeyboardFocusable", False)),
        automation_id=str(read("AutomationId", "")),
        runtime_id=runtime_id,
    )


def _window_text(hwnd) -> str:
    buffer = ctypes.create_unicode_buffer(512)
    ctypes.windll.user32.GetWindowTextW(hwnd, buffer, 512)
    return buffer.value.strip()


def _window_class(hwnd) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetClassNameW(hwnd, buffer, 256)
    return buffer.value


def _cloaked(hwnd) -> bool:
    state = ctypes.c_int(0)
    ctypes.windll.dwmapi.DwmGetWindowAttribute(
        hwnd, DWMWA_CLOAKED, ctypes.byref(state), ctypes.sizeof(state)
    )
    return bool(state.value)


def _set_dpi_aware() -> None:
    """Without this, every coordinate is a lie on a scaled display.

    Windows reports rectangles in virtual pixels to a process that has not said
    it understands scaling, so a click at 150% lands two thirds of the way to
    where it was aimed. Failing here means it was already set, by a manifest or
    by an earlier call, which is the outcome wanted anyway.
    """
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)  # PER_MONITOR_AWARE_V2
        return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        logger.debug("Could not set DPI awareness; coordinates may be scaled.")
