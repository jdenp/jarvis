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

- Do not ask what they need. Call wait_for_speech straight away and let them say
  it out loud. Asking "what can I help with?" in text wastes a turn.
- Every answer goes through say(). The user is listening, not reading - an answer
  written only in chat reaches them as silence, and looks like you ignored them.
  wait_for_speech will refuse to listen again until you have called say().
- The loop is: wait_for_speech, do the work, say() the answer, wait_for_speech
  again. Go straight back to listening. No "anything else?", no written recap.
- Narrate anything slow. They cannot see your screen, so a long silence looks
  like a crash.
- Keep spoken replies short and free of markdown, since they are read aloud.

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
    # The last thing heard that has not been answered out loud. Instructions
    # alone do not stop an agent answering in text and going back to listening,
    # so the server refuses to listen again until it has spoken.
    unanswered: str | None = None
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
            "not the user's patience: just call it again. You must answer the "
            "previous utterance with say() before calling this again; it will "
            "refuse otherwise."
        ),
    )
    def wait_for_speech() -> dict:
        # No timeout argument on purpose. Given one, models pick a small number
        # and give up while the user is still deciding what to say.
        nonlocal cursor, unanswered

        if unanswered is not None:
            return {
                "refused": True,
                "heard": [],
                "unanswered_question": unanswered,
                "next_step": (
                    "You have not spoken your answer yet, so the user has heard "
                    "nothing. They asked: "
                    f'"{unanswered}". Call say() with your answer to that now. '
                    "Writing it in chat does not reach them. This tool will keep "
                    "refusing until you do."
                ),
            }

        try:
            result = voice.heard(since=cursor, wait=config.service.max_wait_seconds)
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
        # The reminder lives here, not only in the server instructions, because a
        # tool result lands in context immediately before the model replies -
        # which is the moment it decides whether to speak or merely write.
        spoken_text = [item["text"] for item in heard]
        unanswered = spoken_text[-1]
        acknowledger.arm()  # fills the silence if the answer takes a while
        return {
            "heard": spoken_text,
            "next_step": (
                "Do what was asked, then call say() with your answer. YOU MUST "
                "ANSWER WITH say() - the user is listening, not reading, so an "
                "answer written in chat reaches them as silence. wait_for_speech "
                "will refuse to listen again until you have called say()."
            ),
            "detail": heard,
        }

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
        nonlocal unanswered
        acknowledger.cancel()
        try:
            voice.say(text)
        except ServiceUnavailable as exc:
            return {"error": str(exc), "spoken": False}
        unanswered = None
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
    build_server(config).run(transport="stdio")
    return 0
