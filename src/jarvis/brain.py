"""JARVIS's own agent loop.

Everything before this version, JARVIS was called. An agent held the loop, and
speaking was a tool that agent had to remember to call - which it forgot, in
sessions recorded in DESIGN.md, and no schema could make it remember: a required
argument shapes a call that happens and cannot cause a call to happen. Five
mechanisms were built against that and four were removed as jank.

Owning the loop dissolves the problem rather than solving it. **The reply is the
speech.** There is no say tool here to forget, because what the model writes as
its answer is what goes through the speakers - and a turn that ends with nothing
to say gets one more call with the tools taken away, so prose is the only move
left. Silence is now the thing that takes deliberate effort.

    hear -> model, with tools -> run them -> model again -> speak the reply

The model is whatever OpenAI-compatible endpoint `brain.url` points at, which
here is llama-server on loopback. The tools are the repo's own Python, called
directly (`tools.py`). Nothing in this file knows about MCP; `mcp_server.py`
still exists for handing the microphone to a coding agent instead, and the two
should not be running at once - they would both answer.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import memories, tools, ui
from .config import BrainConfig, Config, project_root
from .tools import Toolbox, build_toolbox, parse_arguments
from .tts import for_speaking

logger = logging.getLogger("jarvis.brain")

# How long one wait for speech blocks before looping. Only so that stopping is
# quick - nothing is polled, the wait is on a condition.
LISTEN_SLICE_SECONDS = 1.0

# How often the room is checked while a reply is being generated. Often enough
# that talking over it feels immediate, rarely enough that it is not a transcript
# read per token.
WATCH_SECONDS = 0.3

# Said when the model has run its tools and then produced nothing at all, twice.
# Reporting a failure out loud beats silence, which is indistinguishable from a
# crash to somebody who can only hear.
NOTHING_TO_SAY = "Sorry sir, I could not put an answer together."

# Said once when the endpoint is unreachable, and not again until it answers.
NO_MODEL = "I cannot reach my model, sir."

# Prefixed to something said while the model was still writing. The reply it was
# part way through is gone, so this has to say why the thread was dropped.
STEERED = (
    "They spoke over you, so whatever you were part way through writing was "
    "discarded and never reached them. This is what they said - do that instead:\n"
)

# Sent back when the answer at the end of a turn is a typed out tool call. There
# is nothing left to call by then, so this asks for the only thing still useful.
OUT_OF_STEPS = (
    "That was markup, not an answer, and it would have been read out with the "
    "tags in it. There are no tools left this turn. Say in one plain sentence "
    "what you found and what you could not do."
)

# Sent back when a reply is a tool call typed out as text rather than emitted as
# one. Costs a round trip and saves reading `search_web(query="...")` aloud.
CALL_IT = (
    "That was {name} written out as words, in the text that gets read to them. "
    "It did not run. Emit it as a tool call this time, or if you cannot, say in "
    "plain English what you were trying to do."
)

# A lesson naming a target number is worse than no lesson. Every scan numbers
# what it finds from scratch, so "the Close button is target 3 when focused" -
# written down verbatim in a live session - is a click on something else
# tomorrow. Asked for in the prompt below and enforced here.
PER_SCAN = re.compile(r"\btargets?\s+(number\s+)?\d", re.IGNORECASE)

# Enough for two sentences and no more. Whether one line is worth keeping is not
# a question that gets better with more room to answer it in.
LOOK_BACK_TOKENS = 200

# Asked after the answer has gone to the speakers, so the wait is somebody
# else's. Deliberately hard to say yes to: most turns teach nothing, and a list
# that fills up with "Teams was open" is worse than an empty one.
LOOK_BACK = """That turn is over and they are hearing the answer now. Nobody is
waiting on this.

Look back at what you just did. Was there anything about how this DESKTOP
behaves that would have saved you a step, and that is not already written down
below? A window that behaves oddly, a route that works, one that never does, the
name a program is really installed under.

Reply with nothing at all if there is nothing worth keeping, which is most
turns. Otherwise reply with at most two lines, each beginning with "- " and each
a single sentence somebody could act on months from now.

Never a target number: every scan numbers what it finds again from scratch, so a
number written down here points at something else tomorrow. Never anything that
would be true of any Windows machine, only this one.

Never anything about this conversation - not what they asked, not what you said,
not what they seem to want. Only how the machine behaves.

Already written down:
{known}"""

SOUL = "context/soul/jarvis.md"

# The ears paragraph, kept inline in that file inside these markers so whoever
# opens it reads the whole prompt. Taken out again when there is no microphone
# to close, because a prompt naming a tool that is not there invites a call that
# comes straight back as an error.
EARS_OPEN = "<!-- ears -->"
EARS_SHUT = "<!-- /ears -->"


class ModelUnavailable(RuntimeError):
    """The endpoint in `brain.url` did not answer. Usually llama-server is down."""


class Cancelled(Exception):
    """Escape was pressed. Whatever was in flight is abandoned unanswered.

    Distinct from Interrupted, which carries a new instruction and starts the
    turn again knowing more. This one carries nothing, because there is nothing
    to carry: they have decided the answer is not worth waiting for.
    """


class Interrupted(Exception):
    """Somebody spoke while the model was still writing.

    Carries what they said. Raised rather than returned because a half generated
    reply is not an answer to anything - the request is abandoned, which stops
    the server generating too, and the turn starts again knowing more.
    """

    def __init__(self, said: list[str]) -> None:
        super().__init__(" ".join(said))
        self.said = said


@dataclass(frozen=True)
class Call:
    """One tool call, with whatever the model actually sent."""

    id: str
    name: str
    arguments: dict = field(default_factory=dict)
    # Why the arguments could not be read, if they could not be.
    broken: str = ""


@dataclass(frozen=True)
class Reply:
    """One model turn: prose, tool calls, or both."""

    text: str
    calls: tuple[Call, ...] = ()
    # What it reasoned on the way, kept for the log. Never sent back: it is the
    # largest thing in a response and the endpoint does not want it returned.
    thinking: str = ""
    # Whether the generation hit max_tokens rather than finishing. Reasoning and
    # answer share that budget, so a hard think can leave nothing to say.
    truncated: bool = False
    # Prompt and completion tokens, as the endpoint reported them. (0, 0) when
    # it did not - the meter says nothing rather than guessing.
    tokens: tuple[int, int] = (0, 0)
    # The assistant message to append to the history, trimmed to the three keys
    # the endpoint needs. Reasoning fields are dropped rather than echoed back.
    message: dict = field(default_factory=dict)


class Model:
    """An OpenAI-compatible chat endpoint, one call at a time."""

    def __init__(
        self, config: BrainConfig, client: httpx.Client | None = None, terminal=None
    ) -> None:
        self.config = config
        self.base = config.url.rstrip("/")
        # Where the reasoning goes while it is being generated. Silent by
        # default, which is also what turns the streaming read off - there is no
        # point reading a reply a token at a time with nobody watching.
        self.ui = terminal or ui.Silent()
        self._limit: int | None = None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(config.timeout_seconds, connect=5.0),
            headers=({"Authorization": f"Bearer {config.api_key}"} if config.api_key else {}),
        )

    def available(self) -> str:
        """Empty if the endpoint answers, otherwise why it did not.

        Checked before the loop starts, the same way Whisper proves its device
        with a real inference: a broken default should say so at startup rather
        than the first time somebody speaks.
        """
        try:
            response = self._client.get(f"{self.base}/models", timeout=5.0)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            return str(exc)
        return ""

    def context_limit(self) -> int:
        """How much context the server has, or 0 if it will not say.

        llama.cpp answers this on /props, which is not part of the OpenAI shape -
        hence the fallback to `brain.context_limit` and to saying nothing.
        """
        if self.config.context_limit:
            return self.config.context_limit
        if self._limit is None:
            self._limit = 0
            try:
                response = self._client.get(f"{self.base.removesuffix('/v1')}/props", timeout=5.0)
                response.raise_for_status()
                settings = response.json().get("default_generation_settings") or {}
                self._limit = int(settings.get("n_ctx") or 0)
            except (httpx.HTTPError, ValueError, TypeError):
                logger.debug("The endpoint did not say how big its context is.")
        return self._limit

    def reply(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        limit: int | None = None,
        watch=None,
        think: bool | None = None,
        stop=None,
    ) -> Reply:
        """One completion. `tools=None` leaves the model nothing to do but write.

        `think` overrides `brain.thinking` for this call alone. Only one place
        uses it: reasoning earns its cost when a tool has to be chosen, and the
        looking back afterwards has no tools to choose between.
        """
        payload: dict = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": limit or self.config.max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools
        if not (self.config.thinking if think is None else think):
            # The model's own chat template decides whether it reasons, and this
            # is what llama.cpp's --jinja passes through to it. An endpoint that
            # does not know the argument ignores it, which is the right failure.
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if self.config.stream and not isinstance(self.ui, ui.Silent):
            return self._streamed(payload, watch, stop)
        try:
            response = self._client.post(f"{self.base}/chat/completions", json=payload)
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as exc:
            raise ModelUnavailable(f"No model at {self.base} ({exc}).") from exc
        except ValueError as exc:
            raise ModelUnavailable(f"{self.base} did not return JSON ({exc}).") from exc
        return _read(body)

    def _streamed(self, payload: dict, watch=None, stop=None) -> Reply:
        """The same completion, read as it is written.

        Worth the extra code for two reasons. The reasoning can be shown while it
        is happening - a spinner says a model is busy, and the last line of what
        it is thinking says whether it is busy on the right thing. And a reply
        being read a token at a time is a reply that can be abandoned: `watch` is
        checked as it goes, and anything said into the room stops the generation
        where it stands.

        Everything else is reassembled here, so the caller gets the same `Reply`
        it would have got from one response - a half-read stream is a failure,
        not a short answer.
        """
        payload = {**payload, "stream": True, "stream_options": {"include_usage": True}}
        used: dict = {}
        text: list[str] = []
        thinking: list[str] = []
        calls: dict[int, dict] = {}
        stopped = ""
        looked_at = 0.0
        try:
            with self._client.stream(
                "POST", f"{self.base}/chat/completions", json=payload
            ) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    body = line[5:].strip()
                    if body == "[DONE]":
                        break
                    try:
                        chunk = json.loads(body)
                    except ValueError:
                        continue
                    # Cheap, but not per token: at fifty tokens a second that
                    # would be fifty transcript reads for a delay nobody could
                    # perceive being shortened.
                    if (watch or stop) and time.monotonic() - looked_at > WATCH_SECONDS:
                        looked_at = time.monotonic()
                        if stop is not None and stop():
                            logger.info("Cancelled mid reply.")
                            raise Cancelled
                        if watch is not None and (said := watch()):
                            logger.info("Interrupted mid reply by %r", said)
                            raise Interrupted(said)

                    used = chunk.get("usage") or used
                    choice = (chunk.get("choices") or [{}])[0]
                    stopped = choice.get("finish_reason") or stopped
                    delta = choice.get("delta") or {}
                    if piece := delta.get("reasoning_content"):
                        thinking.append(str(piece))
                        self.ui.thinking("".join(thinking))
                    if piece := delta.get("content"):
                        text.append(str(piece))
                        self.ui.thinking("".join(text))
                    _collect(calls, delta.get("tool_calls") or [])
        except httpx.HTTPError as exc:
            raise ModelUnavailable(f"No model at {self.base} ({exc}).") from exc

        message: dict = {"content": "".join(text), "reasoning_content": "".join(thinking)}
        if calls:
            message["tool_calls"] = [calls[index] for index in sorted(calls)]
        one = {"message": message, "finish_reason": stopped}
        return _read({"choices": [one], "usage": used})


def _collect(calls: dict[int, dict], deltas: list) -> None:
    """Reassemble tool calls from the fragments a stream delivers them in.

    Keyed by `index` rather than by arrival, because a model can interleave two
    calls, and the arguments come through as a string built a few characters at
    a time - one delta is rarely valid JSON on its own.
    """
    for part in deltas:
        if not isinstance(part, dict):
            continue
        index = part.get("index", 0)
        call = calls.setdefault(index, {"id": "", "type": "function", "function": {}})
        if identifier := part.get("id"):
            call["id"] = identifier
        function = part.get("function") or {}
        if name := function.get("name"):
            call["function"]["name"] = name
        if arguments := function.get("arguments"):
            call["function"]["arguments"] = call["function"].get("arguments", "") + arguments


def _read(body: dict) -> Reply:
    """Pull the one choice apart, tolerating a thin or odd response."""
    choices = body.get("choices") or [{}]
    message = choices[0].get("message") or {}
    ran_out = choices[0].get("finish_reason") == "length"
    text = str(message.get("content") or "").strip()

    calls = []
    for index, raw in enumerate(message.get("tool_calls") or []):
        function = raw.get("function") or {}
        arguments, broken = parse_arguments(function.get("arguments"))
        calls.append(
            Call(
                id=str(raw.get("id") or f"call_{index}"),
                name=str(function.get("name") or ""),
                arguments=arguments,
                broken=broken,
            )
        )

    kept: dict = {"role": "assistant", "content": text}
    if message.get("tool_calls"):
        kept["tool_calls"] = message["tool_calls"]
    used = body.get("usage") or {}
    spent = (int(used.get("prompt_tokens") or 0), int(used.get("completion_tokens") or 0))
    thought = str(message.get("reasoning_content") or "")
    return Reply(
        text=text,
        calls=tuple(calls),
        message=kept,
        thinking=thought,
        truncated=ran_out,
        tokens=spent,
    )


class ServiceVoice:
    """Ears and mouth of a VoiceService running in this process.

    No HTTP: the brain is inside the process that owns the hardware, so the
    loopback API is for everything else that is not.
    """

    def __init__(self, service) -> None:
        self.service = service
        self._cursor = service.transcript.cursor

    def hear(self, timeout: float) -> list[str]:
        """Everything said since the last look. Empty means nobody spoke."""
        heard = self.service.transcript.wait_for(self._cursor, timeout=timeout)
        if heard:
            self._cursor = heard[-1].id
        return [item.text for item in heard]

    def say(self, text: str) -> None:
        self.service.say(text)

    def hush(self) -> None:
        self.service.hush()

    def waiting(self) -> str:
        """What the live line should say while nothing is happening.

        Not always "listening": the microphone can be shut, by the hotkey or by
        the model itself, and a status line that claims to be listening while
        deaf is how a working feature comes to look broken.
        """
        if self.service.transcript.paused:
            key = self.service.config.service.hotkey or "resume_transcription"
            return f"not listening - {key} to start again"
        return "listening"

    # Having these is what puts the two transcription tools in the toolbox. Chat
    # mode has no microphone, so it has neither and neither tool appears.
    def pause(self) -> bool:
        return self.service.pause()

    def resume(self) -> None:
        self.service.resume()


class Brain:
    """The loop. One utterance in, one spoken reply out, tools in between."""

    def __init__(
        self,
        config: Config,
        voice,
        model: Model | None = None,
        toolbox: Toolbox | None = None,
        terminal=None,
    ) -> None:
        self.config = config
        self.settings = config.brain
        self.voice = voice
        # The terminal, if there is one. Silent by default so nothing here has
        # to check whether anybody is watching - and the model reads its reply as
        # a stream only when there is somebody to show it to.
        self.ui = terminal or ui.Silent()
        self.model = model or Model(config.brain, terminal=self.ui)
        self.toolbox = toolbox or build_toolbox(config, ears=voice)
        self.messages: list[dict] = [{"role": "system", "content": self.system_prompt()}]
        # Last measured prompt size, kept so the meter reads the same all the way
        # through a turn rather than dropping when the tools come off, and so
        # that _trim knows how big the conversation has actually got.
        self._room = ""
        self._spent = 0
        # What the whole session has cost, which is the question the numbers in
        # the corner are really answering - one call's usage says nothing about
        # whether an afternoon of this has been expensive.
        self._tokens_in = 0
        self._tokens_out = 0
        # Set by escape and read as the stream arrives, so a reply can be
        # abandoned the moment they decide it is going the wrong way.
        self.stopped = threading.Event()
        # Whether there is anything to abandon. Escape with nothing happening
        # should do nothing at all rather than put a prompt up for no reason.
        self._working = threading.Event()
        self.thread: threading.Thread | None = None

    def system_prompt(self) -> str:
        """Who JARVIS is, read from `context/soul/brain.md`.

        A file rather than a string in here, because it is prose: it is tuned by
        reading it out loud and changing a word, not by editing Python, and it is
        the largest single influence on behaviour in the whole repository. There
        is no copy of it in the code to drift from - `brain.system_prompt_file`
        points somewhere else, and nothing falls back to a built-in, because a
        JARVIS with a stand-in personality and no obvious cause is worse than one
        that says which file is missing.
        """
        path = Path(self.settings.system_prompt_file or project_root() / SOUL).expanduser()
        body = path.read_text(encoding="utf-8")
        return with_ears(body, "pause_transcription" in self.toolbox.tools).format(
            user=os.environ.get("USERNAME") or "the user",
            tools=", ".join(self.toolbox.names) or "nothing but your own knowledge",
            memories="\n" + self.remembered() if self.remembered() else "",
        )

    def remembered(self) -> str:
        """The lessons block, re-read rather than cached.

        Read at the top of every turn, which is what makes a memory written at
        half past two apply at half past three - and means editing the file by
        hand takes effect on the next thing anyone says.
        """
        if not self.settings.memories:
            return ""
        return memories.as_prompt(self._known())

    def _known(self) -> list[str]:
        """Every line of it, reference and learned, as the prompt will see it."""
        if not self.settings.memories:
            return []
        written = self._written()
        return memories.load(written[0].parent, written, self.settings.max_memory_chars)

    def _written(self) -> tuple[Path, Path]:
        """The two files JARVIS adds to, which are the two that get capped."""
        return tools.memory_file(self.config), tools.navigation_file(self.config)

    # ------------------------------------------------------------------ a turn

    def turn(self, said: list[str]) -> str:
        """Answer one utterance, then look back at it.

        Returns what was spoken, "" if nothing was. The looking back happens
        after that, on purpose: speech is queued and played on another thread,
        so a model call made now costs nobody anything.
        """
        before = len(self.messages)
        self.stopped.clear()
        self._working.set()
        try:
            try:
                spoken = self._answer(said)
            except Cancelled:
                # The whole turn goes with it, tool results and all. A half
                # worked request left in the history is a question they have
                # already withdrawn, and an assistant message whose tool calls
                # were never answered is one the endpoint will refuse outright.
                del self.messages[before:]
                logger.info("Cancelled - the turn was dropped.")
                self.ui.note("  cancelled")
                return ""
            used_hands = any(message.get("role") == "tool" for message in self.messages[before:])
            if self.settings.consolidate and self.settings.memories and used_hands:
                self._look_back()
            return spoken
        finally:
            self._working.clear()
            self.stopped.clear()

    def cancel(self) -> bool:
        """Stop whatever is in flight. True if there was anything to stop.

        The answer is what the caller does next with: escape while nothing is
        happening should be nothing at all, not a prompt appearing for no
        reason. Speech goes as well as thinking - "stop" said to something part
        way through a sentence means that sentence too.
        """
        if not self._working.is_set():
            return False
        self.stopped.set()
        self.voice.hush()
        return True

    def _look_back(self) -> None:
        """Write down anything about the desk that turn just taught.

        The only way a lesson outlives the conversation it was learned in
        without somebody typing it up. Everything about it is deliberately quiet:
        it happens behind the speech, its thinking goes to the live line and then
        vanishes like any other, and it never touches the conversation - the
        question is asked over a copy and the answer is thrown away.
        """
        self.ui.status("looking back")
        known = "\n".join(f"- {x}" for x in self._known()) or "- nothing yet"
        asked = [*self.messages, {"role": "user", "content": LOOK_BACK.format(known=known)}]
        try:
            # No reasoning, and a short leash. Reasoning earns its cost when a
            # tool has to be chosen, and this call has no tools: left on, it
            # spent four thousand characters weighing up whether one line was
            # worth keeping and then ran out of room before writing it.
            reply = self.model.reply(
                asked,
                tools=None,
                limit=LOOK_BACK_TOKENS,
                think=False,
                stop=self.stopped.is_set,
            )
        except Cancelled:
            return
        except Exception:
            logger.exception("Looking back failed; carrying on without it.")
            return

        if reply.thinking:
            logger.info("looking back: %s", " ".join(reply.thinking.split()))
        path = tools.navigation_file(self.config)
        for lesson in memories.bullets_in(reply.text)[:2]:
            if PER_SCAN.search(lesson):
                logger.info("Not kept, target numbers do not survive the scan: %s", lesson)
                continue
            logger.info("Learned: %s", lesson)
            memories.remember(path, lesson, self.settings.max_memory_chars)

    def _answer(self, said: list[str]) -> str:
        """One utterance, worked and spoken. Returns what was said out loud."""
        self.messages[0] = {"role": "system", "content": self.system_prompt()}
        self.messages.append({"role": "user", "content": "\n".join(said)})
        self._trim()
        used_tools = False

        step = 0
        while step < max(1, self.settings.max_steps):
            step += 1
            # Checked here as well as mid stream, so a cancel that arrives while
            # a tool is running lands the moment it finishes rather than costing
            # a whole model call first.
            if self.stopped.is_set():
                raise Cancelled
            try:
                reply = self._ask(self.toolbox.specs())
            except Interrupted as cut:
                # Everything found so far is kept. "No, the other one" should
                # build on the look that has already happened rather than start
                # the turn again from nothing.
                #
                # But the budget goes back to full. A live session spent eleven
                # of twelve steps opening the wrong thing, was told "no, go in
                # the taskbar", and had one step left to do it in - which is
                # precisely backwards. A new instruction is a new turn's worth of
                # work, and the only thing that can spend the budget this way is
                # somebody choosing to keep talking.
                #
                # Not drawn here: the service already drew it when it was
                # transcribed, and twice on screen reads as being said twice.
                steer = STEERED + "\n".join(cut.said)
                self.messages.append({"role": "user", "content": steer})
                used_tools = True
                step = 0
                continue
            self.messages.append(reply.message)

            if not reply.calls:
                # A tool call written out as words instead of emitted as one.
                # Seen with reasoning off and ten tools in front of it, and the
                # failure is as bad as it gets here: the answer that reaches
                # them is `search_web(query="the weather")`, read aloud.
                if wanted := written_as_words(reply.text, self.toolbox.names):
                    logger.warning("Typed a tool call instead of making one: %r", reply.text[:80])
                    self.messages.append({"role": "user", "content": CALL_IT.format(name=wanted)})
                    used_tools = True
                    continue
                if reply.text:
                    return self._speak(reply.text)
                break

            # A line written alongside the first tool calls is the lead-in - "let
            # me have a look, sir" - and worth saying, because the work that
            # follows is seconds of silence. Only the first: saying something
            # after every tool call is narration nobody asked for. There is a
            # first again after an interruption, which is right - they have just
            # asked for something else.
            if reply.text and step == 1:
                self._speak(reply.text)

            used_tools = True
            for call in reply.calls:
                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": self._run(call),
                    }
                )

            # Nothing can interrupt a turn from outside, so the turn looks. This
            # is what owning the loop buys: "no, the other one" lands mid task
            # rather than after the wrong thing has already been done.
            if spoken := self.voice.hear(0.0):
                logger.info("Heard mid-task: %r", spoken)
                self.messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Said while you were working, so read it before you carry on - "
                            "it may be a correction: " + "\n".join(spoken)
                        ),
                    }
                )

        return self._speak(self._final_word(), fallback=used_tools)

    def _ask(self, tools, limit: int | None = None) -> Reply:
        """One model call, watched, with the meter kept up to date afterwards.

        Only a call carrying tools is interruptible. The last one of a turn is a
        sentence away from being spoken, and abandoning that to start again loses
        the answer to work already done.
        """
        self.ui.status("thinking")
        reply = self.model.reply(
            self.messages,
            tools,
            limit=limit,
            watch=(lambda: self.voice.hear(0.0)) if tools else None,
            stop=self.stopped.is_set,
        )
        if reply.thinking:
            logger.info("thought: %s", " ".join(reply.thinking.split()))
        if reply.truncated:
            # The one failure with no other symptom: 2473 characters of
            # reasoning stopped mid sentence against a 600 token cap, and the
            # answer that never got written came out as "I could not put an
            # answer together" with nothing in the log to say why.
            logger.warning(
                "Ran out of room at %d tokens - reasoning and answer share that budget. "
                "Raise brain.max_tokens if this repeats.",
                limit or self.settings.max_tokens,
            )
        prompt, written = reply.tokens
        self._tokens_in += prompt
        self._tokens_out += written
        # ctx comes only from a call that carried the tools. The last call of a
        # turn drops them, which takes about 1.8k of schemas out of the prompt,
        # and a number that halves at the end of every turn reads as the
        # conversation being thrown away rather than as two shapes of request.
        # The totals have no such problem: every call costs what it costs.
        if prompt and tools:
            self._spent = prompt
            self._room = f"ctx {count(prompt)}"
            if limit := self.model.context_limit():
                self._room += f"/{count(limit)}"
        if prompt:
            self.ui.meter(
                f"{self._room} - in {count(self._tokens_in)} - out {count(self._tokens_out)}"
            )
        return reply

    def _run(self, call: Call) -> str:
        if call.broken:
            return (
                f"{call.name} was not called - {call.broken}. Send the arguments again as "
                "a JSON object."
            )
        arguments = "(" + ", ".join(f"{k}={v!r}" for k, v in call.arguments.items()) + ")"
        logger.info("%s%s", call.name, arguments)
        self.ui.tool(call.name, arguments)
        self.ui.status(f"running {call.name}")
        result = self.toolbox.run(call.name, call.arguments)
        # The first line of it, because a post mortem needs what came back and
        # not only what was asked. `start teams` returning nothing is the whole
        # reason one session decided Teams was not installed.
        logger.info("  -> %s", first_line(result))
        self.ui.result(result)
        # Only on a refusal, which is rare and is exactly the moment there is
        # something to learn. Nudging after every call would fill the list with
        # notes about things that worked.
        if result.startswith("Refused:") and self.settings.memories:
            result += (
                "\n(If you work out why that was refused and it would save you next time, "
                "remember() it.)"
            )
        return result

    def _final_word(self) -> str:
        """One more call with the tools taken away, and one more if that fails.

        Reached when the step budget ran out or the model produced nothing at
        all. With no tools in the request there is nothing for it to emit except
        prose, which is the whole reason speaking cannot be forgotten here.

        Two things still go wrong here and both were live. It can TYPE a call
        instead of making one - `<tool_call> <function=look_at_screen>` was read
        out with the tags in it. And it can spend the whole token budget
        thinking: 2473 characters of reasoning stopping mid sentence against a
        cap of 600, with no answer written at all, because the reasoning and the
        answer come out of the same allowance.

        So the last thing said is checked like everything else, and it gets one
        more go with twice the room.
        """
        reply = self._ask(None)
        self.messages.append(reply.message)
        if not (trouble := self._unusable(reply)):
            return reply.text

        logger.warning("No answer to speak - %s: %r", trouble, reply.text[:70])
        self.messages.append({"role": "user", "content": OUT_OF_STEPS})
        # Twice the budget, because running out of room mid thought is one of
        # the two ways to get here and asking again inside the same cap would
        # get the same nothing.
        second = self._ask(None, limit=self.settings.max_tokens * 2)
        self.messages.append(second.message)
        return "" if self._unusable(second) else second.text

    def _unusable(self, reply: Reply) -> str:
        """Why this cannot be said out loud, or nothing if it can."""
        if written_as_words(reply.text, self.toolbox.names):
            return "it typed a tool call"
        if reply.truncated and not reply.text.strip():
            return "it ran out of room before writing one"
        return ""

    def _speak(self, text: str, fallback: bool = False) -> str:
        """Say it, unless it was deliberately nothing.

        A reply with no letters or digits in it is the agreed way to stay quiet -
        a hyphen, an ellipsis, whatever the model reaches for. `fallback` says
        that work was done first, in which case an empty answer is a failure
        rather than a decision and gets reported out loud.
        """
        spoken = for_speaking(text)
        if is_silence(spoken):
            if fallback:
                logger.warning("Nothing to say after running tools; reporting that instead.")
                self.voice.say(NOTHING_TO_SAY)
                return NOTHING_TO_SAY
            logger.info("Not for me, staying quiet.")
            return ""
        self.voice.say(spoken)
        return spoken

    def _trim(self) -> None:
        """Keep the system prompt and the last few turns, whole.

        Cut at a user message rather than at a message count: dropping half of a
        turn leaves a tool result whose call is gone, which some endpoints reject
        outright. Nothing is summarised - see the README on what that would cost.

        Two limits, because turns are not the same size. A greeting is fifty
        tokens; a turn that scans a crowded window twice is six thousand, and
        twenty of those would overflow a 98k window and fail the request outright
        rather than degrade. So the turn count is the usual one and the measured
        size is the backstop, one turn dropped per turn taken - which is enough,
        because the conversation only grows one turn at a time.
        """
        keep = max(1, self.settings.history_turns)
        if self._spent > self._ceiling():
            logger.info("Conversation is %d tokens; dropping the oldest turn.", self._spent)
            keep = max(1, min(keep, self._turns()) - 1)

        starts = self._turns(indexes=True)
        if len(starts) <= keep:
            return
        del self.messages[1 : starts[-keep]]

    def _turns(self, indexes: bool = False):
        """Where each turn starts, or how many there are."""
        starts = [i for i, message in enumerate(self.messages) if message.get("role") == "user"]
        return starts if indexes else len(starts)

    def _ceiling(self) -> int:
        """Most of the window the conversation may take, or none if unknown."""
        fraction = self.settings.max_context_fraction
        limit = self.model.context_limit() if fraction > 0 else 0
        return int(limit * fraction) if limit else 2**31

    # ------------------------------------------------------------------ the loop

    def preload(self) -> None:
        """One throwaway call, so the first real one is not the slow one.

        The system prompt and the tool schemas are the same on every request and
        are most of it, so a server that reuses a cached prefix - llama.cpp's
        `--cache-reuse` - only has to process them once. Doing that here spends a
        second or two of nobody's time instead of putting it on the first answer,
        which is the one that would otherwise feel broken.

        A one token limit, because nothing about the reply is wanted. It is not
        added to the history either: this conversation never happened.
        """
        self.ui.status("warming the model up")
        started = time.monotonic()
        try:
            self.model.reply(
                [*self.messages, {"role": "user", "content": "Are you there?"}],
                self.toolbox.specs(),
                limit=1,
            )
        except ModelUnavailable as exc:
            logger.warning("Could not preload - %s", exc)
            return
        except Exception:
            logger.exception("Preloading failed; carrying on without it.")
            return
        logger.info("Model warmed up in %.1fs.", time.monotonic() - started)

    def run_forever(self, stop: threading.Event | None = None) -> None:
        """Listen, answer, repeat, until stopped."""
        stop = stop or threading.Event()
        complained = False
        if self.settings.preload:
            self.preload()
        while not stop.is_set():
            self.ui.status(self.voice.waiting())
            said = self.voice.hear(LISTEN_SLICE_SECONDS)
            if not said:
                continue
            logger.info("Heard: %r", said)
            try:
                self.turn(said)
                complained = False
            except ModelUnavailable as exc:
                # Once per outage. Repeating it every utterance is a machine
                # telling you the same bad news in a loop.
                logger.warning("%s", exc)
                if not complained:
                    complained = True
                    self.voice.say(NO_MODEL)
            except Exception:
                logger.exception("The turn failed; listening again.")


def first_line(result: str, limit: int = 160) -> str:
    """The opening line of a tool result, short enough for one line of log."""
    opening = next((line for line in result.splitlines() if line.strip()), "")
    return opening[:limit] + ("..." if len(opening) > limit else "")


def count(tokens: int) -> str:
    """Tokens as something readable at a glance in the corner of a line."""
    if tokens < 1000:
        return str(tokens)
    if tokens < 10_000:
        return f"{tokens / 1000:.1f}k"
    return f"{round(tokens / 1000)}k"


def with_ears(body: str, has_them: bool) -> str:
    """The prompt with its microphone paragraph kept or taken out.

    Inline in the file behind markers so that whoever opens it reads the whole
    thing, rather than a prompt with a hole in it and the missing piece in
    Python somewhere.
    """
    if EARS_OPEN not in body or EARS_SHUT not in body:
        return body
    before, rest = body.split(EARS_OPEN, 1)
    inside, after = rest.split(EARS_SHUT, 1)
    return before + (inside.strip() if has_them else "").strip() + after


def written_as_words(text: str, names: list[str]) -> str:
    """The tool it meant to call, if it typed the call instead of making one.

    Two shapes, both seen for real. Python-ish, `search_web(query="weather")`,
    and the template's own markup leaking into the content:

        <tool_call> <function=focus_window> <parameter=target> 11 </parameter>

    That second one was read out loud, word for word, and answered with "11".
    Only when the whole reply is the call - a sentence that mentions read_page
    is an explanation, and swallowing a real answer is the worse mistake.
    """
    stripped = text.strip().strip("`").strip()
    if stripped.startswith(("<tool_call", "<function")):
        named = re.search(r"<function=([\w.-]+)", stripped)
        return named.group(1) if named else "a tool"
    for name in names:
        if stripped.startswith(f"{name}(") and stripped.endswith(")"):
            return name
        if stripped == name:
            return name
    return ""


def is_silence(text: str) -> bool:
    """Whether a reply was a deliberate decision not to speak.

    The prompt asks for a single hyphen, and models reach for an ellipsis, an em
    dash or an empty string instead. Anything with no letter or digit in it is
    the same intention, and no real answer is ever shaped like that.
    """
    return not any(character.isalnum() for character in text)


def is_loopback(url: str) -> bool:
    """Whether an endpoint is on this machine, for the startup privacy line."""
    host = (urlparse(url).hostname or "").lower()
    return host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def start(config: Config, service, voice=None, terminal=None) -> Brain:
    """Start the loop against a running service.

    Returns the brain rather than its thread, because escape has to reach
    something: the terminal needs a `cancel()` to call.

    Raises rather than carrying on without a model. It used to log a line and
    leave the voice service up as ears and hands for an agent over MCP, and the
    result was a JARVIS that listened, transcribed, said nothing and looked
    entirely well - which is a worse thing to hand somebody than a process that
    refuses to start and says why.
    """
    model = Model(config.brain, terminal=terminal)
    if why := model.available():
        raise ModelUnavailable(f"no model at {config.brain.url} - {why}")

    brain = Brain(config, voice or ServiceVoice(service), model=model, terminal=terminal)
    # Said before the thread exists, not after. The loop draws its status line
    # the moment it starts, and a log line written afterwards lands on top of it.
    logger.info(
        "Brain on %s, %d tools: %s",
        config.brain.url,
        len(brain.toolbox.names),
        ", ".join(brain.toolbox.names),
    )
    brain.thread = threading.Thread(target=brain.run_forever, name="jarvis-brain", daemon=True)
    brain.thread.start()
    return brain
