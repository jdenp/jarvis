"""The same numbered targets, burned onto a screenshot.

Written for whoever is debugging a misclick. "It pressed the wrong thing" is
unanswerable from a log of labels, and obvious the moment you can see which box
was numbered what. A vision model can read the same image - that is what
`screen.send_image` is for - but the picture earns its keep either way.

Needs Pillow: uv sync --extra screen
"""

from __future__ import annotations

import logging
from pathlib import Path

from .screen import Scan

logger = logging.getLogger("jarvis.screen")

# Cycled so that two boxes side by side are never the same colour.
PALETTE = (
    (220, 30, 40),
    (20, 110, 220),
    (30, 150, 60),
    (200, 60, 200),
    (225, 130, 0),
)

BADGE_HEIGHT = 17
OUTLINE = 2


class MarksUnavailable(RuntimeError):
    """Pillow is not installed, so nothing can be drawn."""


def crop_box(
    bounds: tuple[int, int, int, int], origin: tuple[int, int]
) -> tuple[int, int, int, int]:
    """A screen rectangle, in the coordinates of a whole-virtual-screen grab.

    The virtual screen starts wherever the leftmost monitor does, which on a
    two monitor desk is a negative number. Everything on screen is measured
    from there, and the grab is measured from its own top left corner.
    """
    left, top, right, bottom = bounds
    origin_x, origin_y = origin
    return left - origin_x, top - origin_y, right - origin_x, bottom - origin_y


def place(x: int, y: int, bounds: tuple[int, int, int, int]) -> tuple[int, int]:
    """A screen coordinate, relative to the cropped window image."""
    return x - bounds[0], y - bounds[1]


def capture(bounds: tuple[int, int, int, int] | None, path: Path, max_width: int = 0) -> Path:
    """Save a screenshot of one window, or of the whole desk if bounds is None.

    The plain picture, with nothing drawn on it. For the times when the numbered
    list is not the question - an error dialog, a chart, anything where what is
    wanted is to see it rather than to press it.
    """
    grab, _draw, _font = _pillow()
    shot = grab.grab(all_screens=True)
    if bounds is not None:
        shot = shot.crop(crop_box(bounds, _virtual_origin()))
    shot = _shrink(shot, max_width)
    path.parent.mkdir(parents=True, exist_ok=True)
    shot.convert("RGB").save(path, "PNG", optimize=True)
    logger.info("Captured %dx%d to %s", shot.width, shot.height, path)
    return path


def draw(scan: Scan, bounds: tuple[int, int, int, int], path: Path) -> Path:
    """Grab the window, box and number every target, and save it."""
    grab, draw_on, font_from = _pillow()
    whole = grab.grab(all_screens=True)
    shot = whole.crop(crop_box(bounds, _virtual_origin())).convert("RGB")
    canvas = draw_on.Draw(shot)
    font = _font(font_from)

    for target in scan.targets:
        colour = PALETTE[(target.number - 1) % len(PALETTE)]
        element = target.element
        left, top = place(element.left, element.top, bounds)
        right, bottom = place(element.right, element.bottom, bounds)
        canvas.rectangle((left, top, right, bottom), outline=colour, width=OUTLINE)

        caption = str(target.number)
        width = 9 * len(caption) + 6
        # Above the box where there is room, inside it where there is not, so a
        # control at the very top of the window still gets its number.
        badge_top = top - BADGE_HEIGHT if top - BADGE_HEIGHT >= 0 else top
        canvas.rectangle((left, badge_top, left + width, badge_top + BADGE_HEIGHT), fill=colour)
        canvas.text((left + 3, badge_top + 2), caption, fill=(255, 255, 255), font=font)

    path.parent.mkdir(parents=True, exist_ok=True)
    shot.save(path, "PNG", optimize=True)
    logger.info("Marked %d targets onto %s", len(scan.targets), path)
    return path


def _pillow():
    """ImageGrab, ImageDraw and ImageFont, or a plain explanation of their absence."""
    try:
        from PIL import ImageDraw, ImageFont, ImageGrab
    except ImportError as exc:
        raise MarksUnavailable(
            "Pillow is not installed, so no screenshot can be taken. Install it "
            "with `uv sync --extra screen`."
        ) from exc
    return ImageGrab, ImageDraw, ImageFont


def _shrink(shot, max_width: int):
    """Narrow a wide grab, since image tokens are charged by the pixel."""
    if max_width <= 0 or shot.width <= max_width:
        return shot
    height = round(shot.height * max_width / shot.width)
    return shot.resize((max_width, height))


def _font(module):
    try:
        return module.truetype("arialbd.ttf", 13)
    except OSError:
        return module.load_default()


def _virtual_origin() -> tuple[int, int]:
    """Top left of the virtual screen, which a grab of all monitors starts from."""
    import ctypes

    metrics = ctypes.windll.user32.GetSystemMetrics
    return metrics(76), metrics(77)  # SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN
