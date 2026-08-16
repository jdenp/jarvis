"""MCP server, so an agent knows it has ears and a voice.

``wait_for_speech`` blocks until the user says something, which is what makes
this an interrupt rather than a polling loop - the agent asks once and the call
returns the instant a sentence lands. ``check_for_speech`` is its non-blocking
twin, for looking up mid task: nothing can preempt an agent, so being steered
part way through only works if it chooses to look. ``say`` speaks a reply.

This is a client of a running `jarvis serve`, not a second copy of it. Only one
process may own the microphone, and the agent may start and stop this one freely.
"""

from __future__ import annotations

import logging
import random
import threading
from datetime import UTC, datetime

from .client import ServiceUnavailable, VoiceClient
from .config import Config, ServiceConfig

logger = logging.getLogger("jarvis.mcp")

INSTRUCTIONS = """\
A microphone and a voice on the user's desktop.

OFF BY DEFAULT. Being connected does not mean voice is wanted. Do not call these
tools or start the service until asked. "jarvis" on its own means start listening
now - call wait_for_speech immediately, no text reply, nothing else first. So do
"listen", "use voice", "talk to me". It ends when they say so or go back to typing.

THE LOOP: wait_for_speech -> do the work -> say(answer) -> wait_for_speech.
Straight back to listening after speaking. No "anything else?", no written recap.
Never end your turn on a say() - they are still there, still listening, and it
drops the conversation mid air. The loop ends when they end it, not when you
have finished a sentence.

THREE RULES:

1. Answering is calling say(). Working out the answer is not answering; writing
   it in your reply is not answering, they cannot see your chat. The moment you
   know what to tell them the next tool call is say() - not wait_for_speech, not
   one more search. Deciding to speak and then listening instead is the commonest
   failure here, and it is identical to being ignored. Say it when a tool fails
   too: four failed searches then silence reads as a crash.

2. Silence is a valid reply. No wake word, so you hear everything - other people,
   videos, thinking aloud. Act only on what was aimed at you; for anything else
   say nothing and listen again. Answering what nobody asked is worse than
   missing one.

3. If it sounds cut off, listen again - do not ask them to repeat it. A phrase
   ends after a fixed silence, not when the speaker finishes, so a mid sentence
   pause splits one request in two and the rest is already queued. Ending mid
   clause, a verb with nothing to act on, or a reference to something never
   mentioned all mean the other half is a moment away. Only ask if it is still
   incomplete the second time.

WHILE YOU WORK: they cannot see your screen, so silence looks like a crash. Say
what you are about to do before anything slow, and call check_for_speech between
steps of a long task - it returns instantly and is the only way "actually, do it
the other way" reaches you before you have finished doing it the first way.

SPOKEN REPLIES are read aloud: under forty words, no markdown, never read code or
long paths. Say "sir" as a tendency not a rule - an acknowledgement, the end of a
short answer, a greeting; once per reply at most, underdo it. Otherwise plain and
unhurried, no theatrics.

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
        # Shuffled, not in order: a new server process is spawned often enough
        # that always starting at the first phrase made it the only one heard.
        self._phrases = list(config.acknowledgements)
        random.shuffle(self._phrases)
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


QUESTION_OPENERS = (
    "what", "when", "where", "who", "why", "how", "which", "whose",
    "is", "are", "was", "were", "do", "does", "did", "can", "could",
    "will", "would", "should", "shall", "have", "has", "am",
)  # fmt: skip


def probably_needs_work(text: str) -> bool:
    """Whether an utterance is likely to send the agent off doing something.

    Used to decide whether to arm the holding line. "Okay", "thanks" and "yeah"
    need no reply, and speaking "Working on it, sir" at them is worse than
    saying nothing - it answers something that was not a request.
    """
    words = text.strip().split()
    return len(words) >= 3 or text.strip().endswith("?")


def age_seconds(item: dict, now: datetime | None = None) -> float | None:
    """How long ago an utterance was recorded, or None if it cannot be told."""
    try:
        at = datetime.fromisoformat(str(item["at"]))
    except (KeyError, TypeError, ValueError):
        return None
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return max(0.0, ((now or datetime.now(UTC)) - at).total_seconds())


def looks_like_a_question(text: str) -> bool:
    """Whether an utterance was plainly asking for an answer.

    Used to decide whether going quiet was a mistake worth mentioning. Most
    silence is correct - background talk, a fragment - but a question that got
    no spoken reply is almost always the agent forgetting to say() it, so only
    questions are worth chasing.
    """
    stripped = text.strip().lower()
    if not stripped:
        return False
    if stripped.endswith("?"):
        return True
    first = stripped.split()[0].strip(",.!'\"") if stripped.split() else ""
    return first in QUESTION_OPENERS


def build_server(config: Config | None = None, client: VoiceClient | None = None):
    """Construct the MCP server. Import is deferred so the CLI stays fast."""
    from mcp.server.mcpserver import MCPServer

    from . import __version__

    config = config or Config.load()
    voice = client or VoiceClient(config.service)
    # Nothing is heard before the agent first asks, so start from "now" rather
    # than replaying whatever was said before it connected.
    cursor = _initial_cursor(voice)
    # A question that got no spoken reply, so it can be raised once. Only
    # questions: most silence is correct, and nagging an agent that is rightly
    # keeping quiet just pushes it into answering things nobody asked.
    unanswered_question: str | None = None
    quiet_calls = 0
    # This process is spawned when the client starts, which can be a long time
    # before anyone asks it to listen. See wait_for_speech.
    first_listen = True
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
        nonlocal cursor, unanswered_question, quiet_calls, first_listen

        # Calling this again means the agent has moved on, so no holding line.
        # There is nothing to chase it about: with no wake word, most utterances
        # deserve no reply, and a server cannot tell a correct silence from a
        # forgotten one.
        acknowledger.cancel()

        # "Start listening" means from now, not from whenever the client
        # happened to launch this process. Anything said in between was said to
        # nobody. Only on the first call: after that a queued utterance is one
        # spoken while the agent was busy, which is exactly what it must not
        # miss.
        if first_listen:
            first_listen = False
            cursor = max(cursor, _initial_cursor(voice))

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
            # Identical empty results in a row look like a stuck loop to a client
            # counting consecutive failures, so say how long has been spent and
            # be explicit that this is the normal idle case rather than an error.
            quiet_calls += 1
            waited = int(quiet_calls * config.service.max_wait_seconds)
            return {
                "heard": [],
                "waited_seconds": waited,
                "next_step": (
                    f"Not an error - the user has simply been quiet for {waited}s. "
                    "This is the expected idle result. Call wait_for_speech again; "
                    "it will return the moment they speak."
                ),
            }
        quiet_calls = 0

        # Age travels with each utterance. Nothing is dropped for being old -
        # the agent judges, as it does with everything else here - but "said
        # twenty minutes ago" is the difference between a live request and a
        # leftover, and it cannot be inferred from the text.
        now = datetime.now(UTC)
        newest_age = None
        for item in heard:
            age = age_seconds(item, now)
            if age is not None:
                item["said_seconds_ago"] = int(age)
                newest_age = age

        spoken_text = [item["text"] for item in heard]
        missed, unanswered_question = unanswered_question, None
        last = spoken_text[-1]
        if looks_like_a_question(last):
            unanswered_question = last
        # Only when there is plausibly work to do. Arming on "okay" or "thanks"
        # produces a holding line for a reply that was never coming.
        if probably_needs_work(last):
            acknowledger.arm()

        # The judgement call is restated here, not only in the server
        # instructions, because a tool result lands in context immediately
        # before the model replies - the moment it decides whether to speak.
        payload = {
            "heard": spoken_text,
            "next_step": (
                "Meant for you? Do the work, then say() the answer. Answering IS "
                "calling say() - there is no other way to reach them, and the next "
                "tool you call must be say(), not wait_for_speech. "
                "Not meant for you (background talk, a fragment that is not a "
                "request)? Stay silent and call wait_for_speech again. "
                "Cut off mid sentence? Call wait_for_speech again; the rest is "
                "already queued."
            ),
            "detail": heard,
        }
        stale_after = config.service.stale_after_seconds
        if stale_after > 0 and newest_age is not None and newest_age > stale_after:
            payload["stale"] = (
                f"This was said {int(newest_age)}s ago, while nobody was listening. "
                "Treat it as a leftover rather than a live request: unless it plainly "
                "still needs doing, stay silent and call wait_for_speech again."
            )
        if missed:
            payload["unanswered"] = (
                f'You never spoke an answer to "{missed}". If you worked one out, '
                "say() it now along with anything new."
            )
        return payload

    @server.tool(
        name="check_for_speech",
        title="Check for anything said while you were working",
        description=(
            "Returns immediately with anything the user has said since you last "
            "looked, or nothing if they have been quiet. Does NOT block. Call it "
            "between the steps of any long task - they cannot interrupt you, so "
            "this is the only way a change of mind reaches you before you finish "
            "doing the wrong thing."
        ),
    )
    def check_for_speech() -> dict:
        # The non-blocking twin of wait_for_speech. Nothing can preempt an agent
        # mid-turn, so steering only works if the agent chooses to look.
        nonlocal cursor
        try:
            result = voice.heard(since=cursor, wait=0, settle=0)
        except ServiceUnavailable as exc:
            return {"error": str(exc), "heard": []}

        heard = result.get("heard", [])
        cursor = result.get("cursor", cursor)
        if not heard:
            return {"heard": [], "next_step": "Nothing new. Carry on with what you were doing."}
        return {
            "heard": [item["text"] for item in heard],
            "next_step": (
                "The user spoke while you were working. Read it before continuing: "
                "they may be redirecting you, correcting a detail, or telling you to "
                "stop. Acknowledge it with say() and act on it - carrying on with the "
                "old plan after they have changed it wastes both your time. If it was "
                "not meant for you, ignore it and carry on."
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
        nonlocal unanswered_question
        acknowledger.cancel()
        try:
            voice.say(text)
        except ServiceUnavailable as exc:
            return {"error": str(exc), "spoken": False}
        unanswered_question = None
        return {
            "spoken": True,
            "text": text,
            "next_step": (
                "Spoken. Do not stop here - they are still listening, and a turn "
                "that ends after say() looks to them like you walked off mid "
                "conversation. Call wait_for_speech now. It is the only way their "
                "reply reaches you, and it is how the loop stays open."
            ),
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
