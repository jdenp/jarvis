"""MCP server, so an agent knows it has ears and a voice.

A client of a running `jarvis serve`, not a second copy - only one process may
own the microphone. DESIGN.md has the reasoning behind the blocking read.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from .client import ServiceUnavailable, VoiceClient
from .config import Config

logger = logging.getLogger("jarvis.mcp")

INSTRUCTIONS = """\
A microphone and a voice on the user's desktop.

OFF BY DEFAULT. Being connected does not mean voice is wanted. Do not call these
tools or start the service until asked. "jarvis" on its own means start listening
now - call wait_for_speech immediately, no text reply, nothing else first. So do
"listen", "use voice", "talk to me". It ends when they say so or go back to typing.

THE LOOP: wait_for_speech -> say() -> wait_for_speech
                                      ^^^^^^^^^^^^^^^
ALWAYS STRAIGHT BACK TO wait_for_speech! No "anything else?", no written recap.
NEVER finish or complete the task after a reply - voice is ONE LONG CONVERSATION,
not a task per sentence, and completing hangs up on someone still sitting at the
microphone. The loop ends when THEY end it.

THREE RULES:

1. ANSWERING IS CALLING say(). NOTHING ELSE REACHES THEM!!

   They are LISTENING, NOT READING. They cannot see your chat, your thinking or
   your task result. Text you write goes NOWHERE.

   *** DECIDING TO SAY IT IS NOT SAYING IT! *** If you catch yourself thinking
   "I should reply via say()" - STOP. Do not then write the reply out. EMIT THE
   TOOL CALL. Writing the words instead of calling say() is the single most
   common failure with these tools and is IDENTICAL TO IGNORING THEM.

   The moment you know what to tell them, your very next tool call is say().
   Not prose. Not wait_for_speech. Not one more search. NOT COMPLETION. Say it
   when a tool fails too: four failed searches then silence reads as a crash.

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

BEFORE ANYTHING SLOW, SPEAK FIRST. One question decides it:

   CAN I ANSWER THIS RIGHT NOW, FROM WHAT I ALREADY KNOW?

   YES -> say(the answer). Done.
   NO, it needs a search, a file, a command, anything at all ->
        say("Let me have a look, sir.")  <- first, before you start
        ...then do the work, then say() the real answer.

That one line is all they need. Guess wrong towards speaking: they cannot see
your screen, and silence is indistinguishable from a crash.

WHILE YOU WORK: call check_for_speech between steps of a long task - it returns
instantly and is the only way "actually, do it the other way" reaches you before
you have finished doing it the first way.

SPOKEN REPLIES are read aloud: under forty words, no markdown, never read code or
long paths. Say "sir" as a tendency not a rule - an acknowledgement, the end of a
short answer, a greeting; once per reply at most, underdo it. Otherwise plain and
unhurried, no theatrics.

wait_for_speech returning nothing means they have not spoken yet. Call it again."""


QUESTION_OPENERS = (
    "what", "when", "where", "who", "why", "how", "which", "whose",
    "is", "are", "was", "were", "do", "does", "did", "can", "could",
    "will", "would", "should", "shall", "have", "has", "am",
)  # fmt: skip


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

    Only questions are chased up when no reply follows - most silence is
    correct, so anything less clear-cut is left alone.
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
    cursor = _initial_cursor(voice)
    # A question that got no spoken reply, raised once on the next call.
    unanswered_question: str | None = None
    quiet_calls = 0
    first_listen = True

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

        # "Start listening" means from now, not from whenever the client
        # launched this process. First call only, or speech queued while the
        # agent was busy would be skipped too.
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
            # Identical empty results in a row read as a stuck loop to a
            # client counting consecutive failures, so make each one differ.
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

        # Nothing is dropped for being old, but "said twenty minutes ago" is
        # the difference between a request and a leftover, and is not in the text.
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
        # Read at the moment the model picks its next tool call, so it says what
        # to call and little else. Length was the problem: the decision used to sit
        # in the middle of five competing clauses, and `detail` came after it, so
        # the last thing read before deciding was a copy of `heard` with
        # timestamps. next_step goes last now.
        payload = {"heard": spoken_text, "detail": heard}
        next_step = (
            "Can you answer right now? say() it. "
            "If it needs a search, a file, a command - say one short line FIRST, in "
            "your own words, so they know you heard: let me take a look, one moment, "
            "let me check that. Then do the work, however many tool calls it takes, "
            "and say() the answer when you have it. "
            "NOTHING YOU WRITE REACHES THEM, ONLY say(). "
            "Not for you, or cut off mid sentence? wait_for_speech again."
        )
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
        payload["next_step"] = next_step
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
        nonlocal cursor
        try:
            result = voice.heard(since=cursor, wait=0)
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
        try:
            voice.say(text)
        except ServiceUnavailable as exc:
            logger.warning("Say failed - %s", exc)
            return {"error": str(exc), "spoken": False}
        unanswered_question = None
        return {
            "spoken": True,
            "text": text,
            "next_step": (
                "Spoken. Was that the answer? NOW CALL wait_for_speech - do NOT "
                "finish the task, they are still listening. Was it a lead-in, you "
                "saying you would go and look? THEN GO AND DO IT NOW - do not "
                "listen yet, and say() the real answer when you have it."
            ),
        }

    @server.tool(
        name="pause_transcription",
        title="Pause transcription",
        description=(
            "Pause transcription so new utterances are not recorded. The microphone "
            "keeps running but the transcript ignores what comes in. Press the Pause "
            "key on your keyboard to toggle this from anywhere. Call again to resume."
        ),
    )
    def pause_transcription() -> dict:
        try:
            result = voice.pause()
            logger.info("Transcription paused.")
            return {"paused": True, "message": "Transcription paused."}
        except ServiceUnavailable as exc:
            logger.warning("Pause failed - %s", exc)
            return {"error": str(exc), "paused": False}

    @server.tool(
        name="resume_transcription",
        title="Resume transcription",
        description=(
            "Resume recording utterances after a pause. The transcript will again "
            "capture everything the user says."
        ),
    )
    def resume_transcription() -> dict:
        try:
            voice.resume()
            logger.info("Transcription resumed.")
            return {"paused": False, "message": "Transcription resumed."}
        except ServiceUnavailable as exc:
            logger.warning("Resume failed - %s", exc)
            return {"error": str(exc), "paused": True}

    @server.tool(
        name="voice_status",
        title="Check the voice service",
        description="Report whether the microphone is live and which backends are in use.",
    )
    def voice_status() -> dict:
        try:
            return voice.status()
        except ServiceUnavailable as exc:
            logger.warning("Status check failed - %s", exc)
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

    # Covers the client being killed rather than closing the pipe, where the
    # launch chain holds the pipe open and we would sit here forever.
    exit_when_orphaned()
    build_server(config).run(transport="stdio")
    return 0
