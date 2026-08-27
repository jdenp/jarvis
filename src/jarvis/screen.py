"""What is on screen, as a short numbered list an agent can point at.

The accessibility tree is unusable as a prompt. One Teams window measured here
is 810 nodes, nearly all of them panes, groups and static text, and a model
handed the lot picks something plausible and wrong. Everything below cuts that
to the few dozen things you can actually act on - 810 to 54 on the same window -
numbers them in reading order, and keeps the coordinates on this side. The agent
names a number. It never sees a pixel and never does the arithmetic.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass

logger = logging.getLogger("jarvis.screen")


class ScreenUnavailable(RuntimeError):
    """UI Automation is out of reach, or the scan no longer describes the screen."""


# Control types worth offering. Everything absent - Pane, Group, Text, Image,
# TitleBar, ScrollBar, Separator - is scenery, and scenery is most of the tree.
CLICKABLE = frozenset(
    {
        "Button",
        "CheckBox",
        "ComboBox",
        "Custom",
        "DataItem",
        "Document",
        "Edit",
        "Hyperlink",
        "ListItem",
        "MenuItem",
        "RadioButton",
        "Slider",
        "SplitButton",
        "Spinner",
        "Tab",
        "TabItem",
        "TreeItem",
    }
)

# An unnamed button is noise. An unnamed text box is still somewhere to type.
TEXT_ENTRY = frozenset({"ComboBox", "Document", "Edit"})

# Past this the wrapper pass costs more than it saves. Real windows land near
# 50 once the cheap filters have run, so this is a runaway guard, not a limit.
WRAPPER_PASS_LIMIT = 600


@dataclass(frozen=True)
class Element:
    """One accessibility node, flattened to plain data.

    Deliberately not a COM object. Everything from here down is decided without
    a desktop, which is the only reason any of it can be tested.
    """

    name: str
    role: str
    left: int
    top: int
    width: int
    height: int
    enabled: bool = True
    offscreen: bool = False
    focusable: bool = False
    automation_id: str = ""
    runtime_id: tuple[int, ...] = ()

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height

    @property
    def area(self) -> int:
        return self.width * self.height

    @property
    def centre(self) -> tuple[int, int]:
        return self.left + self.width // 2, self.top + self.height // 2

    @property
    def label(self) -> str:
        return self.name.strip() or self.automation_id or self.role

    def contains(self, other: Element) -> bool:
        """Whether this rectangle wholly encloses a smaller one."""
        return (
            self.left <= other.left
            and self.top <= other.top
            and self.right >= other.right
            and self.bottom >= other.bottom
            and self.area > other.area
        )


@dataclass(frozen=True)
class Target:
    """An element with the number the agent will call it by."""

    number: int
    element: Element
    where: str = ""

    def as_dict(self, label_chars: int = 80) -> dict:
        """What the agent sees. No coordinates - they are not its to reason about."""
        element = self.element
        described = {
            "id": self.number,
            "label": element.label[:label_chars],
            "role": element.role,
        }
        if self.where:
            described["where"] = self.where
        if element.role in TEXT_ENTRY:
            described["accepts_text"] = True
        return described


@dataclass(frozen=True)
class Scan:
    """One look at one window, and the map from numbers back to pixels."""

    id: int
    window: str
    hwnd: int
    targets: tuple[Target, ...]
    considered: int
    truncated: int
    taken_at: float
    matching: str = ""

    def find(self, number: int) -> Target | None:
        return next((t for t in self.targets if t.number == number), None)

    def age(self, now: float | None = None) -> float:
        return (time.monotonic() if now is None else now) - self.taken_at

    def as_json(self) -> dict:
        """The whole scan, coordinates included, for another process to reload.

        `saved_at` is wall clock where `taken_at` is monotonic. Monotonic clocks
        do not survive a process boundary, and the age is the whole point of
        writing it down.
        """
        return {
            "id": self.id,
            "window": self.window,
            "hwnd": self.hwnd,
            "considered": self.considered,
            "truncated": self.truncated,
            "matching": self.matching,
            "saved_at": time.time(),
            "targets": [
                {"number": target.number, "where": target.where, **asdict(target.element)}
                for target in self.targets
            ],
        }

    @classmethod
    def from_json(cls, data: dict) -> Scan:
        """Reload a scan another process wrote, keeping its real age."""
        elapsed = max(0.0, time.time() - float(data.get("saved_at") or 0))
        targets = []
        for item in data.get("targets", []):
            fields = {key: value for key, value in item.items() if key not in {"number", "where"}}
            fields["runtime_id"] = tuple(fields.get("runtime_id") or ())
            targets.append(Target(item["number"], Element(**fields), item.get("where", "")))
        return cls(
            id=int(data.get("id", 0)),
            window=str(data.get("window", "")),
            hwnd=int(data.get("hwnd", 0)),
            targets=tuple(targets),
            considered=int(data.get("considered", 0)),
            truncated=int(data.get("truncated", 0)),
            taken_at=time.monotonic() - elapsed,
            matching=str(data.get("matching", "")),
        )

    def as_dict(self, label_chars: int = 80) -> dict:
        described: dict = {
            "scan": self.id,
            "window": self.window,
            "targets": [target.as_dict(label_chars) for target in self.targets],
            "considered": self.considered,
        }
        if self.matching:
            described["matching"] = self.matching
        if self.truncated:
            described["not_shown"] = self.truncated
        return described


def select(
    elements: Iterable[Element],
    *,
    limit: int = 60,
    min_pixels: int = 6,
    row_height: int = 24,
    matching: str = "",
    bounds: tuple[int, int, int, int] | None = None,
) -> tuple[tuple[Target, ...], int]:
    """Cut a whole accessibility tree down to what can be pointed at.

    `matching` keeps only the labels containing it, which is how a dense window
    stays under the limit - Outlook in a browser has 142 real targets and no
    ordering of them puts the one you want in the first sixty.

    Returns the numbered targets and how many were left off for the limit.
    """
    survivors = [element for element in elements if _worth_offering(element, min_pixels)]
    survivors = _drop_wrappers(survivors)
    survivors = _drop_duplicates(survivors)
    # Reading order, banded so two controls on the same row are not ordered by a
    # two pixel difference in where their tops happen to sit.
    survivors.sort(key=lambda element: (element.top // max(1, row_height), element.left))

    # Placed before the filter, not after. A search for "close" leaves four
    # buttons and none of their neighbours, and "bottom right" three times over
    # tells you nothing - the tab each one belongs to does.
    placed = _place_the_ambiguous(survivors, bounds)
    if matching:
        wanted = matching.casefold()
        placed = [target for target in placed if wanted in target.element.label.casefold()]

    truncated = max(0, len(placed) - limit)
    return tuple(
        Target(number, target.element, target.where)
        for number, target in enumerate(_thin(placed, limit), start=1)
    ), truncated


def _thin(targets: list[Target], limit: int) -> list[Target]:
    """Cut an over-long list down by spreading the cut, not by lopping the end.

    Taking the first N was silently catastrophic. The list is in reading order,
    so the tail is the bottom of the window - and on a media player that is
    exactly where the transport controls live. Asked to press play in Spotify,
    166 targets became the top 60 and the play button was not among them, so the
    request was impossible rather than merely hard, and nothing said so.

    An even spread degrades instead: whatever is wanted has a chance of being
    there, and every region of the window is represented. The result says it is
    a sample, and `matching` returns the complete set for a search term.
    """
    if len(targets) <= limit or limit <= 0:
        return targets
    step = len(targets) / limit
    return [targets[int(index * step)] for index in range(limit)]


def _place_the_ambiguous(
    elements: list[Element], bounds: tuple[int, int, int, int] | None
) -> list[Target]:
    """Number the targets, saying where the ones sharing a label are.

    A browser offers four buttons called Close and nothing to tell them apart,
    so a model picks one and it is the wrong one three times in four. Only the
    repeats get placed; on anything unique it would be noise.
    """
    counts: dict[str, int] = {}
    for element in elements:
        counts[element.label] = counts.get(element.label, 0) + 1

    span = bounds or _bounds(elements)
    return [
        Target(
            index + 1,
            element,
            where=_where(index, elements, span) if counts[element.label] > 1 else "",
        )
        for index, element in enumerate(elements)
    ]


def _bounds(elements: list[Element]) -> tuple[int, int, int, int]:
    """The rectangle the targets occupy, when the real window rect is not to hand."""
    if not elements:
        return 0, 0, 0, 0
    return (
        min(element.left for element in elements),
        min(element.top for element in elements),
        max(element.right for element in elements),
        max(element.bottom for element in elements),
    )


def _where(index: int, elements: list[Element], span: tuple[int, int, int, int]) -> str:
    """What a repeated label sits next to, or failing that which ninth it is in.

    A row of tab close buttons is all in the same ninth of the window, so the
    coarse position settles nothing. What does is the thing before it in reading
    order - which is how anyone reads a tab strip in the first place.
    """
    element = elements[index]
    neighbour = next(
        (
            other
            for other in reversed(elements[:index])
            if other.label != element.label
            and other.top < element.bottom
            and element.top < other.bottom
        ),
        None,
    )
    if neighbour is not None:
        return f"after {neighbour.label[:40]}"

    left, top, right, bottom = span
    width, height = max(1, right - left), max(1, bottom - top)
    x, y = element.centre
    column = min(2, max(0, int((x - left) * 3 // width)))
    row = min(2, max(0, int((y - top) * 3 // height)))
    vertical = ("top", "middle", "bottom")[row]
    horizontal = ("left", "centre", "right")[column]
    return vertical if horizontal == "centre" else f"{vertical} {horizontal}"


def confirms(target: Target, chain: Sequence[Element]) -> bool:
    """Whether what is under the target's centre now is still the target.

    `chain` is the deepest element at that point followed by its ancestors. The
    centre of a button lands on the label inside it as often as on the button,
    so the match is looked for up the chain rather than only at the hit.

    A runtime id settles it when it matches, but runtime ids do not survive a
    control being rebuilt - which virtualised lists do constantly - so the same
    name and role in the same place is accepted as the same thing.
    """
    wanted = target.element
    for element in chain:
        if wanted.runtime_id and element.runtime_id == wanted.runtime_id:
            return True
        if element.role == wanted.role and element.name and element.name == wanted.name:
            return True
    return False


def offers_nothing_clickable(targets, bounds: tuple[int, int, int, int]) -> bool:
    """Whether a scan found a window that exposes no real controls.

    A UWP or Electron surface that has not activated its accessibility tree
    reports one element: itself. That comes back as a single target whose
    rectangle is the whole window, so its centre is not a control - it is the
    middle of the panel, and whatever happens to be drawn there is what a click
    would hit. The point check then refuses, correctly and forever.

    Measured on the Start menu, which is one element exactly, labelled "Search
    box". Three type_text calls were refused against it in a row while the agent
    kept rescanning, because the scan looked like a window with one button in it.
    """
    if len(targets) != 1:
        return False
    left, top, right, bottom = bounds
    window = max(1, (right - left) * (bottom - top))
    return targets[0].element.area * 100 >= window * 90


def means_the_same(claimed: str, actual: str) -> bool:
    """Whether what an agent said it was aiming at is what it is aiming at.

    Loose on purpose. Labels are truncated before they reach the prompt and a
    model shortens a long one further, so either containing the other counts.
    Strict equality would refuse correct calls, and the mistake being caught is
    not a subtle one - it is Delete where Reply was meant.

    Under three characters only an exact match will do, or "a" would confirm
    every button on the screen.
    """
    wanted = " ".join(claimed.split()).casefold()
    real = " ".join(actual.split()).casefold()
    if not wanted or not real:
        return False
    if len(wanted) < 3:
        return wanted == real
    return wanted in real or real in wanted


def _worth_offering(element: Element, min_pixels: int) -> bool:
    if element.offscreen or not element.enabled:
        return False
    if element.width < min_pixels or element.height < min_pixels:
        return False
    if element.role not in CLICKABLE:
        return False
    return bool(element.name.strip()) or element.role in TEXT_ENTRY


def _drop_wrappers(elements: list[Element], *, encloses: int = 2) -> list[Element]:
    """Keep the leaves, drop the containers.

    A tree item holding nine chats has the same rectangle as all nine of them
    together, and clicking it does nothing anyone wanted. Anything wholly
    enclosing two or more of its neighbours is one of those. Two rather than
    one, so a button that happens to enclose its own label survives.
    """
    if len(elements) > WRAPPER_PASS_LIMIT:
        logger.debug("Skipping the wrapper pass, %d elements survived the filters", len(elements))
        return elements
    return [
        element
        for element in elements
        if sum(1 for other in elements if element.contains(other)) < encloses
    ]


def _drop_duplicates(elements: list[Element], *, slack: int = 6) -> list[Element]:
    """Same name, same role, same corner - keep the smaller of the two.

    Two boxes with different numbers over one control is worse than one.
    """
    kept: list[Element] = []
    for element in sorted(elements, key=lambda e: e.area):
        if any(_same_thing(element, other, slack) for other in kept):
            continue
        kept.append(element)
    return kept


def _same_thing(a: Element, b: Element, slack: int) -> bool:
    return (
        a.role == b.role
        and a.name == b.name
        and abs(a.left - b.left) <= slack
        and abs(a.top - b.top) <= slack
    )


class Screen:
    """The current scan, and the only place a target number becomes a pixel.

    Refusing is most of the job. A number from a scan taken before the window
    scrolled still resolves to a perfectly good coordinate, and clicking it hits
    whatever is there now - which is how automation ends up pressing delete on
    the wrong row. Every number is checked against what is under it before
    anything is pressed.
    """

    def __init__(self, config, backend=None) -> None:
        self.config = config
        self._backend = backend
        self._scan: Scan | None = None
        self._next_id = 1

    @property
    def backend(self):
        """The UI Automation backend, imported on first use so the CLI stays fast."""
        if self._backend is None:
            from .uia import UiaBackend

            self._backend = UiaBackend()
        return self._backend

    @property
    def latest(self) -> Scan | None:
        return self._scan

    def windows(self) -> list[tuple[int, str]]:
        return self.backend.windows()

    def focus(self, window: str, matching: str = "") -> Scan:
        """Bring a window to the front, then look at it.

        The only tool here with a side effect, and it needs one: input goes to
        whatever has the foreground, not to whatever was scanned.
        """
        hwnd, title = self.find_window(window)
        if not self.backend.activate(hwnd):
            logger.warning("Could not bring %r to the front.", title)
        # Restoring is animated, and an element scanned mid animation reports
        # where it was rather than where it lands.
        time.sleep(self.config.focus_settle_seconds)
        return self.look(window, matching)

    def look(self, window: str = "", matching: str = "") -> Scan:
        """Scan one window and number what can be acted on."""
        hwnd, title = self.find_window(window)
        if self.backend.minimised(hwnd):
            raise ScreenUnavailable(
                f"{title!r} is minimised. Its accessibility tree still reads as though "
                "it were on screen, so every coordinate in it points at whatever is "
                "actually there instead. Bring it to the front first."
            )
        elements = self.backend.elements(hwnd)
        targets, truncated = select(
            elements,
            limit=self.config.max_targets,
            min_pixels=self.config.min_target_pixels,
            matching=matching,
            bounds=self.backend.window_rect(hwnd),
        )
        self._scan = Scan(
            id=self._next_id,
            window=title,
            hwnd=hwnd,
            targets=targets,
            considered=len(elements),
            truncated=truncated,
            taken_at=time.monotonic(),
            matching=matching,
        )
        self._next_id += 1
        logger.info(
            "Scan %d of %r: %d elements down to %d targets%s",
            self._scan.id,
            title,
            len(elements),
            len(targets),
            f", {truncated} over the limit" if truncated else "",
        )
        return self._scan

    def aim(self, number: int) -> tuple[Target, Scan]:
        """Resolve a target number, or say why it cannot be trusted."""
        scan = self._scan
        if scan is None:
            raise ScreenUnavailable(
                "Nothing has been scanned yet, so there are no numbers to use. "
                "Look at the screen first."
            )

        target = scan.find(number)
        if target is None:
            raise ScreenUnavailable(
                f"There is no target {number}. Scan {scan.id} of {scan.window!r} numbered "
                f"1 to {len(scan.targets)}. Look again if the screen has moved on."
            )

        age = scan.age()
        if self.config.max_scan_age_seconds > 0 and age > self.config.max_scan_age_seconds:
            raise ScreenUnavailable(
                f"Scan {scan.id} is {int(age)}s old and has expired. The screen has had "
                "time to change since, so look again before acting on it."
            )

        # Check before raising anything. A target already under the pointer
        # needs no help, and raising costs something: the taskbar is always on
        # top and SetForegroundWindow refuses it, so the attempt logged a warning
        # and then the check failed against a window that had been perfectly
        # clickable a moment earlier. Only reach for the foreground when the
        # point says the target is not visible.
        x, y = target.element.centre
        if confirms(target, self.backend.chain_at(x, y)):
            return target, scan

        # Not there. Either something covers it or the window is behind - and
        # input goes to the foreground regardless of what was scanned, so this is
        # both the diagnosis and the fix.
        if self.backend.foreground()[0] != scan.hwnd:
            if not self.backend.activate(scan.hwnd):
                logger.warning("Could not bring %r to the front.", scan.window)
            time.sleep(self.config.focus_settle_seconds)

        if not confirms(target, self.backend.chain_at(x, y)):
            raise ScreenUnavailable(
                f"Target {number} was {target.element.label!r}, but something else is "
                "there now - the window has moved, scrolled or redrawn, or something is "
                "covering it. Look again and use the new numbers. Nothing was pressed."
            )
        return target, scan

    def remember(self, path) -> None:
        """Write the current scan out, so `jarvis click` can act on it.

        The numbers a human read off `jarvis look` are worthless to the next
        process without this, and a scan stale enough to matter is still caught
        by aim() - the file is a shortcut, not a second source of truth.
        """
        import json

        if self._scan is None:
            raise ScreenUnavailable("Nothing has been scanned, so there is nothing to save.")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self._scan.as_json(), indent=2), encoding="utf-8")

    def recall(self, path) -> Scan:
        """Load the scan a previous look wrote, and work from it."""
        import json

        try:
            self._scan = Scan.from_json(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError) as exc:
            raise ScreenUnavailable(
                f"No usable scan at {path} - run `jarvis look` first ({exc})."
            ) from exc
        self._next_id = self._scan.id + 1
        return self._scan

    def find_window(self, window: str) -> tuple[int, str]:
        """The window a name refers to, or a refusal listing what is open."""
        if not window:
            hwnd, title = self.backend.foreground()
            if not hwnd:
                raise ScreenUnavailable("No window is in the foreground.")
            return hwnd, title or "(untitled)"

        open_now = self.backend.windows()
        wanted = window.casefold()
        # Exact first. "Taskbar" should not land on "Taskbar (second screen)"
        # because that one happened to come back higher in the z order.
        for match in (lambda title: wanted == title, lambda title: wanted in title):
            for hwnd, title in open_now:
                if match(title.casefold()):
                    return hwnd, title
        listed = ", ".join(repr(title) for _, title in open_now) or "nothing"
        raise ScreenUnavailable(f"No open window matches {window!r}. Open right now: {listed}")
