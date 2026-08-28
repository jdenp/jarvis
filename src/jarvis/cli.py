"""Command line entry point.

`jarvis` with no arguments runs the voice service; everything else is a client.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import tomllib
from dataclasses import replace

from . import __version__
from .client import ServiceUnavailable, VoiceClient
from .config import Config, find_config_file, project_root
from .logging_setup import configure
from .microphone import Microphone, MicrophoneError

BANNER = r"""
   _   _   ___ _   _ ___ ___
  | | /_\ | _ \ | | |_ _/ __|   Just A Rather Very Intelligent System
  | |/ _ \|   / |_| || |\__ \   v{version}
 _/ /_/ \_\_|_\\___/|___|___/
|__/
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jarvis",
        description="A local voice service. Run with no arguments to start listening.",
    )
    parser.add_argument("--version", action="version", version=f"jarvis {__version__}")
    parser.add_argument(
        "--list-devices", action="store_true", help="print the input devices and exit"
    )
    parser.add_argument("--device", type=int, metavar="N", help="input device index")
    parser.add_argument(
        "--tts", choices=["auto", "edge", "sapi", "none"], help="text to speech engine"
    )
    parser.add_argument("--stt", choices=["whisper", "google"], help="speech to text backend")
    parser.add_argument("--port", type=int, help="port for the voice service")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    sub = parser.add_subparsers(
        dest="command",
        metavar="[serve | chat | say | next | status | look | click | screenshot | rules | mcp]",
    )

    serve = sub.add_parser("serve", help="run the voice service (the default with no arguments)")
    serve.add_argument("--no-http", action="store_true", help="transcribe to file only, no API")

    talk = sub.add_parser("chat", help="type to JARVIS instead of speaking - no microphone")
    talk.add_argument(
        "--verbose", action="store_true", help="show tool results and warnings on screen too"
    )

    speak = sub.add_parser("say", help="speak text through the running service")
    speak.add_argument("text", nargs="+", help="what to say")

    nxt = sub.add_parser("next", help="block until the user says something, then print it")
    nxt.add_argument(
        "--wait",
        type=float,
        default=None,
        metavar="SECONDS",
        help="give up after this long. Omit to wait indefinitely.",
    )
    nxt.add_argument("--since", type=int, help="only utterances after this id")
    nxt.add_argument("--json", action="store_true", help="print the raw record")
    nxt.add_argument("--follow", action="store_true", help="keep printing as more arrives")

    sub.add_parser("mcp", help="run as an MCP server over stdio, for a connected agent")
    sub.add_parser("status", help="report on the running voice service")

    look = sub.add_parser("look", help="number what is clickable on screen, and save the map")
    look.add_argument("window", nargs="?", default="", help="part of a window title")
    look.add_argument("--matching", default="", help="only targets whose label contains this")
    look.add_argument("--focus", action="store_true", help="bring the window to the front first")
    look.add_argument("--marks", action="store_true", help="write the marked screenshot too")
    look.add_argument("--json", action="store_true", help="print the raw scan")
    look.add_argument(
        "--raw",
        action="store_true",
        help="every element the window exposed, not only the ones offered as targets",
    )

    kit = sub.add_parser("tools", help="what the brain can do, as the model is told it")
    kit.add_argument(
        "--write", action="store_true", help="write context/tools/tools.md instead of printing"
    )

    rules = sub.add_parser("rules", help="check the agent guide the client is loading")
    rules.add_argument(
        "--install", action="store_true", help="copy the current guide over the stale one"
    )
    rules.add_argument("--path", help="the file the agent reads its rules from")

    shot = sub.add_parser("screenshot", help="save a picture of a window")
    shot.add_argument("window", nargs="?", default="", help="part of a window title")
    shot.add_argument("--whole-desk", action="store_true", help="every monitor, not one window")
    shot.add_argument("--numbers", action="store_true", help="draw the target numbers on it")
    shot.add_argument("--out", help="where to write it, instead of logs/")

    press = sub.add_parser("click", help="click a number from the last `jarvis look`")
    press.add_argument("target", type=int, help="the id printed by `jarvis look`")
    press.add_argument(
        "--expecting", required=True, help="the label you read next to it, checked before clicking"
    )
    press.add_argument("--right", action="store_true", help="right click instead of left")
    press.add_argument("--double", action="store_true", help="double click")

    cfg = sub.add_parser("config", help="show the settings in effect, as JSON")
    cfg.add_argument(
        "--defaults",
        action="store_true",
        help="show the built-in defaults rather than what is in effect",
    )
    cfg.add_argument(
        "--write",
        action="store_true",
        help="write to config/defaults.json instead of printing",
    )
    return parser


def apply_args(config: Config, args: argparse.Namespace) -> Config:
    """Overlay command line flags, which win over the file and environment."""
    audio = config.audio
    if args.device is not None:
        audio = replace(audio, device_index=args.device)

    stt = replace(config.stt, backend=args.stt) if args.stt else config.stt
    tts = replace(config.tts, engine=args.tts) if args.tts else config.tts
    service = replace(config.service, port=args.port) if args.port else config.service

    return replace(
        config,
        audio=audio,
        stt=stt,
        tts=tts,
        service=service,
        log_level=args.log_level or config.log_level,
    )


def _banner() -> str:
    """The name, in orange if the terminal will take it.

    `usable` turns VT processing on as a side effect, which has to happen before
    the first escape code goes out - and this is the first thing printed.
    """
    from . import ui as terminal

    art = BANNER.format(version=__version__).rstrip()
    return terminal.paint("art", art, terminal.usable(sys.stdout))


def print_devices() -> int:
    for index, name in Microphone.list_devices():
        print(f"{index:>3}  {name}")
    return 0


def privacy_report(config: Config, stt_local: bool, tts_local: bool) -> str:
    """One line naming each stage and whether it leaves the machine."""
    stages = [
        ("ears", config.stt.backend, stt_local, "your microphone audio"),
        ("voice", config.tts.engine, tts_local, "every reply"),
    ]
    from urllib.parse import urlparse

    from .brain import is_loopback

    where = urlparse(config.brain.url).netloc or config.brain.url
    # Between the two, because that is the order the words travel in. An
    # endpoint off this machine sees every word of every conversation.
    stages.insert(1, ("brain", where, is_loopback(config.brain.url), "the conversation"))
    if config.brain.web:
        engine = urlparse(config.brain.search_url).netloc or config.brain.search_url
        stages.append(("web", engine, is_loopback(config.brain.search_url), "what you look up"))
    summary = " -> ".join(
        f"{name}: {what} ({'local' if local else 'REMOTE'})" for name, what, local, _ in stages
    )
    leaks = [sends for _, _, local, sends in stages if not local]
    if not leaks:
        return f"{summary}. Nothing leaves this machine."
    return f"{summary}. Leaving this machine: {', '.join(leaks)}."


def run_serve(config: Config, args: argparse.Namespace, logger) -> int:
    """Own the microphone and expose it. This is what an agent talks to."""
    from . import ui as terminal
    from .reap import reap_orphans
    from .service import VoiceService, build_server

    if cleared := reap_orphans():
        logger.info("Cleared %d stranded MCP server(s) from an earlier session.", cleared)

    # Built now, handed to the service only once the brain is actually running -
    # see below. Until then the boot lines go through plain logging, because a
    # five second Whisper load with nothing on screen looks like a hang.
    screen = terminal.Ui()
    service = VoiceService(config)
    try:
        service.start()
    except MicrophoneError as exc:
        logger.error("%s", exc)
        logger.error("Run `jarvis --list-devices` and pick one with --device N.")
        return 2

    logger.info(
        privacy_report(
            config,
            stt_local=getattr(service.transcriber, "is_local", False),
            tts_local=service.speech.is_local,
        )
    )
    logger.info("Transcript: %s", config.log_dir / config.service.transcript_file)
    from .screen import we_are_admin

    if we_are_admin():
        logger.warning(
            "Running as administrator. Elevated windows can be driven, and every command "
            "run_command runs is an administrator command with nothing asked first."
        )

    from . import brain
    from .brain import ModelUnavailable

    try:
        mind = brain.start(config, service, terminal=screen)
    except (ModelUnavailable, OSError) as exc:
        # Nothing carries on without a brain. Listening, transcribing and saying
        # nothing is a JARVIS that looks entirely well and answers no one, which
        # is a worse thing to leave running than a process that stops here.
        logger.error("JARVIS cannot start: %s.", exc)
        logger.error("It only runs with a model. Start one, or point brain.url at another.")
        service.stop()
        return 2

    service.ui = screen
    _hand_the_terminal_over(logger, screen)
    # Only with a terminal to read from. Started after the takeover so that the
    # first thing it does cannot land in the middle of the boot lines.
    typing = None
    if screen.colour:
        from .typed import Typing

        typing = Typing(screen, service.typed, on_cancel=mind.cancel)
        typing.start()
        screen.note("Type a line and press enter to say it without speaking.")
        screen.note("Escape stops JARVIS mid thought and asks you again.")

    if getattr(args, "no_http", False):
        logger.info("Listening. Transcript file only, no API. Ctrl+C to stop.")
        try:
            while True:
                service.transcript.wait_for(service.transcript.cursor, timeout=3600)
        except KeyboardInterrupt:
            logger.info("\nStopping.")
        finally:
            service.stop()
        return 0

    try:
        server = build_server(service)
    except OSError as exc:
        logger.error("Could not bind %s:%s - %s", config.service.host, config.service.port, exc)
        logger.error("Another `jarvis serve` may already be running.")
        service.stop()
        return 2

    logger.info(
        "Voice service on http://%s:%s - `jarvis say`, `jarvis next` and `jarvis mcp` all "
        "talk to this. Ctrl+C to stop.",
        config.service.host,
        config.service.port,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("\nStopping.")
    finally:
        if typing is not None:
            typing.stop()
        screen.close()
        server.shutdown()
        server.server_close()
        service.stop()
    return 0


def _hand_the_terminal_over(logger, screen) -> None:
    """Swap the plain console handler for one that draws through the UI.

    A stream handler writes straight to stdout, which walks over the live status
    line and interleaves badly with the conversation above it.
    """
    from . import ui as terminal

    for handler in list(logger.handlers):
        if type(handler) is logging.StreamHandler:
            logger.removeHandler(handler)
    logger.addHandler(terminal.LogToUi(screen))


def run_say(config: Config, args: argparse.Namespace) -> int:
    text = " ".join(args.text).strip()
    if not text:
        print("Nothing to say.", file=sys.stderr)
        return 1
    with VoiceClient(config.service) as voice:
        try:
            voice.say(text)
        except ServiceUnavailable as exc:
            print(exc, file=sys.stderr)
            return 2
    return 0


def run_next(config: Config, args: argparse.Namespace) -> int:
    """Block until something is said.

    The service caps a single poll, so waiting indefinitely means re-issuing
    it. Still no busy looping - each poll sleeps until spoken to.
    """
    slice_seconds = config.service.max_wait_seconds
    remaining = args.wait  # None means wait indefinitely

    with VoiceClient(config.service) as voice:
        try:
            cursor = args.since if args.since is not None else voice.status()["cursor"]
            while True:
                wait = slice_seconds if remaining is None else min(remaining, slice_seconds)
                result = voice.heard(since=cursor, wait=wait)
                cursor = result["cursor"]
                for item in result["heard"]:
                    print(json.dumps(item) if args.json else item["text"], flush=True)

                if result["heard"] and not args.follow:
                    return 0
                if remaining is not None and not args.follow:
                    remaining -= wait
                    if remaining <= 0:
                        return 1
        except ServiceUnavailable as exc:
            print(exc, file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            return 130


DEFAULTS_FILE = "defaults.json"


def run_config(config: Config, args: argparse.Namespace) -> int:
    """Show the settings, or regenerate the defaults file from the dataclasses."""
    shown = Config() if args.defaults else config
    body = json.dumps(shown.as_dict(), indent=2) + "\n"

    if not args.write:
        print(body, end="")
        if not args.defaults:
            source = find_config_file()
            print(f"\n// from {source}" if source else "\n// no config file, all defaults")
        return 0

    target = config.config_dir / DEFAULTS_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    print(f"Wrote {target}")
    return 0


SCAN_FILE = "scan.json"


def run_look(config: Config, args: argparse.Namespace) -> int:
    """Number what is on screen and leave the map where `jarvis click` will find it."""
    from .screen import Screen, ScreenUnavailable

    screen = Screen(config.screen)
    try:
        scan = (
            screen.focus(args.window, args.matching)
            if args.focus
            else screen.look(args.window, args.matching)
        )
    except ScreenUnavailable as exc:
        print(exc, file=sys.stderr)
        return 2

    if args.raw:
        # What the window said, before any of the filtering. The only way to
        # tell "the control is not in the tree" from "the control was dropped",
        # which from the outside look identical and want opposite fixes.
        offered = {element.runtime_id: target.number for target, element in scan.pairs()}
        elements = screen.backend.elements(scan.hwnd)
        print(f"{scan.window}  ({len(elements)} elements, {len(scan.targets)} offered)")
        for element in elements:
            number = offered.get(element.runtime_id)
            mark = f"{number:>4}" if number else "   ."
            box = f"{element.width}x{element.height} at {element.left},{element.top}"
            print(f"{mark}  {element.role:14s} {element.name[:44]:44s} {box}")
        print()
        print("A dot is an element that was not offered as a target.")
    elif args.json:
        print(json.dumps(scan.as_json(), indent=2))
    else:
        print(f"{scan.window}  ({scan.considered} elements, {len(scan.targets)} targets)")
        for target in scan.targets:
            where = f"  [{target.where}]" if target.where else ""
            print(f"{target.number:4d}  {target.element.role:11s} {target.element.label}{where}")
        if scan.truncated:
            print(f"\n{scan.truncated} more did not fit. Narrow it with --matching.")

    screen.remember(config.log_dir / SCAN_FILE)
    if args.marks:
        from . import marks

        try:
            path = marks.draw(
                scan,
                screen.backend.window_rect(scan.hwnd),
                config.log_dir / (config.screen.marks_file or "marks.png"),
            )
            print(f"\nMarked screenshot: {path}")
        except (OSError, RuntimeError) as exc:
            print(exc, file=sys.stderr)
            return 2
    return 0


# Generated, and committed, for the same reason config/defaults.json is: it is
# the only readable account of what the model is actually told, and a test keeps
# it honest.
TOOLS_FILE = "context/tools/tools.md"


def run_tools(config: Config, args: argparse.Namespace) -> int:
    """Write out what the brain can do, or print it.

    Every feature on, whatever this machine's config says - the file is a
    description of the software rather than of one installation.
    """
    from .tools import as_markdown, build_toolbox

    class Ears:
        """Stands in for the microphone, which only the voice path has."""

        def pause(self) -> bool:
            return True

        def resume(self) -> None:
            pass

    body = as_markdown(build_toolbox(Config(), ears=Ears()))
    if not args.write:
        print(body, end="")
        return 0

    target = project_root() / TOOLS_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    print(f"Wrote {target}")
    return 0


# Who JARVIS is: the brain reads it as its system prompt, and it is what an
# agent driving JARVIS over MCP should be handed as well. There used to be two
# files here, one per path, and the difference between them was never clear
# enough to be worth the duplication - the character is the same either way, and
# the MCP mechanics are served over the protocol by mcp_server.INSTRUCTIONS.
GUIDE = "context/soul/jarvis.md"


def run_rules(config: Config, args: argparse.Namespace) -> int:
    """Whether the guide the agent is actually reading is the current one.

    Worth a command of its own because the failure is invisible and total: a
    guide from before the tools were renamed names tools that do not exist, and
    the model believes it over the schemas. Nothing in the session says so.

    Only the MCP path needs it, so there is no default location - clients keep
    their rules wherever they like. `service.agent_rules`, or --path.
    """
    from pathlib import Path

    source = project_root() / GUIDE
    where = args.path or config.service.agent_rules
    if not where:
        print(
            "Nowhere to check. Set service.agent_rules to the file your agent reads its "
            f"rules from, or pass --path, and this will keep it in step with {GUIDE}. "
            "Only needed when the microphone is handed to an agent over MCP.",
            file=sys.stderr,
        )
        return 2

    installed = Path(where).expanduser()
    current = source.read_bytes()

    if args.install:
        installed.parent.mkdir(parents=True, exist_ok=True)
        installed.write_bytes(current)
        print(f"Wrote {installed} from {source}")
        print("Start a new task - the old text is still in the open one's history.")
        return 0

    if not installed.is_file():
        print(f"No guide at {installed}. Install it with `jarvis rules --install`.")
        return 1
    if installed.read_bytes() == current:
        print(f"{installed}\n  matches {GUIDE} ({len(current)} bytes).")
        return 0
    print(
        f"STALE: {installed} does not match {source}.\n"
        "The agent is being told about tools that may no longer exist, every turn, "
        "and it will believe that over the schemas.\n"
        "Fix it with `jarvis rules --install`.",
        file=sys.stderr,
    )
    return 1


def run_screenshot(config: Config, args: argparse.Namespace) -> int:
    """A plain picture, for the times when the numbered list is not the question."""
    from pathlib import Path

    from . import marks
    from .screen import Screen, ScreenUnavailable

    screen = Screen(config.screen)
    out = (
        Path(args.out)
        if args.out
        else config.log_dir / (config.screen.screenshot_file or "screen.png")
    )
    try:
        if args.whole_desk:
            path = marks.capture(None, out, config.screen.screenshot_max_width)
        elif args.numbers:
            scan = screen.look(args.window)
            path = marks.draw(scan, screen.backend.window_rect(scan.hwnd), out)
        else:
            hwnd, _title = screen.find_window(args.window)
            bounds = screen.backend.window_rect(hwnd)
            path = marks.capture(bounds, out, config.screen.screenshot_max_width)
    except (ScreenUnavailable, OSError, RuntimeError) as exc:
        print(exc, file=sys.stderr)
        return 2
    print(f"{path}  ({path.stat().st_size // 1024} KB)")
    return 0


def run_click(config: Config, args: argparse.Namespace) -> int:
    """Click a number from the saved scan. Yours to run - `screen.control` gates
    the agent's tools, not this."""
    from . import hands
    from .screen import Screen, ScreenUnavailable

    screen = Screen(config.screen)
    try:
        screen.recall(config.log_dir / SCAN_FILE)
        target, _scan = screen.aim(args.target)
    except ScreenUnavailable as exc:
        print(exc, file=sys.stderr)
        return 2

    from .screen import means_the_same

    if not means_the_same(args.expecting, target.element.label):
        print(
            f"Target {args.target} is {target.element.label!r}, not {args.expecting!r}. "
            "Nothing was clicked.",
            file=sys.stderr,
        )
        return 1

    x, y = target.element.centre
    hands.click(
        x,
        y,
        button="right" if args.right else "left",
        count=2 if args.double else 1,
        settle=config.screen.click_settle_seconds,
    )
    print(f"Clicked {target.element.label!r} at {x},{y}")
    return 0


def run_status(config: Config) -> int:
    with VoiceClient(config.service) as voice:
        try:
            print(json.dumps(voice.status(), indent=2))
        except ServiceUnavailable as exc:
            print(exc, file=sys.stderr)
            return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_devices:
        return print_devices()

    try:
        config = apply_args(Config.load(), args)
    except (ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"jarvis.toml is not valid - {exc}", file=sys.stderr)
        return 2

    # Clients print results for something else to read, and MCP in particular
    # must keep stdout clean - anything there is parsed as JSON-RPC.
    if args.command in {
        "say",
        "next",
        "status",
        "config",
        "look",
        "click",
        "screenshot",
        "rules",
        "tools",
    }:
        configure(config.log_dir, "WARNING")
        if args.command == "say":
            return run_say(config, args)
        if args.command == "next":
            return run_next(config, args)
        if args.command == "config":
            return run_config(config, args)
        if args.command == "look":
            return run_look(config, args)
        if args.command == "click":
            return run_click(config, args)
        if args.command == "screenshot":
            return run_screenshot(config, args)
        if args.command == "rules":
            return run_rules(config, args)
        if args.command == "tools":
            return run_tools(config, args)
        return run_status(config)

    if args.command == "chat":
        # No microphone and no service, so this runs anywhere an ssh session
        # does. Logging stays off the screen unless asked for - the tool calls
        # are printed by the chat front end and duplicating them is noise.
        configure(config.log_dir, config.log_level, console=args.verbose)
        from .chat import run as run_chat

        return run_chat(config, args.verbose)

    if args.command == "mcp":
        configure(config.log_dir, config.log_level, console=False)
        from .mcp_server import main as run_mcp

        return run_mcp(config)

    logger = configure(config.log_dir, config.log_level)
    logger.info(_banner())
    try:
        return run_serve(config, args, logger)
    except KeyboardInterrupt:
        return 130
    finally:
        logging.shutdown()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
