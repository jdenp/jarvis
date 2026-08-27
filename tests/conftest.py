"""Fakes and fixtures shared across the suite.

The fakes live here rather than in either test module because pytest only
guarantees this file is importable from both - a test module importing another
test module works only while pytest happens to be run from the repository root.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from jarvis.screen import Element


@pytest.fixture(autouse=True, scope="session")
def _keep_the_suite_out_of_the_real_log():
    """Send test logging to a temporary directory, not to logs/jarvis.log.

    `cli.main()` configures logging, so any test that goes through it attached a
    rotating file handler to the repository's own log - and from then on every
    warning any test provoked was written there too. Diagnosing a live session
    then meant reading past "Unknown key 'nope'", "Pillow is not installed" and
    a dozen dropped-phrase warnings, none of which had happened to the user.
    Measured at ~3.5KB of noise per run.
    """
    from jarvis import logging_setup

    real = logging_setup.configure
    elsewhere = Path(tempfile.mkdtemp(prefix="jarvis-tests-"))

    def configure(log_dir, level="INFO", console=True):
        return real(elsewhere, level, console=False)

    patch = pytest.MonkeyPatch()
    patch.setattr(logging_setup, "configure", configure)
    # cli.py imported the name directly, so patching the module is not enough.
    patch.setattr("jarvis.cli.configure", configure)
    yield
    patch.undo()


def button(name, left=0, top=0, width=80, height=24, **kwargs) -> Element:
    return Element(
        name=name, role="Button", left=left, top=top, width=width, height=height, **kwargs
    )


class FakeDesktop:
    """Stands in for UI Automation, with a real notion of what covers what."""

    def __init__(
        self,
        elements=(),
        *,
        title="Notepad",
        hwnd=1,
        front=1,
        minimised=False,
        rect=(0, 0, 800, 600),
        others=(),
        always_visible=False,
    ) -> None:
        self._elements = list(elements)
        self.title = title
        self.hwnd = hwnd
        self.front = front
        self._minimised = minimised
        self.rect = rect
        self.others = list(others)
        # The taskbar is always on top, so its targets are under the pointer even
        # when it is not the foreground window - and SetForegroundWindow refuses
        # to raise it. Everything else is covered when it is not in front.
        self.always_visible = always_visible
        self.activations: list[int] = []

    def windows(self):
        return [(self.hwnd, self.title), *self.others]

    def foreground(self):
        found = next((w for w in self.windows() if w[0] == self.front), None)
        return found or (self.front, "something else")

    def minimised(self, hwnd):
        return self._minimised

    def window_rect(self, hwnd):
        return self.rect

    def elements(self, hwnd):
        return list(self._elements)

    def chain_at(self, x, y):
        """Deepest first, as ElementFromPoint plus a walk up the parents gives.

        Models occlusion, because the code under test decides whether to raise a
        window on the strength of what is under the point.
        """
        if self.front != self.hwnd and not self.always_visible:
            return [Element("", "Window", *self.rect[:2], 10, 10)]
        over = [
            element
            for element in self._elements
            if element.left <= x <= element.right and element.top <= y <= element.bottom
        ]
        return sorted(over, key=lambda element: element.area)

    def activate(self, hwnd):
        self.activations.append(hwnd)
        self.front = hwnd
        return True
