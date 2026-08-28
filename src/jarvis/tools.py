"""The desk, as tools a model can call.

Schemas in OpenAI's shape and dispatch straight into the Python that already
does the work - `screen.py` for looking, `hands.py` for acting, `subprocess` for
the shell. No MCP anywhere in here: this is JARVIS calling its own hands, and a
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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from .screen import Screen, ScreenUnavailable, means_the_same, offers_nothing_clickable

logger = logging.getLogger("jarvis.tools")

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

    def __init__(self, tools: list[Tool]) -> None:
        self.tools = {tool.name: tool for tool in tools}
        # The last refusal, cleared by anything that works. Repeating one word
        # for word is the signal that looking again is not going to help.
        self._refused = ""

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
            return f"{name} was called wrongly - {exc}. Check the arguments and try again."
        except (ScreenUnavailable, ValueError) as exc:
            # A refusal is the normal answer to a stale number or an unknown key
            # name, so it reads as one rather than as a stack trace.
            logger.info("%s refused - %s", name, exc)
            return self._refusal(f"Refused: {exc}")
        except Exception as exc:  # a tool failing must not end the conversation
            logger.exception("%s failed", name)
            return f"{name} failed - {type(exc).__name__}: {exc}"
        self._refused = ""
        return result

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

    # No marked screenshot on this path, deliberately. It exists for `send_image`
    # and for a human to look at, the brain never sends an image, and a full
    # screen grab is half a second on every look. `jarvis look --marks` draws one.
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

    if not config.screen.control:
        return Toolbox(tools + _extras(config, ears))

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

    def press_keys(keys: str) -> str:
        hands.press(keys)
        logger.info("Pressed %s", keys)
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
                "around; hunting for a title bar button is not."
            ),
            run=press_keys,
            properties={"keys": {"type": "string", "description": "e.g. ctrl+s, playpause"}},
            required=("keys",),
        )
    )

    return Toolbox(tools + _extras(config, ears))


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
    """Stopping and starting the microphone, if there is one to stop.

    Absent in chat mode, where there are no ears to close. `hasattr` rather than
    a flag because that is the only difference between the two front ends.
    """
    if ears is None or not hasattr(ears, "pause"):
        return []
    key = config.service.hotkey or "the configured key"

    def pause_transcription() -> str:
        if not ears.pause():
            return "Already not listening."
        return (
            "Stopped listening. The microphone is no longer being read, so nothing said "
            f"from now is transcribed, logged or recoverable. Tell them the {key} key "
            "brings you back, because from here you cannot hear them ask."
        )

    def resume_transcription() -> str:
        ears.resume()
        return (
            "Listening again. Nothing said during the pause was captured, so there is "
            "nothing to catch up on."
        )

    return [
        Tool(
            name="pause_transcription",
            description=(
                "Stop listening. The microphone stops being read, so nothing is "
                "transcribed, logged or recorded until it is resumed - not merely "
                "withheld from you.\n\n"
                "For when they ask for privacy, or say they are on a call, or are about "
                "to have a conversation that is not with you.\n\n"
                "Call it FIRST and say so afterwards. Your words end your turn, so a "
                "reply that promises to stop listening is a promise instead of the act - "
                f"and once it is done, say that the {key} key brings you back, because "
                "from then on you cannot hear them ask. Not a way to avoid answering "
                "something: a hyphen does that and keeps your ears."
            ),
            run=pause_transcription,
        ),
        Tool(
            name="resume_transcription",
            description=(
                "Start reading the microphone again. Nothing said during the pause is "
                "recoverable - it was never captured."
            ),
            run=resume_transcription,
        ),
    ]


def _memory_tools(config: Config) -> list[Tool]:
    """Writing a lesson down, if there is a list to write it to."""
    if not config.brain.memories:
        return []
    from . import memories

    path = memory_file(config)

    def remember(lesson: str) -> str:
        return memories.remember(path, lesson, config.brain.max_memory_chars)

    return [
        Tool(
            name="remember",
            description=(
                "Write down one thing you have learned about this machine, so you have it "
                "next time. Your whole list is read back into your prompt at the start of "
                "every turn.\n\n"
                "This is for how the desk behaves, and most of it is only discoverable by "
                "getting it wrong: a window whose tree is empty until it has been focused, "
                "an application that takes a moment to build itself, which of four "
                "identically labelled buttons is the one that works, a command that turned "
                "out to be the way to do something. When a tool refuses you and you work "
                "out why, that is exactly what this is for.\n\n"
                "Not for anything about one conversation - not what they asked for, not "
                "what you replied, not what they like. One sentence, and specific enough "
                "to act on months from now: a number from a scan will be wrong by then, "
                "a label or a window name will not."
            ),
            run=remember,
            properties={"lesson": {"type": "string", "description": "one sentence"}},
            required=("lesson",),
        )
    ]


def memory_file(config: Config) -> Path:
    """The file remember() writes to. Relative names sit under the project root."""
    return under_root(config.brain.memories_file)


def navigation_file(config: Config) -> Path:
    """The file the looking back writes to, beside the shipped reference."""
    return under_root(config.brain.navigation_file)


def under_root(named: str) -> Path:
    from .config import project_root

    path = Path(named).expanduser()
    return path if path.is_absolute() else project_root() / path


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
                "machine, say what you did once it is done - your words end your turn, so "
                "announcing it first means it never happens."
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
