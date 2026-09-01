"""The desk, as tools a model can call.

Schemas in OpenAI's shape and dispatch straight into the Python that already
does the work - `screen.py` for looking, `hands.py` for acting, `subprocess` for
the shell. This is JARVIS calling its own hands, and a
protocol between the two would only be something to serialise through.

Results come back as lines of text rather than JSON. A scan of 200 targets is
the largest thing any of these returns and the difference is most of a thousand
tokens, which on a local model is a second of prompt processing per look.

A tool never raises. A missing tool result leaves the conversation unable to
continue, so a failure is a result too - the string says what went wrong and the
model gets to decide what to do about it.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from .screen import (
    Screen,
    ScreenUnavailable,
    means_the_same,
    offers_nothing_clickable,
    runs_as_admin,
)

logger = logging.getLogger("jarvis.tools")

# Combinations that shut something. Sent blind they go wherever the focus
# happens to be, and a live session pressed alt+f4 straight after scanning
# Chrome - which does not focus anything - and closed JARVIS's own console
# instead. The log ends mid line. These have to name the window they mean.
CLOSING = (
    frozenset({"alt", "f4"}),
    frozenset({"ctrl", "w"}),
    frozenset({"ctrl", "f4"}),
    frozenset({"ctrl", "shift", "w"}),
    frozenset({"ctrl", "q"}),
)

# How far back a repeat still counts. Long enough to see through the look that
# sits between two clicks, short enough that a button pressed once a minute for
# a good reason is not nagged about.
RECENT_CALLS = 8

# Past this a command wrote a file rather than an answer, and the tail is
# usually the part that matters - so the middle goes, not the end.
TRUNCATED = "\n... [{dropped} characters dropped from the middle] ...\n"


@dataclass(frozen=True)
class Tool:
    """One callable, with the schema that describes it to a model."""

    name: str
    description: str
    run: Callable[..., str]
    properties: dict[str, Any] = field(default_factory=dict)
    required: tuple[str, ...] = ()

    def spec(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.properties,
                    "required": list(self.required),
                },
            },
        }


class Toolbox:
    """Everything the brain can do, by name."""

    def __init__(self, tools: list[Tool], images: list[str] | None = None) -> None:
        self.tools = {tool.name: tool for tool in tools}
        # Pictures look_at_image has been asked to show, waiting for the brain to
        # put them in front of the model. A tool result is text and no endpoint
        # takes an image on a `tool` message, so they travel as the next user
        # message instead - see Brain._show_the_pictures.
        self.images = images if images is not None else []
        # The last refusal, cleared by anything that works. Repeating one word
        # for word is the signal that looking again is not going to help.
        self._refused = ""
        # The calls that worked, lately. A refusal repeating is already caught;
        # this is the other half - a click that succeeds every time and changes
        # nothing, which is what happens when the only target offered is not
        # the thing anybody wants pressed.
        self._recent: deque[str] = deque(maxlen=RECENT_CALLS)
        # Refusals, failures and loops, counted. What decides whether a turn had
        # anything to learn from: one that worked first time did not.
        self.stumbles = 0

    def specs(self) -> list[dict]:
        return [tool.spec() for tool in self.tools.values()]

    @property
    def names(self) -> list[str]:
        return list(self.tools)

    def run(self, name: str, arguments: dict) -> str:
        """Dispatch one call, turning every failure into a result.

        Both wrong-name and wrong-argument come back as prose the model can act
        on, because the alternative is a tool call with no reply to it.
        """
        tool = self.tools.get(name)
        if tool is None:
            offered = ", ".join(self.names) or "none"
            return f"There is no tool called {name!r}. The ones you have are: {offered}."
        try:
            result = tool.run(**arguments)
        except TypeError as exc:
            logger.warning("Bad arguments for %s(%r) - %s", name, arguments, exc)
            self.stumbles += 1
            return f"{name} was called wrongly - {exc}. Check the arguments and try again."
        except (ScreenUnavailable, ValueError) as exc:
            # A refusal is the normal answer to a stale number or an unknown key
            # name, so it reads as one rather than as a stack trace.
            logger.info("%s refused - %s", name, exc)
            self.stumbles += 1
            return self._refusal(f"Refused: {exc}")
        except Exception as exc:  # a tool failing must not end the conversation
            logger.exception("%s failed", name)
            self.stumbles += 1
            return f"{name} failed - {type(exc).__name__}: {exc}"
        self._refused = ""
        return self._going_round(f"{name}({arguments!r})", result)

    def _going_round(self, signature: str, result: str) -> str:
        """The same call a third time lately, with the screen no further on.

        A live session pressed "More actions for Casual" six times over two
        minutes. Each click worked, so nothing refused and nothing escalated; it
        opened the same little Edit/Delete menu each time, and the profile card
        the user actually wanted was not in the scan to be clicked. Succeeding
        at the wrong thing repeatedly is its own failure and needs its own line.
        """
        seen = self._recent.count(signature)
        self._recent.append(signature)
        if seen < 2:
            return result
        self.stumbles += 1
        return (
            f"{result}\n\nThat is the third time you have run this exact call. It works "
            "and it is not getting you anywhere, so what you want is not here - very "
            "likely it is not in the scan at all, and no amount of looking again will "
            "add it. Try the keyboard or a shell command instead, or say plainly what "
            "you cannot do. Do not run it a fourth time."
        )

    def _refusal(self, message: str) -> str:
        """A refusal, escalated when it is the same one twice.

        The first says to look again and use the new numbers. Followed exactly,
        that gives back the same numbers and the same refusal - a live session
        clicked "System" in a terminal, looked again, and clicked "System"
        again, then spent the rest of its budget going round. The second time it
        has to say something else.
        """
        if message != self._refused:
            self._refused = message
            return message
        return (
            f"{message}\n\nThat is word for word the last refusal, so looking again will "
            "not change it - the numbers will be the same numbers. Stop clicking this one. "
            "The keyboard reaches things the pointer cannot: type_text with no target goes "
            "wherever the caret is, and press_keys sends shortcuts. A shell command is often "
            "the shorter way round anything to do with a program running or not running. If "
            "none of those fit, say what you could not do rather than trying a third time."
        )


TOOLS_HEADER = """\
# What JARVIS can do

Generated from `tools.py` by `jarvis tools --write`, with every feature switched
on. A test fails if the two drift.

Nothing reads this file. The model is sent these descriptions as JSON schemas on
every single call, so there is nothing here for it to remember and nothing for it
to load - this is the same text, written out for a human. It exists because after
the system prompt these descriptions are the largest influence on what JARVIS
does, and reading them should not mean reading Python.

The wording is deliberate throughout. Each one says what the tool refuses and
why, because a model that knows the shape of a refusal asks for the right thing
the first time. To change a description, change `tools.py` and regenerate: the
prose and the signature live together on purpose, since prose that drifts from a
signature is believed over it.
"""


def as_markdown(box: Toolbox) -> str:
    """The toolbox as something readable, for the file under context/tools."""
    out = [TOOLS_HEADER]
    for tool in box.tools.values():
        out.append(f"## {tool.name}\n")
        out.append(tool.description + "\n")
        for name, spec in tool.properties.items():
            kind = spec.get("type", "string")
            if choices := spec.get("enum"):
                kind = " | ".join(str(choice) for choice in choices)
            needed = ", required" if name in tool.required else ""
            note = f" - {spec['description']}" if spec.get("description") else ""
            out.append(f"- `{name}` ({kind}{needed}){note}")
        out.append("" if tool.properties else "No arguments.\n")
    return "\n".join(out).rstrip() + "\n"


# ------------------------------------------------------------------- rendering


def render_scan(scan, label_chars: int = 80, others: list[str] | None = None) -> str:
    """One scan as the model sees it: numbers, roles and labels, never a pixel.

    The label is quoted because `expecting` is meant to be exactly it, and an
    unquoted column let a live session pass "Button      Maximise" - the role,
    the padding and the label read as one string. It was accepted, since the
    check is deliberately lenient, but a format that invites the mistake is one
    that will meet a stricter check some day.
    """
    lines = [f"{scan.window} - {len(scan.targets)} targets from {scan.considered} elements"]
    for target in scan.targets:
        label = target.element.label[:label_chars]
        where = f'  (after "{target.where}")' if target.where else ""
        lines.append(f'{target.number:>4}  {target.element.role:<11} "{label}"{where}')

    if scan.truncated:
        lines.append(
            f"{scan.truncated} more targets did not fit, so this list is an even spread "
            "across the window rather than all of it. Call look_at_screen again with "
            "matching= a word from the label to get every match."
        )
    if others:
        lines.append("Other windows open: " + ", ".join(others[:12]))
    return "\n".join(lines)


# ----------------------------------------------------------------- the toolbox


def build_toolbox(config: Config, screen: Screen | None = None, ears=None) -> Toolbox:
    """Assemble what this configuration allows.

    Looking is always here. Acting needs `screen.control`, and the shell needs
    `brain.shell` - a tool that is switched off is absent rather than present and
    refusing, because a model cannot be told not to reach for something it can
    see. `ears` is whatever owns the microphone, and only the voice path has one.
    """
    from . import hands

    desktop = screen or Screen(config.screen)
    label_chars = config.screen.label_chars
    tools: list[Tool] = []
    seeing: list[str] = []

    # No marked screenshot on this path, deliberately: a full screen grab is
    # half a second on every look. `jarvis look --marks` draws one to look at.
    def look_at_screen(window: str = "", matching: str = "") -> str:
        scan = desktop.look(window, matching)
        others = [title for _, title in desktop.windows() if title != scan.window]
        body = render_scan(scan, label_chars, others)
        if offers_nothing_clickable(scan.targets, desktop.backend.window_rect(scan.hwnd)):
            return (
                f"{body}\n\n{scan.window!r} exposes one element covering the whole window, "
                "so its accessibility tree never populated - the Start menu is like this. "
                "There is nothing here to click. Drive it with type_text (no target) and "
                "press_keys instead."
            )
        if not scan.targets and scan.considered:
            if runs_as_admin(scan.hwnd):
                return (
                    f"{body}\n\n{scan.window!r} runs as administrator and you do not, "
                    "so Windows will not show you what is in it or let you click or "
                    "type into it. Waiting will not change that. If it has to be "
                    "closed, run_command can - taskkill /F /IM name.exe - and they "
                    "will be asked to approve it. Otherwise say plainly that this is "
                    "a window you cannot touch."
                )
            return (
                f"{body}\n\nNothing can be acted on yet, which is what a window looks like "
                "while it is still building itself. Wait a moment and look again."
            )
        return body

    tools.append(
        Tool(
            name="look_at_screen",
            description=(
                "List everything on screen that can be clicked or typed into, numbered. "
                "With no arguments it reads the window in front; `window` picks another "
                "by any part of its title, and `matching` keeps only labels containing "
                "it, which is how you find one control in a crowded window. You get "
                "numbers and labels, never coordinates. Look again after anything you do."
            ),
            run=look_at_screen,
            properties={
                "window": {"type": "string", "description": "part of a window title"},
                "matching": {"type": "string", "description": "only labels containing this"},
            },
        )
    )

    tools += _eye_tools(config, desktop, seeing)

    if not config.screen.control:
        return Toolbox(tools + _extras(config, ears), images=seeing)

    def aim(target: int, expecting: str):
        """Resolve a number, having made the model say what it is aiming at."""
        found, scan = desktop.aim(target)
        if not means_the_same(expecting, found.element.label):
            raise ScreenUnavailable(
                f"Target {target} is {found.element.label!r}, not {expecting!r}. Nothing "
                "was pressed. Look at the screen again and read the numbers off the new "
                "list."
            )
        return found, scan

    def acted(what: str, found, scan) -> str:
        logger.info("%s target %d %r in %r", what, found.number, found.element.label, scan.window)
        return (
            f"{what} {found.element.label!r} in {scan.window}. That will have changed the "
            f"screen, so every number from this scan is now a guess - look again before the "
            "next one."
        )

    def focus_window(window: str, matching: str = "") -> str:
        scan = desktop.focus(window, matching)
        return render_scan(scan, label_chars)

    tools.append(
        Tool(
            name="focus_window",
            description=(
                "Raise a window, restoring it if it was minimised, then scan it. Input "
                "goes to whatever holds the foreground, so this is what to call when "
                "what you want is behind something else, and the only thing that gets "
                "at a minimised window."
            ),
            run=focus_window,
            properties={
                "window": {"type": "string", "description": "part of a window title"},
                "matching": {"type": "string", "description": "only labels containing this"},
            },
            required=("window",),
        )
    )

    def click(target: int, expecting: str, button: str = "left", clicks: int = 1) -> str:
        found, scan = aim(target, expecting)
        x, y = found.element.centre
        hands.click(
            x,
            y,
            button=button if button in {"left", "right"} else "left",
            count=2 if clicks == 2 else 1,
            settle=config.screen.click_settle_seconds,
        )
        return acted("double clicked" if clicks == 2 else f"{button} clicked", found, scan)

    tools.append(
        Tool(
            name="click",
            description=(
                "Click one of the numbers from look_at_screen. `expecting` is what is "
                "inside the quotation marks beside that number, not the role in front of "
                "them, and it is checked first: if the number now "
                "points at something else the click is refused rather than landing on "
                "it. This is the real pointer on the real desktop."
            ),
            run=click,
            properties={
                "target": {"type": "integer", "description": "a number from look_at_screen"},
                "expecting": {"type": "string", "description": "the label beside that number"},
                "button": {"type": "string", "enum": ["left", "right"]},
                "clicks": {"type": "integer", "enum": [1, 2]},
            },
            required=("target", "expecting"),
        )
    )

    def type_text(
        text: str,
        then: str = "leave_it",
        target: int = 0,
        expecting: str = "",
        clear_first: bool = False,
    ) -> str:
        if target and not expecting:
            return (
                f"target={target} was given without `expecting`. Pass the label beside "
                "that number, or leave `target` out to type wherever the caret already is."
            )
        where = "whatever had keyboard focus"
        found = scan = None
        if target:
            found, scan = aim(target, expecting)
            x, y = found.element.centre
            hands.click(x, y, settle=config.screen.click_settle_seconds)
        if clear_first:
            hands.press("ctrl+a")
        hands.type_text(text)
        if then == "press_enter":
            hands.press("enter")

        what = "typed and submitted" if then == "press_enter" else "typed"
        if found is not None:
            return acted(what, found, scan)
        logger.info("%s at the keyboard focus: %r", what.capitalize(), text)
        return (
            f"{what} {text!r} into {where} - nothing here can confirm where it went. Look "
            "at the screen to check it landed, and remember that whatever you opened to "
            'get here is still open: press_keys("escape") closes it.'
        )

    tools.append(
        Tool(
            name="type_text",
            description=(
                "Type text. `then` decides whether it is submitted: press_enter sends "
                "the message or runs the search, leave_it types and stops - a half "
                "written message sent early cannot be taken back.\n\n"
                "Name a `target` and it is clicked first to put the caret there, with "
                "`expecting` checked exactly as click does. Leave `target` out and the "
                "text goes wherever the keyboard focus already is, which is what you "
                "want for something that just opened with its caret ready - the Start "
                "menu, a dialog, a search bar that took focus on its own - and the only "
                "way into a window with nothing clickable in it. `clear_first` selects "
                "what is there so the text replaces it."
            ),
            run=type_text,
            properties={
                "text": {"type": "string"},
                "then": {"type": "string", "enum": ["press_enter", "leave_it"]},
                "target": {"type": "integer", "description": "optional, from look_at_screen"},
                "expecting": {"type": "string", "description": "required with target"},
                "clear_first": {"type": "boolean"},
            },
            required=("text", "then"),
        )
    )

    def scroll(target: int, expecting: str, direction: str = "down", notches: int = 3) -> str:
        found, scan = aim(target, expecting)
        x, y = found.element.centre
        turns = max(1, min(20, notches)) * (1 if direction == "up" else -1)
        hands.scroll(x, y, turns, settle=config.screen.click_settle_seconds)
        return acted(f"scrolled {direction}", found, scan)

    tools.append(
        Tool(
            name="scroll",
            description=(
                "Wheel over a target and scroll. Use it when what you want is not in the "
                "list because it is scrolled out of view - offscreen elements are left "
                "out of a scan rather than offered at coordinates nobody can click."
            ),
            run=scroll,
            properties={
                "target": {"type": "integer"},
                "expecting": {"type": "string"},
                "direction": {"type": "string", "enum": ["up", "down"]},
                "notches": {"type": "integer"},
            },
            required=("target", "expecting", "direction"),
        )
    )

    def press_keys(keys: str, window: str = "") -> str:
        if window:
            desktop.focus(window)
            front, title = desktop.backend.foreground()
            wanted, _ = desktop.find_window(window)
            if front != wanted:
                return (
                    f"Refused: {window!r} would not come to the front - {title!r} is there "
                    f"instead, and {keys} would have gone to that. Nothing was pressed."
                )
        elif closes_something(keys):
            return (
                f"Refused: {keys} shuts whatever is in front, and nothing here knows what "
                "that is - looking at a window does not focus it. Name the window: "
                f"press_keys(keys={keys!r}, window='part of its title'). Nothing was pressed."
            )
        hands.press(keys)
        logger.info("Pressed %s%s", keys, f" at {window!r}" if window else "")
        if window:
            return f"Pressed {keys} at {window!r}. Look at the screen to see what it did."
        return (
            f"Pressed {keys}, at whatever had focus. Look at the screen to see what it did. "
            'If it opened something it is still open - press_keys("escape") closes it.'
        )

    tools.append(
        Tool(
            name="press_keys",
            description=(
                "Press a combination like ctrl+s, alt+f4, escape or f5, at whatever holds "
                "the keyboard focus. An unknown key name is refused rather than half "
                "pressed.\n\n"
                "Two sets are worth reaching for before anything else, because neither "
                "needs a window, a scan or a target. The media keys - playpause, "
                "nexttrack, prevtrack, stop, volumeup, volumedown, mute - which Windows "
                "routes to whatever is playing. And the window keys: win+up maximises "
                "whatever is in front, win+down restores or minimises it, win+left and "
                "win+right put it against one side. That is how a window gets moved "
                "around; hunting for a title bar button is not.\n\n"
                "Pass `window` and it is focused first and checked before anything is "
                "pressed, which is the only safe way to send a combination to a "
                "particular window. Anything that closes one - alt+f4, ctrl+w - is "
                "refused without it."
            ),
            run=press_keys,
            properties={
                "keys": {"type": "string", "description": "e.g. ctrl+s, playpause"},
                "window": {
                    "type": "string",
                    "description": "part of the title of the window to send it to, focused first",
                },
            },
            required=("keys",),
        )
    )

    return Toolbox(tools + _extras(config, ears), images=seeing)


def _eye_tools(config: Config, desktop: Screen, seeing: list[str]) -> list[Tool]:
    """Taking a picture, and being shown one.

    Two tools rather than one because they are two different questions. A
    screenshot is cheap and often all that is wanted is the file - to open it, to
    keep it, to hand to somebody. Looking costs a couple of thousand tokens and
    should be asked for on purpose.
    """
    if not config.brain.images:
        return []

    def screenshot(window: str = "") -> str:
        from . import marks

        target = config.log_dir / (config.screen.screenshot_file or "screen.png")
        bounds, where = None, "every monitor"
        if window:
            hwnd, where = desktop.find_window(window)
            bounds = desktop.backend.window_rect(hwnd)
        path = marks.capture(bounds, target, config.screen.screenshot_max_width)
        return (
            f"Saved a picture of {where} to {path}. Nothing has looked at it yet - "
            f'call look_at_image(path="{path}") to see what is in it.'
        )

    def look_at_image(path: str) -> str:
        found, encoded, size = read_image(path, config.screen.screenshot_max_width)
        seeing.append(encoded)
        return (
            f"{found.name} is in front of you now, {size[0]} by {size[1]}. It arrives with "
            "the next message rather than in this result, so say what is in it after you "
            "have seen it, not before."
        )

    return [
        Tool(
            name="screenshot",
            description=(
                "Take a picture of the screen and save it. Returns where it went; it does "
                "not show it to you - look_at_image does that.\n\n"
                "With no window it is every monitor, which is what you want for 'what is "
                "on screen'. Name a window for that window alone. This is the tool for "
                "anything that has to be seen rather than pressed: a chart, an error "
                "dialog, a photograph, a page that scans as nothing. For pressing "
                "something, look_at_screen and its numbers are surer than any picture."
            ),
            run=screenshot,
            properties={
                "window": {
                    "type": "string",
                    "description": "part of a window title, or leave it out for the whole desk",
                }
            },
        ),
        Tool(
            name="look_at_image",
            description=(
                "Look at an image file. Any picture on this machine - one screenshot just "
                "saved, or something that was already there.\n\n"
                "It is attached to the next message rather than returned here, so this "
                "call tells you it is coming and the one after is where you can describe "
                "it. Costs a couple of thousand tokens, so ask when the answer is "
                "genuinely in the picture."
            ),
            run=look_at_image,
            properties={"path": {"type": "string", "description": "the image file to look at"}},
            required=("path",),
        ),
    ]


IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})


def read_image(named: str, max_width: int = 0) -> tuple[Path, str, tuple[int, int]]:
    """One image file as a data URL, shrunk to something worth sending.

    PNG whatever went in, because the usual subject is a screenshot and what is
    being read off it is text - and JPEG artefacts on eight point type are the
    difference between reading a filename and guessing at it.
    """
    path = under_root(named)
    if not path.is_file():
        raise ScreenUnavailable(f"There is no file at {path}.")
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        offered = ", ".join(sorted(IMAGE_SUFFIXES))
        raise ScreenUnavailable(f"{path.name} is not an image. Readable: {offered}.")

    import base64
    import io

    from PIL import Image

    with Image.open(path) as opened:
        picture = opened.convert("RGB")
        if 0 < max_width < picture.width:
            height = round(picture.height * max_width / picture.width)
            picture = picture.resize((max_width, height), Image.LANCZOS)
        buffer = io.BytesIO()
        picture.save(buffer, "PNG", optimize=True)
        size = (picture.width, picture.height)

    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    logger.info("Read %s at %dx%d, %dKB encoded", path.name, *size, len(encoded) // 1024)
    return path, f"data:image/png;base64,{encoded}", size


def _extras(config: Config, ears=None) -> list[Tool]:
    """The tools that are not about the desk at all."""
    return (
        _ear_tools(config, ears) + _web_tools(config) + _memory_tools(config) + _shell_tools(config)
    )


def _web_tools(config: Config) -> list[Tool]:
    """Searching and reading, if anything is allowed off the machine."""
    if not config.brain.web:
        return []
    from . import web

    def search_web(query: str) -> str:
        return web.search(query, config.brain.search_results, config.brain.search_url)

    def read_page(url: str) -> str:
        return web.read(url, config.brain.page_chars)

    return [
        Tool(
            name="search_web",
            description=(
                "Search the web and get back a few results: a title, the site, a sentence "
                "of each and the address. For anything you cannot know from here - the "
                "news, a score, an opening time, a fact you are not sure of.\n\n"
                "The snippets often answer the question on their own, and then you just "
                "say the answer. Open one with read_page only when they do not, because a "
                "page is slow and long. Say where an answer came from when it is the sort "
                "of thing that could be wrong, and never repeat a result you did not get."
            ),
            run=search_web,
            properties={"query": {"type": "string", "description": "what to search for"}},
            required=("query",),
        ),
        Tool(
            name="read_page",
            description=(
                "Fetch one web page and return its text, markup stripped and cut short. "
                "Use it on an address from search_web when the snippet was not enough.\n\n"
                "It reads what the server sends, so a page that builds itself in the "
                "browser comes back empty - not something to retry, something to say. And "
                "they are listening: summarise it in a sentence, never read it out."
            ),
            run=read_page,
            properties={"url": {"type": "string", "description": "the address to read"}},
            required=("url",),
        ),
    ]


def _ear_tools(config: Config, ears) -> list[Tool]:
    """Stopping the microphone, if there is one to stop.

    Absent in code mode, where there are no ears to close. `hasattr` rather than
    a flag because that is the only difference between the two front ends.

    Stopping only. There was a `resume_transcription` beside this and it went:
    the microphone it would open is the one that would have had to hear the
    request, so the tool was reachable exactly when it was not needed, and what
    it actually produced was JARVIS calling it on a hunch and announcing it was
    back from a state it had never been in. The key is the way back.
    """
    if ears is None or not hasattr(ears, "pause"):
        return []
    key = config.service.hotkey or "the configured key"

    def pause_transcription() -> str:
        if not ears.pause():
            # Said at length because the short version read as "you are deaf",
            # and JARVIS told somebody it could not hear them while answering
            # every word they said.
            return (
                "The desk microphone was already shut. Anything you can hear right now "
                "is reaching you some other way - the web app on a phone - so do not "
                "tell them you have gone deaf, because you plainly have not."
            )
        return (
            "Stopped listening at the desk. That microphone is no longer being read, so "
            f"nothing said in that room is transcribed, logged or recoverable. The {key} "
            "key is the only way back and you have no tool for it: say so now, because "
            "from here you cannot hear them ask."
        )

    return [
        Tool(
            name="pause_transcription",
            description=(
                "Shut the microphone on the desk. It stops being read, so nothing "
                "said in that room is transcribed, logged or recorded until it is "
                "resumed - not merely withheld from you.\n\n"
                "The desk only. A phone using the web app has its own control and "
                "keeps hearing, which is the point: somebody can leave the room "
                "muted and still talk to you from the next one.\n\n"
                "For when they ask for privacy, or say they are on a call, or are about "
                "to have a conversation that is not with you.\n\n"
                "Call it FIRST and say so afterwards. A reply that promises to stop "
                "listening is a promise where the act should be - "
                f"and once it is done, say that the {key} key brings you back, because "
                "from then on you cannot hear them ask. There is no tool for starting "
                "again, on purpose: it would be a tool the shut microphone had to hear "
                "you asked for. Not a way to avoid answering something either: a "
                "hyphen does that and keeps your ears."
            ),
            run=pause_transcription,
        ),
    ]


def _memory_tools(config: Config) -> list[Tool]:
    """Writing a lesson down, if there is a list to write it to."""
    if not config.brain.memories:
        return []
    from . import memories

    path = session_file(config)

    def remember(heading: str, lesson: str) -> str:
        return memories.remember(path, heading, lesson, config.brain.max_memory_chars)

    return [
        Tool(
            name="remember",
            description=(
                "Write down one thing worth still knowing next month, so you have it "
                "next time. Your whole list is read back to you the next time they "
                "speak.\n\n"
                "How the desk behaves, and most of that is only discoverable by getting "
                "it wrong: a window whose tree is empty until it has been focused, an "
                "application that takes a moment to build itself, which of four "
                "identically labelled buttons is the one that works, a command that "
                "turned out to be the way to do something. When a tool refuses you and "
                "you work out why, that is exactly what this is for.\n\n"
                "And who you are talking to: what they do, what they are working on, what "
                "they own, what they enjoy, how they like things done. Only what they "
                "actually said - what somebody asks for is not a fact about them, and "
                "they can open this file and read it.\n\n"
                "`heading` is which group it belongs under - Navigation, Applications, "
                "Preferences, Work, Personal, or any other. Your headings are in your "
                "prompt with everything under them: reuse one that fits rather than "
                "making a second heading for the same kind of thing.\n\n"
                "Not for anything about one conversation - not what they asked for today, "
                "not what you replied, not what was on screen at the time. One sentence, "
                "and specific enough to act on months from now: a number from a scan will "
                "be wrong by then, a label or a window name will not."
            ),
            run=remember,
            properties={
                "heading": {"type": "string", "description": "which group it goes under"},
                "lesson": {"type": "string", "description": "one sentence"},
            },
            required=("heading", "lesson"),
        )
    ]


def memory_file(config: Config) -> Path:
    """The file a session's notes are folded into. Relative names sit under the
    project root."""
    return under_root(config.brain.memories_file)


def session_file(config: Config) -> Path:
    """The file remember() writes to now, which is the one at the end of the
    prompt. Falls back to the stable file when it is not configured.
    """
    named = config.brain.session_memories_file
    return under_root(named) if named else memory_file(config)


def under_root(named: str) -> Path:
    from .config import under_root as resolve

    return resolve(named)


def _shell_tools(config: Config) -> list[Tool]:
    """The shell, if it is switched on. This is how a coding agent gets run."""
    if not config.brain.shell:
        return []
    # Named in config or not at all. A hardcoded command in a tool description is
    # a claim about the machine that nothing checks, and a voice assistant
    # confidently running something that is not installed is worse than one that
    # says coding is not its job.
    handoff = (
        "Anything that is really a coding job belongs to a coding agent rather than to "
        f'you - run `{config.brain.coding_agent} "the whole request in one sentence"` and '
        "it works the repository itself, then report what it said. Do not try to edit "
        "source files a line at a time through this."
        if config.brain.coding_agent
        else (
            "Editing source files a line at a time through this is not your job and not "
            "something you are any good at. Say so and leave it to whoever asked."
        )
    )

    def run_command(command: str) -> str:
        return shell(
            command,
            timeout=config.brain.shell_timeout_seconds,
            limit=config.brain.shell_output_chars,
        )

    return [
        Tool(
            name="run_command",
            description=(
                "Run a PowerShell command and return its output. This is everything the "
                "desktop tools are not: files, git, winget, curl, any program on PATH.\n\n"
                "REACH FOR THIS FIRST for anything with a text answer, and for files "
                "above all. Finding, listing, reading, copying, renaming and deleting are "
                "one call here - Get-ChildItem, Test-Path, Get-Content, Move-Item - "
                "against several minutes of clicking through File Explorer, which redraws "
                "under you and then refuses the numbers you were given. It also reaches "
                "folders nobody has open. The same goes for what is installed, what is "
                "running and how much disk is left. The pointer is for applications that "
                "only exist as a window.\n\n" + handoff + "\n\n"
                "It waits for the command to finish, so nothing interactive: no prompts, "
                "no pagers, no servers held in the foreground. Anything that changes the "
                "machine, report it once it is done and never in advance."
            ),
            run=run_command,
            properties={"command": {"type": "string"}},
            required=("command",),
        )
    ]


def shell(command: str, timeout: float = 60.0, limit: int = 2000) -> str:
    """Run one PowerShell command and report what happened.

    Non-interactive and profile-free, so it behaves the same however the user's
    own shell is set up, and a command that waits for input hits the timeout
    instead of hanging forever.
    """
    logger.info("run_command: %s", command)
    try:
        done = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True,
            text=True,
            timeout=timeout,
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return f"Timed out after {timeout:g}s and was killed. It may have been waiting for input."
    except OSError as exc:
        return f"Could not run it - {exc}"

    body = (done.stdout or "") + (("\n" + done.stderr) if done.stderr else "")
    body = clip(body.strip(), limit) or "(no output)"
    if done.returncode:
        return f"Exit code {done.returncode}.\n{body}"
    return body


def closes_something(keys: str) -> bool:
    """Whether this combination shuts a window, however it was spelled."""
    pressed = frozenset(part.strip().lower() for part in keys.replace("-", "+").split("+"))
    return any(combination == pressed for combination in CLOSING)


def clip(text: str, limit: int) -> str:
    """Cut the middle out of something too long, keeping both ends.

    The head says what ran and the tail carries the error, so taking a prefix
    loses the half that mattered.
    """
    if limit <= 0 or len(text) <= limit:
        return text
    keep = limit // 2
    dropped = len(text) - keep * 2
    return text[:keep] + TRUNCATED.format(dropped=dropped) + text[-keep:]


def parse_arguments(raw: Any) -> tuple[dict, str]:
    """A model's tool arguments, and why they could not be read.

    Local models emit the arguments as a JSON string, occasionally a malformed
    one. Both come back here as (arguments, complaint) so the caller can answer
    the call either way rather than dropping it.
    """
    if isinstance(raw, dict):
        return raw, ""
    if not raw:
        return {}, ""
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        return {}, f"the arguments were not valid JSON ({exc})"
    if not isinstance(parsed, dict):
        return {}, f"the arguments came back as {type(parsed).__name__}, not an object"
    return parsed, ""
