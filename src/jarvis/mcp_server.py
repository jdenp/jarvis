"""MCP server, so an agent knows it has ears and a voice.

A client of a running `jarvis serve`, not a second copy - only one process may
own the microphone. DESIGN.md has the reasoning behind the blocking read.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Literal

# At module level rather than deferred: the SDK resolves tool annotations from
# module globals, so a `ctx: Context` parameter cannot see a local import. The
# CLI only imports this module for the `mcp` command, so nothing else pays.
import pydantic_core
from mcp.server.mcpserver import Context, MCPServer
from mcp_types import CallToolResult, TextContent

from .client import ServiceUnavailable, VoiceClient
from .config import Config
from .screen import Screen, ScreenUnavailable, means_the_same

logger = logging.getLogger("jarvis.mcp")

INSTRUCTIONS = """\
A microphone and a voice on the user's desktop.

OFF BY DEFAULT. Being connected does not mean voice is wanted. Do not call these
tools or start the service until asked. "jarvis" on its own means start listening
now - converse(say="", then="listen") at once. Silently: no greeting, no text
reply, nothing else first. So do
"listen", "use voice", "talk to me". It ends when they say so or go back to typing.

ONE TOOL DOES THE WHOLE LOOP: converse(). It speaks, then blocks and returns
what they say next. Every turn of the conversation is one call to it.

    converse(say="Ten thousand.",  then="listen")       speak, then hear the reply
    converse(say="One moment.",    then="keep_working")  speak, return now, go work
    converse(say="",               then="listen")        say nothing, just listen

There is no separate listen tool to forget, and no second call to drop: the reply
you are about to read came back from the same call that spoke. Every turn looks
exactly like the last one, including the first.

say="" while you owe them an answer is refused. You cannot claim you replied when
nothing went through the speakers.

THREE RULES:

1. ANSWERING IS CALLING converse(). NOTHING ELSE REACHES THEM!!

   They are LISTENING, NOT READING. They cannot see your chat, your thinking or
   your task result. Text you write goes NOWHERE.

   *** DECIDING TO SAY IT IS NOT SAYING IT! *** If you catch yourself thinking
   "I should greet them back" or "I should reply" - that thought is not the
   reply. Do not write it out. EMIT converse(). Writing the words instead of
   calling converse() is the single most common failure with these tools and is
   IDENTICAL TO IGNORING THEM. It happens most on the easy ones: a greeting
   feels too small to need a tool, and it needs one exactly as much as anything
   else does.

   The moment you know what to tell them, your very next output is converse().
   Not prose. Not one more search. If you have composed a sentence for the user,
   it belongs in converse(say=...), and you may only write it out AFTER the call
   has gone. Say it when a tool fails too: four failed searches then silence
   reads as a crash.

2. Silence is a valid reply. No wake word, so you hear everything - other people,
   videos, thinking aloud. Act only on what was aimed at you; for anything else
   converse(say="", then="listen"). Answering what nobody asked is worse than
   missing one.

3. If it sounds cut off, listen again - do not ask them to repeat it. A phrase
   ends after a fixed silence, not when the speaker finishes, so a mid sentence
   pause splits one request in two and the rest is already queued. Ending mid
   clause, a verb with nothing to act on, or a reference to something never
   mentioned all mean the other half is a moment away. Only ask if it is still
   incomplete the second time.

BEFORE ANYTHING SLOW, SPEAK FIRST. One question decides it, and it is the same
question that picks `then`:

   CAN I ANSWER THIS RIGHT NOW, FROM WHAT I ALREADY KNOW?

   YES -> converse(say=the answer, then="listen"). Done, and listening again.
   NO, it needs a search, a file, a command, anything at all ->
        converse(say="Let me have a look, sir.", then="keep_working")  <- first
        ...then the work, then converse(say=the answer, then="listen").

That one line is all they need. Guess wrong towards speaking: they cannot see
your screen, and silence is indistinguishable from a crash.

WHILE YOU WORK: call check_for_speech between steps of a long task - it returns
instantly and is the only way "actually, do it the other way" reaches you before
you have finished doing it the first way.

SPOKEN REPLIES are read aloud: under forty words, no markdown, never read code or
long paths. Say "sir" as a tendency not a rule - an acknowledgement, the end of a
short answer, a greeting; once per reply at most, underdo it. Otherwise plain and
unhurried, no theatrics.

Nothing heard means they have not spoken yet, not that anything is wrong.
converse(say="", then="listen") again."""


# Appended only when screen.control is on, because until it is there is nothing
# here an agent can do. Same shape as the rest: the constraint is in the
# signature, and this only says what the signature will not let it get wrong.
SCREEN_INSTRUCTION = """

YOU ALSO HAVE HANDS. look_at_screen numbers everything clickable in a window and
gives you the numbers - never coordinates, and there is nothing to measure.

    click(target=12, expecting="Reply")

`expecting` is the label you read beside that number, and it is checked. Get it
wrong, or use a number from a scan the screen has moved on from, and the click is
refused rather than landing on whatever took its place. Look again after anything
you do: acting changes the numbers.

This is the user's real pointer on their real desktop. Say what you are about to
do before you do it, and say what happened after - and saying means converse(),
same as everything else."""


QUESTION_OPENERS = (
    "what", "when", "where", "who", "why", "how", "which", "whose",
    "is", "are", "was", "were", "do", "does", "did", "can", "could",
    "will", "would", "should", "shall", "have", "has", "am",
)  # fmt: skip

# The last thing read before the model picks its next output, so it is an
# instruction and nothing else. Three things were wrong with the version before
# it: `detail` sat between this and the decision, it opened with a question -
# which a model answers in prose, because that is what questions are for - and
# it ended on the name of the tool that does not speak. It now opens and closes
# on the same imperative, and there is nothing else in the result to compete.
REPLY_WITH_CONVERSE = (
    "Your next output is a tool call, not prose. They are LISTENING, NOT READING: "
    "anything you type is discarded unread, including a greeting."
    + "\n"
    + '  converse(say="the answer", then="listen")        you can answer now'
    + "\n"
    + '  converse(say="one moment, sir", then="keep_working")  it needs a look first'
    + "\n"
    + '  converse(say="", then="listen")                  it was not aimed at you'
    + "\n"
    + "Short and easy is still a tool call. Do not write the reply out. "
    "EMIT converse()."
)


def as_error(payload: dict) -> CallToolResult:
    """The same payload, flagged so the client will not let the turn end on it.

    A fallback for models that forget the call. Nothing in MCP can require a
    tool call to happen - see DESIGN.md - but an agent that will end its turn on
    a tool result will not end it on a tool *error*: an error is the one signal
    every client treats as unfinished business rather than as an answer. A model
    that reliably calls converse() never sees this and is unaffected; a
    forgetful one gets the shove it needs.

    It is a deliberate lie about a call that succeeded. Two things keep it
    honest enough to live with. It only fires when the user actually spoke and a
    reply is genuinely outstanding, so an idle poll is still a plain success.
    And `service.force_a_reply` switches it off, for a client that counts
    consecutive errors and gives up rather than pressing on.
    """
    body = pydantic_core.to_json(payload, fallback=str, indent=2).decode()
    return CallToolResult(content=[TextContent(type="text", text=body)], is_error=True)


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

    It used to decide whether an unanswered utterance was chased at all, which
    let "Hey Jarvis" through - so everything is chased now and this only picks
    the wording, asked against said.
    """
    stripped = text.strip().lower()
    if not stripped:
        return False
    if stripped.endswith("?"):
        return True
    first = stripped.split()[0].strip(",.!'\"") if stripped.split() else ""
    return first in QUESTION_OPENERS


def build_server(
    config: Config | None = None,
    client: VoiceClient | None = None,
    screen: Screen | None = None,
):
    """Construct the MCP server."""
    from . import __version__, hands

    config = config or Config.load()
    voice = client or VoiceClient(config.service)
    desktop = screen or Screen(config.screen)
    cursor = _initial_cursor(voice)
    # The last thing heard. Cleared only by say(..., then="listen"), so while it
    # is set the agent owes a reply - and "I already replied" is then a claim the
    # server can weigh against what actually went through the speakers.
    unanswered: str | None = None
    quiet_calls = 0
    first_listen = True
    complained_about_marks = False
    logged_capabilities = False

    server = MCPServer(
        name="jarvis",
        title="JARVIS voice",
        version=__version__,
        instructions=INSTRUCTIONS + (SCREEN_INSTRUCTION if config.screen.control else ""),
    )

    def listen(wait: float) -> dict:
        """The listening half of converse(), and the whole of it when say is empty."""
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
                    "This is the expected idle result. Call "
                    'converse(say="", then="listen") again; it returns the moment '
                    "they speak."
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
        # No `detail`. It was eight lines of id and timestamp for a two word
        # greeting, sitting between the words and the instruction, and nothing
        # ever read it - the one thing it carried that mattered is below.
        payload = {"heard": spoken_text}
        stale_after = config.service.stale_after_seconds
        if stale_after > 0 and newest_age is not None and newest_age > stale_after:
            payload["stale"] = (
                f"This was said {int(newest_age)}s ago, while nobody was listening. "
                "Treat it as a leftover rather than a live request: unless it plainly "
                'still needs doing, keep quiet - converse(say="", then="listen").'
            )
        payload["next_step"] = REPLY_WITH_CONVERSE
        return payload

    @server.tool(
        name="converse",
        title="Speak, and hear what they say next",
        description=(
            "One turn of the conversation. Speaks `say` aloud, then blocks until the "
            "user speaks and returns what they said. Speaking and listening are the "
            "same call, so there is no second call to forget and no other tool to "
            "choose - every turn of a voice session is this one, including the "
            "first.\n\n"
            "`say` is required and it is the whole point. The user is LISTENING, NOT "
            "READING: text you write into your reply is discarded unread, so a reply "
            "that is not in `say` was never delivered. That is true of short easy "
            "ones too - a greeting feels too small to spend a tool call on and needs "
            "one exactly as much as anything else.\n\n"
            'say="" listens without speaking. Two honest uses: entering voice mode, '
            "and hearing something that was not aimed at you - there is no wake word, "
            "so some of what comes back is other people, videos, or thinking aloud. "
            "It is refused if you owe them an answer, because then it is a claim to "
            "have replied when nothing went through the speakers.\n\n"
            '`then` is the other decision. then="listen" means this IS the reply; it '
            'waits for what comes next. then="keep_working" means a holding line '
            "before a search, a file or a command, and returns at once so you can get "
            "on with it, however many tool calls that takes, and the real answer "
            "follows in the next converse(). Compose that line in your own words - one "
            "fixed phrase every time sounds like a machine. Guess wrong towards "
            "speaking: they cannot see your screen, and silence is indistinguishable "
            "from a crash.\n\n"
            "Read aloud by a synthesiser, so keep it short and plain: no markdown, "
            "lists or emoji, they get pronounced. Waits as long as it can and there is "
            "no timeout to tune; nothing heard means the client's own limit was "
            "reached, not the user's patience."
        ),
    )
    def converse(say: str, then: Literal["listen", "keep_working"], ctx: Context):
        nonlocal unanswered, logged_capabilities
        spoken = say.strip()

        # Once per session, because it decides what is even possible here. A
        # client advertising `sampling` can be asked to run a completion, which
        # is the only mechanism in MCP that lets the server obtain model output
        # rather than wait to be called - see DESIGN.md on what cannot be
        # enforced. Without it there is no way to make a reply happen.
        if not logged_capabilities:
            logged_capabilities = True
            logger.info("Client capabilities: %s", _capabilities(ctx))

        # The one refusable claim, and the reason `say` is a string rather than
        # an enum of excuses: an empty one while a reply is owed IS the claim to
        # have answered, and it is checkable against what actually went through
        # the speakers. Bounced rather than blocked - the second call goes
        # through either way, so an agent that has correctly decided to keep
        # quiet cannot be wedged.
        if not spoken and unanswered is not None:
            missed, unanswered = unanswered, None
            # Any unanswered utterance, not only the ones that parse as
            # questions. "Hey Jarvis" is not a question and is exactly what went
            # unanswered in practice. It self-clears, so the cost of being wrong
            # is one cheap round trip and the next call goes through - which is
            # what keeps a room with a television in it from wedging.
            asked = "asked" if looks_like_a_question(missed) else "said"
            # An error whatever `force_a_reply` says, because this one is not a
            # white lie: the call was asked to listen and refused.
            return as_error(
                {
                    "spoke": False,
                    "heard": [],
                    "error": (
                        f'They {asked} "{missed}" and nothing has gone through the '
                        "speakers since, so this call did not listen. Whatever you were "
                        "about to type, or just typed, put it in "
                        'converse(say=..., then="listen") - that speaks it AND listens, '
                        "so replying properly costs you nothing. If it really was not "
                        "for you, call this again and it will go through."
                    ),
                }
            )

        if spoken:
            try:
                voice.say(spoken)
            except ServiceUnavailable as exc:
                logger.warning("Say failed - %s", exc)
                return {"spoke": False, "error": str(exc)}

        if then == "keep_working":
            # The reply is still owed. By its own declaration this line was not
            # it, so a lead-in followed by silence is still caught.
            logger.info("Spoke a lead-in, not listening yet.")
            return {
                "spoke": bool(spoken),
                "said": spoken,
                "next_step": (
                    "Spoken, and NOT listening - you have just told them you would go "
                    "and look, so go and do it now. However many tool calls it takes, "
                    'then converse(say=the answer, then="listen").'
                ),
            }

        if spoken:
            unanswered = None
            logger.info("Spoke, now listening for the reply.")
        # Speech plays on the service's own thread with the microphone muted
        # until it finishes, so listening from here costs nothing extra.
        result = listen(config.service.max_wait_seconds)
        if spoken:
            result = {"spoke": True, "said": spoken, **result}
        # Nothing heard means nothing is owed, so an idle poll stays a plain
        # success. That is most of them, which keeps the error rare enough to
        # still mean something when it does fire.
        if config.service.force_a_reply and result.get("heard"):
            return as_error(result)
        return result

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

    # ------------------------------------------------------------------ screen

    def marked(scan) -> str | None:
        """Write the numbered boxes onto a screenshot, if one was asked for."""
        nonlocal complained_about_marks
        if not config.screen.marks_file:
            return None
        try:
            from . import marks

            path = marks.draw(
                scan,
                desktop.backend.window_rect(scan.hwnd),
                config.log_dir / config.screen.marks_file,
            )
            return str(path)
        except (OSError, RuntimeError) as exc:
            # Once. It fails for the same reason every scan, and a warning per
            # look buries everything else in the log.
            logger.log(
                logging.WARNING if not complained_about_marks else logging.DEBUG,
                "No marked screenshot - %s",
                exc,
            )
            complained_about_marks = True
            return None

    def described(scan):
        """One scan, as the agent sees it. Never a coordinate."""
        payload = scan.as_dict(config.screen.label_chars)
        others = [title for _, title in desktop.windows() if title != scan.window]
        if others:
            payload["other_windows"] = others[:12]

        image = marked(scan)
        if image:
            payload["marked_screenshot"] = image

        if not config.screen.control:
            payload["next_step"] = (
                "Looking only - clicking and typing are switched off. Tell the user to "
                'set "screen": {"control": true} in config/jarvis.json and restart the '
                "MCP server if they want you to act on any of this."
            )
        elif scan.truncated:
            payload["next_step"] = (
                f"{scan.truncated} more targets did not fit and are not listed. If what "
                "you want is missing, call look_at_screen again with matching= a word "
                "from its label rather than guessing at a number. To act, name the "
                'number AND what you expect it to be: click(target=12, expecting="Reply").'
            )
        else:
            payload["next_step"] = (
                "Act by number, and say what you expect that number to be: "
                'click(target=12, expecting="Reply"). The label has to match or nothing '
                "is pressed, which is what stops a number left over from an older scan "
                "clicking whatever has moved into its place."
            )
        if config.screen.send_image and image:
            from mcp.server.mcpserver.utilities.types import Image

            return [payload, Image(path=image)]
        return payload

    def aim(target: int, expecting: str):
        """Resolve a number, having made the agent say what it is aiming at."""
        found, scan = desktop.aim(target)
        if not means_the_same(expecting, found.element.label):
            raise ScreenUnavailable(
                f"Target {target} is {found.element.label!r}, not {expecting!r}. Nothing "
                "was pressed. Either the number is wrong or you are working from an "
                "older scan - look at the screen again and read the ids off the new list."
            )
        return found, scan

    def acted(what: str, found, scan) -> dict:
        logger.info("%s target %d %r in %r", what, found.number, found.element.label, scan.window)
        return {
            "done": what,
            "target": found.number,
            "label": found.element.label,
            "next_step": (
                "That will have changed the screen, so every number from scan "
                f"{scan.id} is now a guess. Call look_at_screen before the next one "
                "unless you are certain nothing moved - a number that no longer "
                "matches is refused rather than clicked, but a refusal costs a turn."
            ),
        }

    @server.tool(
        name="look_at_screen",
        title="See what is on screen, as numbered targets",
        description=(
            "List everything on screen that can be clicked or typed into, numbered. "
            "Call it before acting, and again after anything you do.\n\n"
            "With no arguments it reads the window in front. `window` picks a "
            "different one by any part of its title, and the result names the others "
            "that are open. `matching` keeps only the labels containing it, which is "
            "how you find one control in a crowded window - a browser has hundreds "
            "and only the first few dozen are listed.\n\n"
            "You get ids and labels, not coordinates. There is nothing to measure and "
            "no arithmetic to do: name the number and JARVIS works out where it is. A "
            "minimised window is refused rather than scanned, because its coordinates "
            "are left over from wherever it was last drawn."
        ),
    )
    def look_at_screen(window: str = "", matching: str = ""):
        try:
            return described(desktop.look(window, matching))
        except ScreenUnavailable as exc:
            logger.warning("Look failed - %s", exc)
            return {"error": str(exc), "targets": []}

    @server.tool(
        name="screenshot",
        title="See the screen as a picture",
        description=(
            "Take a picture of a window and return it. The fallback for when the "
            "numbered list is not the question - an error dialog to read, a chart, "
            "anything where seeing it is the point rather than pressing it. With no "
            "arguments it captures the window in front; whole_desk captures every "
            "monitor.\n\n"
            "Prefer look_at_screen for anything you intend to act on. It is smaller, it "
            "is exact, and it gives you numbers you can click. This gives you neither. "
            "with_numbers=true draws the boxes from a fresh scan onto the picture, which "
            "is the two together.\n\n"
            "Needs a model that can read images. If yours cannot, you will get a "
            "description of the file and nothing useful in it."
        ),
    )
    def screenshot(window: str = "", whole_desk: bool = False, with_numbers: bool = False):
        from . import marks

        target = config.log_dir / (config.screen.screenshot_file or "screen.png")
        try:
            if with_numbers and not whole_desk:
                scan = desktop.look(window)
                bounds = desktop.backend.window_rect(scan.hwnd)
                path = marks.draw(scan, bounds, target)
                described = scan.as_dict(config.screen.label_chars)
            else:
                if whole_desk:
                    bounds, where = None, "every monitor"
                else:
                    hwnd, where = desktop.find_window(window)
                    bounds = desktop.backend.window_rect(hwnd)
                path = marks.capture(bounds, target, config.screen.screenshot_max_width)
                described = {"window": where}
        except (ScreenUnavailable, OSError, RuntimeError) as exc:
            logger.warning("Screenshot failed - %s", exc)
            return {"error": str(exc)}

        described["screenshot"] = str(path)
        described["next_step"] = (
            "The picture is attached. Describe what the user needs from it and say it "
            'with converse(say=..., then="listen") - they cannot see your reply text. To act on '
            "anything in it, call look_at_screen and use the numbers."
        )
        from mcp.server.mcpserver.utilities.types import Image

        return [described, Image(path=str(path))]

    if config.screen.control:

        @server.tool(
            name="focus_window",
            title="Bring a window to the front and look at it",
            description=(
                "Raise a window, restoring it if it was minimised, then scan it. Input "
                "goes to whatever holds the foreground rather than to whatever was "
                "scanned last, so this is what to call when the thing you want is behind "
                "something else. Matches any part of the title."
            ),
        )
        def focus_window(window: str, matching: str = ""):
            try:
                return described(desktop.focus(window, matching))
            except ScreenUnavailable as exc:
                logger.warning("Focus failed - %s", exc)
                return {"error": str(exc), "targets": []}

        @server.tool(
            name="click",
            title="Click a numbered target",
            description=(
                "Click one of the numbers from look_at_screen. Both arguments are "
                "required and `expecting` is checked: pass the label you read next to "
                "that number, and if the number now points at something else the click "
                "is refused instead of landing on it. Say what you meant and a stale "
                "number costs you a turn rather than deleting the wrong message.\n\n"
                "The window is brought to the front first. Clicking is a real pointer "
                "moving on a real desktop, so it is visible and it interrupts whatever "
                "the user was doing."
            ),
        )
        def click(
            target: int,
            expecting: str,
            button: Literal["left", "right"] = "left",
            clicks: Literal[1, 2] = 1,
        ) -> dict:
            try:
                found, scan = aim(target, expecting)
            except ScreenUnavailable as exc:
                logger.warning("Click refused - %s", exc)
                return {"error": str(exc), "done": None}
            x, y = found.element.centre
            hands.click(
                x, y, button=button, count=clicks, settle=config.screen.click_settle_seconds
            )
            what = f"{button} click x{clicks}" if clicks > 1 else f"{button} click"
            return acted(what, found, scan)

        @server.tool(
            name="type_text",
            title="Type into a numbered target",
            description=(
                "Click a target to put the caret in it, then type. `then` is required "
                "and it is the decision worth getting right: press_enter submits the "
                "form or sends the message, leave_it types and stops. A half written "
                "message sent early cannot be taken back.\n\n"
                "`clear_first` selects what is already there so the text replaces it "
                "rather than joining onto the end. `expecting` is checked against the "
                "label, as with click. Typed as unicode, so it does not depend on the "
                "keyboard layout, and newlines in the text are sent as enter."
            ),
        )
        def type_text(
            target: int,
            expecting: str,
            text: str,
            then: Literal["press_enter", "leave_it"],
            clear_first: bool = False,
        ) -> dict:
            try:
                found, scan = aim(target, expecting)
            except ScreenUnavailable as exc:
                logger.warning("Typing refused - %s", exc)
                return {"error": str(exc), "done": None}
            x, y = found.element.centre
            hands.click(x, y, settle=config.screen.click_settle_seconds)
            if clear_first:
                hands.press("ctrl+a")
            hands.type_text(text)
            if then == "press_enter":
                hands.press("enter")
            result = acted("typed and submitted" if then == "press_enter" else "typed", found, scan)
            result["text"] = text
            return result

        @server.tool(
            name="scroll",
            title="Scroll at a numbered target",
            description=(
                "Wheel the pointer over a target and scroll. Use it when what you want "
                "is not in the list because it is scrolled out of view - offscreen "
                "elements are left out of a scan rather than offered at coordinates "
                "nobody can click. Scan again afterwards: the numbers will have moved."
            ),
        )
        def scroll(
            target: int,
            expecting: str,
            direction: Literal["up", "down"],
            notches: int = 3,
        ) -> dict:
            try:
                found, scan = aim(target, expecting)
            except ScreenUnavailable as exc:
                logger.warning("Scroll refused - %s", exc)
                return {"error": str(exc), "done": None}
            x, y = found.element.centre
            turns = max(1, min(20, notches)) * (1 if direction == "up" else -1)
            hands.scroll(x, y, turns, settle=config.screen.click_settle_seconds)
            return acted(f"scrolled {direction}", found, scan)

        @server.tool(
            name="press_keys",
            title="Press a keyboard shortcut",
            description=(
                "Press a combination like ctrl+s, alt+f4, escape or f5. It goes to "
                "whatever holds the keyboard focus, which is whatever you last clicked "
                "or typed into - there is no target to name and nothing to check, so be "
                "sure of what is in front before using it. An unknown key name is "
                "refused rather than half pressed.\n\n"
                "The media keys are the exception and worth reaching for first: "
                "playpause, nexttrack, prevtrack, stop, volumeup, volumedown, mute. "
                "Windows routes those to whatever is playing, so pausing or skipping "
                "music needs no window, no scan and no target at all."
            ),
        )
        def press_keys(keys: str) -> dict:
            try:
                hands.press(keys)
            except ValueError as exc:
                logger.warning("Keys refused - %s", exc)
                return {"error": str(exc), "done": None}
            logger.info("Pressed %s", keys)
            return {
                "done": "pressed",
                "keys": keys,
                "next_step": (
                    "Sent to whatever had focus. Call look_at_screen to see what it did."
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
            logger.warning("Status check failed - %s", exc)
            return {"error": str(exc), "listening": False}

    return server


def _capabilities(ctx) -> str:
    """What the client says it can do, or why we could not ask.

    `sampling` is the one that matters: a client advertising it can be asked to
    run a completion, which is the only way in MCP for the server to obtain
    model output rather than sit and wait to be called again. Everything else
    here is enforcement of calls that happen; that would be enforcement of a
    call happening at all.
    """
    try:
        reported = ctx.client_capabilities()
    except Exception as exc:  # no request context, e.g. called directly in a test
        return f"unavailable ({type(exc).__name__})"
    if reported is None:
        return "none reported"
    named = [name for name, value in reported.model_dump().items() if value is not None]
    return ", ".join(named) or "none"


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
