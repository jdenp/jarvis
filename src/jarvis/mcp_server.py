"""MCP server, so an agent knows it has ears and a voice.

Two tools. ``wait_for_speech`` blocks until the user says something, which is
what makes this an interrupt rather than a polling loop - the agent asks once
and the call returns the instant a sentence lands. ``say`` speaks a reply.

This is a client of a running `jarvis serve`, not a second copy of it. Only one
process may own the microphone, and the agent may start and stop this one freely.
"""

from __future__ import annotations

import logging
import threading

from .client import ServiceUnavailable, VoiceClient
from .config import Config, ServiceConfig

logger = logging.getLogger("jarvis.mcp")

INSTRUCTIONS = """\
JARVIS gives you a microphone and a voice on the user's desktop.

THESE TOOLS ARE OFF BY DEFAULT. Being connected does not mean voice is wanted.
Unless the user has asked for it, work normally in text and do not touch them:
do not call wait_for_speech, do not call say, and do not start the service.

Voice mode starts only when the user asks in words - "listen", "wait on jarvis",
"use voice", "talk to me" or similar. Until they do, ignore all of this.

Once they have asked, and until they say to stop:

YOU DECIDE WHAT WAS MEANT FOR YOU. There is no wake word. The microphone sends
you everything it hears: requests, half sentences, the user thinking aloud,
someone else in the room, a video playing. Your first job on every utterance is
to judge whether it was addressed to you.

- A task or a question aimed at you: do it, then say() the answer.
- Anything else - background talk, muttering, a fragment that is not a request,
  something clearly said to another person: say NOTHING. Call wait_for_speech
  again and keep listening. Silence is the correct response, not a failure.
- Half a request, or something that trails off: do not guess and do not ask them
  to repeat it. Call wait_for_speech again; the rest of the sentence is usually
  in the next batch, and you will see it together with what came before.

When you do answer, answer out loud. say() is a tool call - writing the words in
your reply is not the same thing, and the user cannot see your chat. Go straight
back to listening afterwards: no "anything else?", no written recap.

Narrate anything slow; they cannot see your screen and a long silence looks like
a crash. Keep spoken replies short and free of markdown, since they are read out.

Address them as "sir". Not every line - that turns into a tic - but often enough
that it is plainly the register. It sits best on an acknowledgement ("Yes, sir."),
at the end of a short answer, and on a greeting. Once per reply at most, and never
mid sentence. Underdo it rather than overdo it. Beyond that stay plain: dry and
unhurried, no theatrics, no "certainly!".

wait_for_speech returning nothing means they have not spoken yet. Call it again."""


class Acknowledger:
    """Speaks a holding line when an answer is taking a while.

    Anything that needs a search or a slow model leaves several seconds of dead
    air, which from the user's side is indistinguishable from a crash. This fills
    it, and cancels itself the moment the real answer arrives - so a quick reply
    never gets a redundant "one moment" in front of it.
    """

    def __init__(self, voice: VoiceClient, config: ServiceConfig) -> None:
        self._voice = voice
        self._after = config.acknowledge_after
        self._phrases = tuple(config.acknowledgements)
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._index = 0

    @property
    def enabled(self) -> bool:
        return self._after > 0 and bool(self._phrases)

    def arm(self) -> None:
        """Start the clock on an answer we are now waiting for."""
        self.cancel()
        if not self.enabled:
            return
        with self._lock:
            self._timer = threading.Timer(self._after, self._speak)
            self._timer.daemon = True
            self._timer.start()

    def cancel(self) -> None:
        with self._lock:
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()

    def _speak(self) -> None:
        with self._lock:
            phrase = self._phrases[self._index % len(self._phrases)]
            self._index += 1
        try:
            self._voice.say(phrase)
        except Exception:  # the real answer still matters more than this
            logger.debug("Could not speak the holding line.", exc_info=True)


def build_server(config: Config | None = None, client: VoiceClient | None = None):
    """Construct the MCP server. Import is deferred so the CLI stays fast."""
    from mcp.server.mcpserver import MCPServer

    from . import __version__

    config = config or Config.load()
    voice = client or VoiceClient(config.service)
    # Nothing is heard before the agent first asks, so start from "now" rather
    # than replaying whatever was said before it connected.
    cursor = _initial_cursor(voice)
    acknowledger = Acknowledger(voice, config.service)

    server = MCPServer(
        name="jarvis",
        title="JARVIS voice",
        version=__version__,
        instructions=INSTRUCTIONS,
    )

    @server.tool(
        name="wait_for_speech",
        title="Listen for the user",
        description=(
            "Block until the user says something out loud, then return it. Takes "
            "no arguments and waits as long as it can - there is nothing to tune. "
            "If it returns with nothing heard, the client's own limit was reached, "
            "not the user's patience: just call it again. There is no wake word, so "
            "some of what comes back will not be for you - decide, and stay silent "
            "when it is not."
        ),
    )
    def wait_for_speech() -> dict:
        # No timeout argument on purpose. Given one, models pick a small number
        # and give up while the user is still deciding what to say.
        nonlocal cursor

        # Calling this again means the agent has moved on, so no holding line.
        # There is nothing to chase it about: with no wake word, most utterances
        # deserve no reply, and a server cannot tell a correct silence from a
        # forgotten one.
        acknowledger.cancel()

        try:
            result = voice.heard(
                since=cursor,
                wait=config.service.max_wait_seconds,
                addressed_only=True,
            )
        except ServiceUnavailable as exc:
            return {
                "error": str(exc),
                "heard": [],
                "next_step": (
                    "The voice service is not running. Start it with "
                    "`jarvis.ps1 -Windowed` from the repository root, then call this again."
                ),
            }
        heard = result.get("heard", [])
        cursor = result.get("cursor", cursor)
        if not heard:
            return {
                "heard": [],
                "next_step": "Nothing said yet. Call wait_for_speech again to keep waiting.",
            }

        spoken_text = [item.get("command") or item["text"] for item in heard]
        acknowledger.arm()  # fills the silence if an answer takes a while

        # The judgement call is restated here, not only in the server
        # instructions, because a tool result lands in context immediately
        # before the model replies - the moment it decides whether to speak.
        payload = {
            "heard": spoken_text,
            "next_step": (
                "There is no wake word, so this may not have been meant for you. "
                "Decide first. If it is a task or a question for you, do it and "
                "call say() with the answer - the user is listening, not reading, "
                "so anything you only write down reaches them as silence. If it is "
                "background talk, muttering, or half a sentence that is not a "
                "request, say NOTHING and call wait_for_speech again. Staying quiet "
                "is a correct answer here, not a failure."
            ),
            "detail": heard,
        }
        return payload

    @server.tool(
        name="say",
        title="Speak to the user",
        description=(
            "Speak text aloud through the user's speakers. Call this for every "
            "answer and every outcome - the user is listening, not reading, so a "
            "reply you only write down never reaches them. The microphone is muted "
            "while speaking, so JARVIS does not transcribe itself. Keep it short "
            "and plain: no markdown, lists or emoji, they get read out literally."
        ),
    )
    def say(text: str) -> dict:
        acknowledger.cancel()
        try:
            voice.say(text)
        except ServiceUnavailable as exc:
            return {"error": str(exc), "spoken": False}
        return {
            "spoken": True,
            "text": text,
            "next_step": "Call wait_for_speech again to keep listening.",
        }

    @server.tool(
        name="voice_status",
        title="Check the voice service",
        description="Report whether the microphone is live and which backends are in use.",
    )
    def voice_status() -> dict:
        try:
            return voice.status()
        except ServiceUnavailable as exc:
            return {"error": str(exc), "listening": False}

    return server


def _initial_cursor(voice: VoiceClient) -> int:
    try:
        return int(voice.status().get("cursor", 0))
    except (ServiceUnavailable, TypeError, ValueError):
        logger.warning("No voice service yet, starting from the beginning of the transcript.")
        return 0


def main(config: Config | None = None) -> int:
    """Run over stdio, which is how Cline and friends launch an MCP server."""
    from .reap import exit_when_orphaned

    # Closing the pipe is the normal way this ends, and it is handled for us.
    # This covers the client being killed instead, where the launch chain keeps
    # the pipe open and we would otherwise sit here forever.
    exit_when_orphaned()
    build_server(config).run(transport="stdio")
    return 0
