"""Fakes shared between the screen tests and the tool tests.

Here rather than in either of them because pytest only guarantees this file is
importable from both - a test module importing another test module works only
while pytest happens to be run from the repository root.
"""

from __future__ import annotations

from jarvis.screen import Element


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
    ) -> None:
        self._elements = list(elements)
        self.title = title
        self.hwnd = hwnd
        self.front = front
        self._minimised = minimised
        self.rect = rect
        self.others = list(others)
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
        """Deepest first, as ElementFromPoint plus a walk up the parents gives."""
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
