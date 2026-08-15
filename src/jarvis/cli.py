"""Command line entry point.

JARVIS is ears and a mouth. Whatever agent is on the other end is the brain, so
`jarvis` with no arguments runs the voice service and everything else is a thin
client of it.
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
from .config import Config
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
    parser.add_argument(
        "--no-wake-word", action="store_true", help="pass on everything, not just 'jarvis ...'"
    )
    parser.add_argument("--port", type=int, help="port for the voice service")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    sub = parser.add_subparsers(dest="command", metavar="[serve | say | next | status | mcp]")

    serve = sub.add_parser("serve", help="run the voice service (the default with no arguments)")
    serve.add_argument("--no-http", action="store_true", help="transcribe to file only, no API")

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

    sub.add_parser("mcp", help="run as an MCP server over stdio, for Cline and friends")
    sub.add_parser("status", help="report on the running voice service")
    return parser


def apply_args(config: Config, args: argparse.Namespace) -> Config:
    """Overlay command line flags, which win over the file and environment."""
    audio = config.audio
    if args.device is not None:
        audio = replace(audio, device_index=args.device)

    wake = replace(config.wake, required=False) if args.no_wake_word else config.wake
    stt = replace(config.stt, backend=args.stt) if args.stt else config.stt
    tts = replace(config.tts, engine=args.tts) if args.tts else config.tts
    service = replace(config.service, port=args.port) if args.port else config.service

    return replace(
        config,
        audio=audio,
        wake=wake,
        stt=stt,
        tts=tts,
        service=service,
        log_level=args.log_level or config.log_level,
    )


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
    summary = " -> ".join(
        f"{name}: {what} ({'local' if local else 'REMOTE'})" for name, what, local, _ in stages
    )
    leaks = [sends for _, _, local, sends in stages if not local]
    if not leaks:
        return f"{summary}. Nothing leaves this machine."
    return f"{summary}. Leaving this machine: {', '.join(leaks)}."


def run_serve(config: Config, args: argparse.Namespace, logger) -> int:
    """Own the microphone and expose it. This is what an agent talks to."""
    from .service import VoiceService, build_server

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
        server.shutdown()
        server.server_close()
        service.stop()
    return 0


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
    """Block until something is said. This is the interrupt an agent waits on.

    The service caps a single long poll so its threads cannot be held forever,
    so waiting indefinitely means re-issuing the poll rather than asking for an
    unbounded one. Still no busy looping - each poll sleeps until spoken to.
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
    if args.command in {"say", "next", "status"}:
        configure(config.log_dir, "WARNING")
        if args.command == "say":
            return run_say(config, args)
        if args.command == "next":
            return run_next(config, args)
        return run_status(config)

    if args.command == "mcp":
        configure(config.log_dir, config.log_level, console=False)
        from .mcp_server import main as run_mcp

        return run_mcp(config)

    logger = configure(config.log_dir, config.log_level)
    logger.info(BANNER.format(version=__version__).rstrip())
    try:
        return run_serve(config, args, logger)
    except KeyboardInterrupt:
        return 130
    finally:
        logging.shutdown()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
