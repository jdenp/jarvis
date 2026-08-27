"""MCP server, so an agent knows it has ears and a voice.

A client of a running `jarvis serve`, not a second copy - only one process may
own the microphone. DESIGN.md has the reasoning behind the blocking read.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Literal

from .client import ServiceUnavailable, VoiceClient
from .config import Config

logger = logging.getLogger("jarvis.mcp")

INSTRUCTIONS = """\
A microphone and a voice on the user's desktop.

OFF BY DEFAULT. Being connected does not mean voice is wanted. Do not call these
tools or start the service until asked. "jarvis" on its own means start listening
now - stay_silent(because="starting_to_listen") at once, no text reply, nothing
else first. So do
"listen", "use voice", "talk to me". It ends when they say so or go back to typing.

THE LOOP IS THE TOOL, NOT YOUR MEMORY. say() takes a required `then`:

    say(answer,  then="listen")        speaks, then blocks and returns their reply
    say(lead_in, then="keep_working")  speaks and returns at once, so you can work

Answering and listening again are therefore ONE CALL. You cannot answer and then
forget to listen - say() has already done it, and their next words are in the
result you are reading.

stay_silent is the other thing you can do with a turn: say nothing. It listens
the same way, minus the speaking, and it takes a required `because`:

    starting_to_listen | not_aimed_at_me | sounded_cut_off | already_spoke_my_reply

The last one is checked against what actually went through the speakers. Write
your reply as text, come here claiming you answered, and the call is refused.

THREE RULES:

1. ANSWERING IS CALLING say(). NOTHING ELSE REACHES THEM!!

   They are LISTENING, NOT READING. They cannot see your chat, your thinking or
   your task result. Text you write goes NOWHERE.

   *** DECIDING TO SAY IT IS NOT SAYING IT! *** If you catch yourself thinking
   "I should reply via say()" - STOP. Do not then write the reply out. EMIT THE
   TOOL CALL. Writing the words instead of calling say() is the single most
   common failure with these tools and is IDENTICAL TO IGNORING THEM.

   The moment you know what to tell them, your very next tool call is say().
   Not prose. Not one more search. If you have composed a sentence for the user,
   it belongs in say(), not in your reply text - you may only write it out AFTER
   the tool call has gone. Say it when a tool fails too: four failed searches
   then silence reads as a crash.

2. Silence is a valid reply. No wake word, so you hear everything - other people,
   videos, thinking aloud. Act only on what was aimed at you; for anything else
   stay_silent(because="not_aimed_at_me"). Answering what nobody asked is
   worse than missing one.

3. If it sounds cut off, listen again - do not ask them to repeat it. A phrase
   ends after a fixed silence, not when the speaker finishes, so a mid sentence
   pause splits one request in two and the rest is already queued. Ending mid
   clause, a verb with nothing to act on, or a reference to something never
   mentioned all mean the other half is a moment away. Only ask if it is still
   incomplete the second time.

BEFORE ANYTHING SLOW, SPEAK FIRST. One question decides it, and it is the same
question that picks `then`:

   CAN I ANSWER THIS RIGHT NOW, FROM WHAT I ALREADY KNOW?

   YES -> say(the answer, then="listen"). Done, and you are listening again.
   NO, it needs a search, a file, a command, anything at all ->
        say("Let me have a look, sir.", then="keep_working")  <- before you start
        ...then do the work, then say(the real answer, then="listen").

That one line is all they need. Guess wrong towards speaking: they cannot see
your screen, and silence is indistinguishable from a crash.

WHILE YOU WORK: call check_for_speech between steps of a long task - it returns
instantly and is the only way "actually, do it the other way" reaches you before
you have finished doing it the first way.

SPOKEN REPLIES are read aloud: under forty words, no markdown, never read code or
long paths. Say "sir" as a tendency not a rule - an acknowledgement, the end of a
short answer, a greeting; once per reply at most, underdo it. Otherwise plain and
unhurried, no theatrics.

Either tool returning nothing heard means they have not spoken yet. Call
stay_silent(because="already_spoke_my_reply") again."""


QUESTION_OPENERS = (
    "what", "when", "where", "who", "why", "how", "which", "whose",
    "is", "are", "was", "were", "do", "does", "did", "can", "could",
    "will", "would", "should", "shall", "have", "has", "am",
)  # fmt: skip

# Why a listen is not a say(). Naming it is the point: an agent that has just
# written its reply out as prose has no honest answer here but
# "already_spoke_my_reply", and that is the one the server can check.
SilenceReason = Literal[
    "starting_to_listen",
    "not_aimed_at_me",
    "sounded_cut_off",
    "already_spoke_my_reply",
]

# Read at the moment the model picks its next tool call, so it says what to call
# and little else. Length was the problem: the decision used to sit in the middle
# of five competing clauses, and `detail` came after it, so the last thing read
# before deciding was a copy of `heard` with timestamps. next_step goes last now.
ANSWER_OR_STAY_QUIET = (
    "Can you answer right now, from what you know? "
    'say(it, then="listen") - that speaks AND listens, so their reply comes back here. '
    'Needs a search, a file, a command? say(one line, then="keep_working") FIRST, in '
    "your own words, so they know you heard: let me take a look, one moment. Then the "
    'work, however many tool calls it takes, and say(the answer, then="listen"). '
    "NOTHING YOU WRITE REACHES THEM, ONLY say(). "
    "Not for you, or cut off mid sentence? stay_silent, and say why."
)


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
    # The last thing heard. Cleared only by say(..., then="listen"), so while it
    # is set the agent owes a reply - and "I already replied" is then a claim the
    # server can weigh against what actually went through the speakers.
    unanswered: str | None = None
    quiet_calls = 0
    first_listen = True

    server = MCPServer(
        name="jarvis",
        title="JARVIS voice",
        version=__version__,
        instructions=INSTRUCTIONS,
    )

    def listen(wait: float) -> dict:
        """One turn of listening, shared by stay_silent and say(then="listen")."""
        nonlocal cursor, unanswered, quiet_calls, first_listen

        # "Start listening" means from now, not from whenever the client
        # launched this process. First call only, or speech queued while the
        # agent was busy would be skipped too.
        if first_listen:
            first_listen = False
            cursor = max(cursor, _initial_cursor(voice))

        try:
            result = voice.heard(since=cursor, wait=wait)
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
                    "This is the expected idle result. Call stay_silent again; "
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
        unanswered = spoken_text[-1]
        payload = {"heard": spoken_text, "detail": heard}
        stale_after = config.service.stale_after_seconds
        if stale_after > 0 and newest_age is not None and newest_age > stale_after:
            payload["stale"] = (
                f"This was said {int(newest_age)}s ago, while nobody was listening. "
                "Treat it as a leftover rather than a live request: unless it plainly "
                "still needs doing, keep quiet and call stay_silent again."
            )
        payload["next_step"] = ANSWER_OR_STAY_QUIET
        return payload

    @server.tool(
        name="stay_silent",
        title="Say nothing, and listen for what comes next",
        description=(
            "Decide to say nothing, and block until the user speaks. Listens exactly "
            'as say(text, then="listen") does, minus the speaking. Two uses and no '
            "others: entering the conversation, and listening again after judging that "
            "a reply was not wanted. IT IS NOT HOW YOU ANSWER.\n\n"
            "`because` is required. Choosing this over say() leaves them sitting in "
            "silence, and only a few things make that right:\n"
            "  starting_to_listen     entering voice mode; nothing said yet\n"
            "  not_aimed_at_me        heard, but not addressed to you - stay quiet\n"
            "  sounded_cut_off        the rest of the sentence is still coming\n"
            "  already_spoke_my_reply you have called say() and want to hear more\n\n"
            "That last one is checked. Writing your reply as text and then coming "
            "here is the failure this argument exists to catch - nothing you write "
            "reaches them, so the call is refused and you are sent back to say().\n\n"
            "Waits as long as it can - there is no timeout to tune. Nothing heard "
            "means the client's own limit was reached, not the user's patience: call "
            "it again. There is no wake word, so some of what comes back will not be "
            "for you."
        ),
    )
    def stay_silent(because: SilenceReason) -> dict:
        # No timeout argument on purpose. Given one, models pick a small number
        # and give up while the user is still deciding what to say.
        nonlocal unanswered

        # The one claim here that is checkable, and the only hard refusal: it did
        # not speak, so whatever it thinks it replied went nowhere. Every other
        # reason still goes through, so this cannot deadlock - the way out is in
        # the enum rather than in a retry counter.
        if unanswered is not None and because == "already_spoke_my_reply":
            return {
                "heard": [],
                "listening": False,
                "next_step": (
                    f'You have not called say() since they said "{unanswered}", so '
                    "nothing you wrote was heard and this call did not listen. Take the "
                    'words you just wrote and pass them to say(text, then="listen") - '
                    "that speaks them AND listens, so you lose nothing by doing it "
                    "properly. If they really were not talking to you, come back with "
                    "not_aimed_at_me and this will go through."
                ),
            }

        # Bounced, not blocked. Refusing outright deadlocks a session against an
        # agent that has correctly decided to keep quiet, so this costs one cheap
        # round trip and the next call goes through either way.
        if unanswered is not None and looks_like_a_question(unanswered):
            missed, unanswered = unanswered, None
            return {
                "heard": [],
                "listening": False,
                "next_step": (
                    f'You were asked "{missed}" and never spoke an answer, so this call '
                    'did not listen. Worked one out? say(it, then="listen") now - that '
                    "speaks and listens in one go, so answering costs you nothing. Still "
                    "sure it was not for you, or that it was cut off? Call stay_silent "
                    "again and it will go through."
                ),
            }
        return listen(config.service.max_wait_seconds)

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
                'stop. Acknowledge it with say(..., then="keep_working") and act on it '
                "- carrying on with the old plan after they have changed it wastes both "
                "your time. If it was not meant for you, ignore it and carry on."
            ),
            "detail": heard,
        }

    @server.tool(
        name="say",
        title="Speak to the user, and listen for the reply",
        description=(
            "Speak text aloud through the user's speakers. Every answer and every "
            "outcome goes through here - the user is listening, not reading, so a "
            "reply you only write down never reaches them. "
            "`then` is required, and it asks what you were deciding anyway: is this "
            "the reply, or a holding line before some work? "
            'then="listen" means this IS the reply. It speaks, then blocks for what '
            "they say next and returns it, exactly as stay_silent would, so the "
            'conversation carries on by itself. then="keep_working" means a lead-in '
            "before a search, a file or a command. It returns at once so you can get "
            'on with it, and the real answer follows with then="listen". '
            "Keep it short and plain: no markdown, lists or emoji, they get read out "
            "literally. The microphone is muted while speaking, so JARVIS does not "
            "transcribe itself."
        ),
    )
    def say(text: str, then: Literal["listen", "keep_working"]) -> dict:
        nonlocal unanswered
        try:
            voice.say(text)
        except ServiceUnavailable as exc:
            logger.warning("Say failed - %s", exc)
            return {"error": str(exc), "spoken": False}
        if then == "keep_working":
            # The reply is still owed. By its own declaration this line was not
            # it, so a lead-in followed by silence is still caught.
            logger.info("Spoke a lead-in, not listening yet.")
            return {
                "spoken": True,
                "text": text,
                "next_step": (
                    "Spoken, and NOT listening - you have just told them you would go "
                    "and look, so go and do it now. However many tool calls it takes, "
                    'then say(the answer, then="listen").'
                ),
            }
        unanswered = None
        # Speech plays on the service's own thread and the microphone stays muted
        # until it finishes, so listening from here costs nothing extra - and it
        # cannot hear anyone who replies before JARVIS has stopped talking, which
        # was equally true of say() followed by stay_silent.
        logger.info("Spoke, now listening for the reply.")
        return {"spoken": True, "text": text, **listen(config.service.max_wait_seconds)}

    @server.tool(
        name="pause_transcription",
        title="Stop listening",
        description=(
            "Stop listening. The microphone stops being read, so nothing is "
            "transcribed, logged or recorded until it is resumed - not merely "
            "withheld from you. Press the "
            f"{config.service.hotkey} key to toggle this from anywhere. "
            "Call resume_transcription to start listening again."
        ),
    )
    def pause_transcription() -> dict:
        try:
            voice.pause()
            logger.info("Listening paused.")
            return {"paused": True, "message": "Listening paused."}
        except ServiceUnavailable as exc:
            logger.warning("Pause failed - %s", exc)
            return {"error": str(exc), "paused": False}

    @server.tool(
        name="resume_transcription",
        title="Start listening again",
        description=(
            "Start reading the microphone again after a pause. Nothing said "
            "during the pause is recoverable - it was never captured."
        ),
    )
    def resume_transcription() -> dict:
        try:
            voice.resume()
            logger.info("Listening resumed.")
            return {"paused": False, "message": "Listening resumed."}
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
