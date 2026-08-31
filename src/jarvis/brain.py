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
directly (`tools.py`). Nothing in this file knows about the service; `service.py`
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
    "tags in it. There are no tools left to call. Say in one plain sentence "
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

# Enough for three lines and the headings over them, and no more. Whether a line
# is worth keeping is not a question that gets better with more room to answer
# it in, and this is asked after everything JARVIS says.
LOOK_BACK_TOKENS = 160

# Longest the looking back may take before it is abandoned. Its own, and far
# under `brain.timeout_seconds`: this call happens on the listening thread, so
# a stalled one is a JARVIS that hears nothing until it gives up. Three minutes
# of that has happened, and nothing was learned at the end of it anyway.
LOOK_BACK_SECONDS = 30.0

# How often the endpoint is asked whether it is up yet, while waiting for it at
# startup, and how often that wait says so out loud. Every five seconds because
# a refused connection costs nothing and the thing being waited for is a model
# loading off disk; once a minute because a log line every five seconds for two
# minutes is not news.
MODEL_POLL_SECONDS = 5.0
MODEL_SAY_SECONDS = 60.0

# How many of them are kept from one turn. It is asked after everything JARVIS
# says now, so the ceiling is what stops one talkative afternoon filling the
# file on its own.
LOOK_BACK_LINES = 3

# Asked after the answer has gone to the speakers, so the wait is somebody
# else's. Deliberately hard to say yes to: most turns teach nothing, and a list
# that fills up with "Teams was open" is worse than an empty one.
LOOK_BACK = """/no_think
They have stopped talking. Look back over the last {since}, which is everything
since you last did this rather than only the most recent. Answer in one go.

Anything in it worth still knowing next month that is not already below? Two
kinds count. How this DESKTOP behaves - a window that behaves oddly, a route
that works, one that never does, the name a program is really installed under.
And who you are talking to - their work, what they are building, what they own,
what they enjoy, how they want things done.

Nothing at all is the usual answer. What somebody asks for is not a fact about
them: "open Chrome" is a request, "I always use Chrome" is a preference.
Otherwise at most three lines, each beginning "- ", under a "## " heading -
reusing one from the list below whenever it fits, because two headings for the
same kind of thing is the mess this is here to avoid.

Only what you saw or heard. Never a guess at why, never what you have decided
about them, and nothing you would not say to their face - they can read this
file. Nothing that was merely true at the time: what was open, what was
running, what the clock said. Never a target number, they are renumbered every
scan. Nothing about this conversation itself.

Already written down:
{known}"""

# What an old tool result is replaced with once the window gets tight. Named,
# because "you ran look_at_screen here" is worth keeping and the three thousand
# tokens of numbered targets under it are not.
SQUASHED = "({name} ran here. Its result was dropped to make room - run it again if you need it.)"

# Results shorter than this are left alone. Squashing forty tokens to thirty
# saves nothing and a short result is usually a fact worth having - a filename, a
# path, an answer.
SQUASH_OVER = 400

# Turns whose results survive whatever the pressure: this one and the one before.
# The last scan is what "no, the one below it" refers to.
KEEP_WHOLE = 2

# Room the summary gets. A paragraph or two of plain sentences, which is what
# the prompt below asks for.
SUMMARY_TOKENS = 400

SUMMARISE = """This is the earlier half of a conversation you have been having.
It does not fit any more, so write it down before it goes.

An account of what happened, in the past tense, the way you would tell somebody
about it afterwards. What they asked for, what you did about it, how it turned
out, and anything they told you about themselves or about how they like things
done.

Not a log. No tool names, no target numbers, no exact parameters, no headings
and no lists. None of that means anything now - a number written down here
points at something else by the time it is read. Plain sentences, a paragraph or
two at most, and nothing else: no preamble, no offer to help.

Here is what happened:
{story}"""

# How the summary comes back in. As a user message, because an assistant one
# reads as the last thing JARVIS said and shapes what it does next.
EARLIER = "Before this, in your own words:\n{summary}"

# Sent with an image, because a picture arriving on its own is a turn the model
# has to guess the purpose of.
HERE_IT_IS = "The image you asked to look at:"

# What an image is replaced with once another arrives. Each one is a couple of
# thousand tokens and they stay in the history, so a turn that looked three
# times would spend the window on pictures nobody is asking about any more.
ALREADY_SEEN = "(an image you were shown earlier, no longer attached)"

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
        self._settings: dict | None = None
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
            # First line only - httpx puts an MDN link on the second.
            return str(exc).strip().partition("\n")[0] or type(exc).__name__
        return ""

    def wait_until_available(self, seconds: float, every: float = MODEL_POLL_SECONDS) -> str:
        """Ask until it answers. Empty once it does, otherwise the last reason.

        For the startup race and nothing else: the model server and this are
        both launched at login, in no order, and a 35B model takes a minute or
        two to load. Refusing to start over that is refusing over the order two
        shortcuts happened to fire in.

        It says what it is doing while it waits. A process that sits silent for
        two minutes and then works is indistinguishable from one that has hung,
        and this one is holding the microphone while it does it.
        """
        why = self.available()
        if not why or seconds <= 0:
            return why
        started = time.monotonic()
        logger.info(
            "No model at %s yet - %s. Waiting up to %.0fs for one, asking every %.0fs.",
            self.base,
            why,
            seconds,
            every,
        )
        said = started
        while time.monotonic() - started < seconds:
            time.sleep(every)
            if not (why := self.available()):
                logger.info("The model answered after %.0fs.", time.monotonic() - started)
                return ""
            if time.monotonic() - said >= MODEL_SAY_SECONDS:
                said = time.monotonic()
                logger.info(
                    "Still waiting for %s after %.0fs - %s",
                    self.base,
                    time.monotonic() - started,
                    why,
                )
        return why

    def _props(self) -> dict:
        """What llama.cpp says about itself, once. Not part of the OpenAI shape,
        so an endpoint that has never heard of it answers nothing and everything
        here falls back."""
        if self._settings is None:
            self._settings = {}
            try:
                response = self._client.get(f"{self.base.removesuffix('/v1')}/props", timeout=5.0)
                response.raise_for_status()
                self._settings = response.json() or {}
            except (httpx.HTTPError, ValueError, TypeError):
                logger.debug("The endpoint would not say anything about itself.")
        return self._settings

    def context_limit(self) -> int:
        """How much context the server has, or 0 if it will not say."""
        if self.config.context_limit:
            return self.config.context_limit
        if self._limit is None:
            settings = self._props().get("default_generation_settings") or {}
            try:
                self._limit = int(settings.get("n_ctx") or 0)
            except (ValueError, TypeError):
                self._limit = 0
        return self._limit

    def can_see(self) -> bool | None:
        """Whether a vision projector is loaded, or None if it will not say."""
        modalities = self._props().get("modalities")
        if not isinstance(modalities, dict) or "vision" not in modalities:
            return None
        return bool(modalities["vision"])

    def reply(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        limit: int | None = None,
        watch=None,
        think: bool | None = None,
        stop=None,
        timeout: float | None = None,
    ) -> Reply:
        """One completion. `tools=None` leaves the model nothing to do but write.

        `think` overrides `brain.thinking` for this call alone: reasoning earns
        its cost when a tool has to be chosen, and neither the last word of a
        turn nor the looking back afterwards has one.

        `preserve_thinking` is sent on every call, never conditionally. It is
        what renders an earlier turn's reasoning back into the prompt, and it
        overrides llama-server's own `--no-reasoning-preserve`; sending it only
        sometimes would rewrite the whole prefix and throw away the server's
        cache of it.
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
        # Both are the chat template's own arguments, passed through by
        # llama.cpp's --jinja. An endpoint that does not know them ignores them,
        # which is the right failure.
        payload["chat_template_kwargs"] = {
            "enable_thinking": self.config.thinking if think is None else think,
            "preserve_thinking": True,
        }
        if self.config.stream and not isinstance(self.ui, ui.Silent):
            return self._streamed(payload, watch, stop, timeout)
        try:
            response = self._client.post(
                f"{self.base}/chat/completions",
                json=payload,
                **({} if timeout is None else {"timeout": timeout}),
            )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as exc:
            raise ModelUnavailable(f"No model at {self.base} ({exc}).") from exc
        except ValueError as exc:
            raise ModelUnavailable(f"{self.base} did not return JSON ({exc}).") from exc
        return _read(body)

    def _streamed(
        self, payload: dict, watch=None, stop=None, timeout: float | None = None
    ) -> Reply:
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
                "POST",
                f"{self.base}/chat/completions",
                json=payload,
                **({} if timeout is None else {"timeout": timeout}),
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
    """Pull the one choice apart, tolerating a thin or odd response.

    The reasoning is kept on the message and sent back. Measured against this
    machine's own server: a thought on the last assistant message renders into
    the prompt already, so the tool loop stops re-deriving at step seven what it
    worked out at step three, and one on an earlier turn renders under
    `preserve_thinking`. It is the first thing emptied when the window tightens.
    """
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

    thought = str(message.get("reasoning_content") or "")
    kept: dict = {"role": "assistant", "content": text}
    if thought:
        kept["reasoning_content"] = thought
    if message.get("tool_calls"):
        kept["tool_calls"] = message["tool_calls"]
    used = body.get("usage") or {}
    spent = (int(used.get("prompt_tokens") or 0), int(used.get("completion_tokens") or 0))
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

        The desk is named rather than implied, because this line is drawn on
        the web app too and a phone there is still listening perfectly well.
        Short, because it is a status bar and the room it is about is the room
        the person reading it is standing in.
        """
        if self.service.paused:
            key = self.service.config.service.hotkey or "resume_transcription"
            return f"desk mic off, {key} to toggle"
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
        # Turns answered since the last look back, and when the last one ended.
        # None means there is nothing waiting to be learned from - see settle.
        self._unlearned = 0
        self._quiet_at: float | None = None
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
            memories=self.remembered(),
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

    def _known(self) -> memories.Groups:
        """Every line of it, reference and learned, as the prompt will see it."""
        if not self.settings.memories:
            return []
        written = self._written()
        return memories.load(written[0].parent, written, self.settings.max_memory_chars)

    def _written(self) -> tuple[Path, ...]:
        """The one file JARVIS adds to, which is the one that gets capped."""
        return (tools.memory_file(self.config),)

    # ------------------------------------------------------------------ a turn

    def turn(self, said: list[str]) -> str:
        """Answer one utterance. Returns what was spoken, "" if nothing was.

        Nothing is learned here. Somebody who has just started talking again is
        the worst moment to spend a model call on last week's lesson, and they
        are about to say something else anyway - see `settle`.
        """
        # For a front end whose `hear` blocks, which is chat mode: there is no
        # idle moment to notice in its loop, so the moment they come back after
        # one is the next best thing.
        self.settle()
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
                self.ui.note("Cancelled.")
                return ""
            # Counted, not acted on. Anything it said out loud counts, including
            # the turns that used no tools: half of what is worth keeping is
            # something they said about themselves, and nobody learns that by
            # clicking.
            if spoken:
                self._unlearned += 1
            return spoken
        finally:
            # From the end of the turn rather than the start of it. A turn that
            # spent a minute on tools has not been a quiet minute.
            self._quiet_at = time.monotonic()
            logger.info("Context: %s", self._meter())
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

    def settle(self) -> bool:
        """Look back, if the conversation has been quiet for long enough.

        True if it did. Cheap and idempotent, so it can be called from the
        listening loop every time nothing was heard rather than scheduled on a
        timer that then has to be cancelled by every utterance.

        It used to run on the end of every turn, which is a second model call on
        every single answer and most of them about nothing. Quiet is both the
        cheaper moment and the better one: nobody is waiting, and by then a run
        of turns has usually happened, so the one call sees what an exchange
        added up to rather than one line out of the middle of it.
        """
        if not (self.settings.consolidate and self.settings.memories):
            return False
        if not self._unlearned or self._quiet_at is None:
            return False
        if time.monotonic() - self._quiet_at < self.settings.settle_seconds:
            return False
        turns, self._unlearned, self._quiet_at = self._unlearned, 0, None
        self._look_back(turns)
        return True

    def _look_back(self, turns: int = 1) -> None:
        """Write down anything those turns taught, about the desk or about them.

        The only way a lesson outlives the conversation it was learned in
        without somebody typing it up. Everything about it is deliberately quiet:
        it happens behind the speech, its thinking goes to the live line and then
        vanishes like any other, and it never touches the conversation - the
        question is asked over a copy and the answer is thrown away.
        """
        self.ui.status("learning")
        known = memories.as_lines(self._known()) or "- nothing yet"
        since = "exchange" if turns <= 1 else f"{turns} exchanges"
        asked = [
            *self.messages,
            {"role": "user", "content": LOOK_BACK.format(known=known, since=since)},
        ]
        try:
            # No reasoning, and a short leash. Reasoning earns its cost when a
            # tool has to be chosen, and this call has no tools: left on, it
            # spent four thousand characters weighing up whether one line was
            # worth keeping and then ran out of room before writing it.
            #
            # Asked twice, because once is not enough. `enable_thinking` is a
            # chat template argument and a fine-tuned template is free to ignore
            # it - this one does, and reasoned its way through every turn until
            # `/no_think` went in the prompt as well.
            reply = self.model.reply(
                asked,
                tools=None,
                limit=LOOK_BACK_TOKENS,
                think=False,
                stop=self.stopped.is_set,
                timeout=LOOK_BACK_SECONDS,
            )
        except Cancelled:
            return
        except Exception:
            logger.exception("Looking back failed; carrying on without it.")
            return

        if reply.thinking:
            logger.info("looking back: %s", " ".join(reply.thinking.split()))
        learned = [
            (heading, lesson)
            for heading, lessons in memories.sections_in(reply.text)
            for lesson in lessons
        ]
        path = tools.memory_file(self.config)
        for heading, lesson in learned[:LOOK_BACK_LINES]:
            if PER_SCAN.search(lesson):
                logger.info("Not kept, target numbers do not survive the scan: %s", lesson)
                continue
            logger.info("Learned under %s: %s", heading, lesson)
            memories.remember(path, heading, lesson, self.settings.max_memory_chars)

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
            self._show_the_pictures()

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

    def _show_the_pictures(self) -> None:
        """Hand over anything look_at_image asked to be seen.

        A tool result is text and no endpoint takes an image on a `tool`
        message, so a picture travels as the user message after it. That is the
        whole reason looking is two calls: the first says one is coming, the
        second is where it can be described.
        """
        waiting = list(self.toolbox.images)
        if not waiting:
            return
        self.toolbox.images.clear()
        # Only the latest survives. Anything else and a turn that looks twice
        # carries both for the rest of the conversation.
        for message in self.messages:
            if message.get("role") == "user" and not isinstance(message.get("content"), str):
                message["content"] = ALREADY_SEEN
        self.messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": HERE_IT_IS},
                    *({"type": "image_url", "image_url": {"url": url}} for url in waiting),
                ],
            }
        )
        logger.info("Attached %d image(s) to the conversation.", len(waiting))

    def _ask(self, tools, limit: int | None = None, think: bool | None = None) -> Reply:
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
            think=think,
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
            self.ui.meter(self._meter())
        return reply

    def _meter(self) -> str:
        """What the corner of the terminal says, in one line.

        Written to the log at the end of every turn as well as drawn. The corner
        is gone the moment the window scrolls or the session ends, and "was it
        already at 80k when that happened" is the first question worth asking
        about a turn that went strangely.
        """
        room = f"{self._room} - " if self._room else ""
        return f"{room}in {count(self._tokens_in)} - out {count(self._tokens_out)}"

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

        Reasoning is off. There is nothing left to decide - the tools are gone
        and one sentence is owed - and reasoning here does not deliberate, it
        ruminates. A live session spent it rewriting one sentence twenty times,
        counting its own words and arguing with itself about whether the prompt
        wanted a clock time in an answer about Task Manager. It put one in.

        Measured on the same conversation: 2620 tokens out and 10270 characters
        of reasoning with it on, against 21 tokens and none with it off, for the
        same sentence. Somebody is listening to silence for the difference.

        Two things still go wrong here and both were live. It can TYPE a call
        instead of making one - `<tool_call> <function=look_at_screen>` was read
        out with the tags in it. And it can spend the whole token budget
        thinking: 2473 characters of reasoning stopping mid sentence against a
        cap of 600, with no answer written at all, because the reasoning and the
        answer come out of the same allowance.

        So the last thing said is checked like everything else, and it gets one
        more go with twice the room.
        """
        reply = self._ask(None, think=False)
        self.messages.append(reply.message)
        if not (trouble := self._unusable(reply)):
            return reply.text

        logger.warning("No answer to speak - %s: %r", trouble, reply.text[:70])
        self.messages.append({"role": "user", "content": OUT_OF_STEPS})
        # Twice the budget, because running out of room mid thought is one of
        # the two ways to get here and asking again inside the same cap would
        # get the same nothing.
        second = self._ask(None, limit=self.settings.max_tokens * 2, think=False)
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

    def _squash(self) -> None:
        """Empty the droppable half of the conversation, oldest first.

        The conversation splits in two. Kept: what they asked, what was called,
        what was answered. Droppable: the reasoning behind a call, and the
        result it came back with. Those two are worth the same nothing an hour
        later - a crowded window is three thousand tokens of numbered targets
        that were stale the moment anything was clicked, and the thought that
        chose them was about a screen that has since changed.

        So above the ceiling they go, oldest first and whichever comes first,
        until it is back under. A thought simply goes; a result leaves a line
        naming the tool, and its call keeps its id, so nothing is orphaned and
        the endpoint still sees a well formed conversation. That is the whole
        reason this is cheaper than dropping turns. It runs before everything
        else and usually means there is nothing else to do.
        """
        ceiling = self._ceiling(self.settings.squash_fraction)
        if self._spent <= ceiling:
            return

        starts = self._turns(indexes=True)
        recent = starts[-KEEP_WHOLE] if len(starts) >= KEEP_WHOLE else 0
        named = {
            call.get("id"): (call.get("function") or {}).get("name") or "A tool"
            for message in self.messages
            for call in message.get("tool_calls") or []
        }

        saved = thoughts = results = 0
        for message in self.messages[:recent]:
            if self._spent - saved <= ceiling:
                break
            # No floor on a thought, unlike a result: nothing stands in for it,
            # so emptying one always wins however short it was.
            if thought := message.pop("reasoning_content", None):
                saved += len(thought) // 4
                thoughts += 1
                continue
            was = message.get("content")
            if message.get("role") != "tool" or not isinstance(was, str):
                continue
            if len(was) < SQUASH_OVER:
                continue
            message["content"] = SQUASHED.format(
                name=named.get(message.get("tool_call_id"), "A tool")
            )
            # Four characters to the token, which is close enough to decide with
            # and wrong in the safe direction on a list of numbers.
            saved += (len(was) - len(message["content"])) // 4
            results += 1

        if thoughts or results:
            logger.info(
                "Emptied %d old result(s) and %d thought(s), about %s tokens.",
                results,
                thoughts,
                count(saved),
            )
            # An estimate standing in until the next call measures it for real.
            self._spent -= saved

    def _droppable(self) -> int:
        """Roughly what another squash could still reclaim from here."""
        loose = 0
        for message in self.messages:
            if thought := message.get("reasoning_content"):
                loose += len(thought) // 4
            body = message.get("content")
            if message.get("role") == "tool" and isinstance(body, str) and len(body) >= SQUASH_OVER:
                loose += (len(body) - len(SQUASHED)) // 4
        return loose

    def _summarise(self) -> None:
        """Replace the oldest half of what cannot be dropped with an account of it.

        The last thing tried before turns start disappearing, and it only comes
        up when the kept half is itself most of the ceiling - by then every
        result and every thought has already gone and there is nothing cheap
        left to take.

        The oldest half of the turns becomes one paragraph in the model's own
        words. Deliberately a story rather than a log: exact parameters and
        target numbers are the first thing to stop being true, and a number
        written down here points at something else by the time anybody reads it.
        What survives is what the conversation was about, which is what somebody
        asking "what did we decide" actually wanted.

        Cut at a turn boundary, like the trim, so no tool result outlives the
        call it answered. If nothing comes back the conversation is left exactly
        as it was and the trim takes it from here.
        """
        if not self.settings.summarise_fraction:
            return
        ceiling = int(self._ceiling() * self.settings.summarise_fraction)
        if not ceiling or max(0, self._spent - self._droppable()) <= ceiling:
            return

        starts = self._turns(indexes=True)
        if len(starts) < 2 * KEEP_WHOLE:
            return
        cut = starts[len(starts) // 2]
        going = self.messages[1:cut]
        story = as_story(going)
        if not story:
            return

        self.ui.status("remembering")
        logger.info("Summarising the oldest %d of %d turns.", len(starts) // 2, len(starts))
        try:
            reply = self.model.reply(
                [{"role": "user", "content": SUMMARISE.format(story=story)}],
                tools=None,
                limit=SUMMARY_TOKENS,
                think=False,
                stop=self.stopped.is_set,
            )
        except Cancelled:
            return
        except Exception:
            logger.exception("Summarising failed; carrying on without it.")
            return

        summary = reply.text.strip()
        if not summary:
            logger.info("Nothing came back, so the conversation is left as it was.")
            return

        kept = {"role": "user", "content": EARLIER.format(summary=summary)}
        self._spent -= sum(weigh(message) for message in going) - weigh(kept)
        self.messages[1:cut] = [kept]
        logger.info("Summarised. About %s tokens now.", count(max(0, self._spent)))

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

        Two gentler things run first. _squash empties the droppable half and
        _summarise rewrites the oldest turns as prose; both leave a conversation
        that still makes sense. A turn is only deleted when neither was enough.
        """
        self._squash()
        self._summarise()
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

    def _ceiling(self, fraction: float | None = None) -> int:
        """Most of the window the conversation may take, or none if unknown."""
        if fraction is None:
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
                # Nothing said is the whole signal. A minute of these is a
                # conversation that has finished for now.
                self.settle()
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


def weigh(message: dict) -> int:
    """Roughly what one message costs, for decisions taken between requests.

    Four characters to the token, and it lands either side of the truth: 4.33
    characters per token across a session's worth of reasoning, 3.89 on one
    dense sample, where the real cost of a kept thought was 301 tokens against
    292 estimated. Close enough to decide with, and never the number shown - the
    meter reads what the endpoint charged.
    """
    total = len(str(message.get("content") or "")) + len(
        str(message.get("reasoning_content") or "")
    )
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        total += len(str(function.get("name") or "")) + len(str(function.get("arguments") or ""))
    return total // 4


def as_story(messages: list[dict]) -> str:
    """The kept half of a conversation as plain lines, for something to summarise.

    What was asked, what was answered, and the name of anything called. Not the
    results and not the reasoning: those are the droppable half by definition
    and have usually gone already, and pasting a scan in here would be
    summarising the one part that was never worth keeping in the first place.
    """
    lines = []
    for message in messages:
        role = message.get("role")
        body = " ".join(str(message.get("content") or "").split())
        if role == "user" and body:
            lines.append(f"They said: {body}")
        elif role == "assistant":
            if body:
                lines.append(f"You said: {body}")
            for call in message.get("tool_calls") or []:
                name = (call.get("function") or {}).get("name") or "a tool"
                lines.append(f"You used {name}.")
    return "\n".join(lines)


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

    Rejoined as paragraphs either way. The markers used to be cut out where
    they stood, which left the block welded to the end of the line above it.
    """
    if EARS_OPEN not in body or EARS_SHUT not in body:
        return body
    before, rest = body.split(EARS_OPEN, 1)
    inside, after = rest.split(EARS_SHUT, 1)
    parts = [before.strip(), inside.strip() if has_them else "", after.strip()]
    return "\n\n".join(part for part in parts if part)


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

    Waits for the endpoint rather than requiring it to be up first, because
    nothing sequences the two at login - see `wait_until_available`.

    Raises once that wait is spent rather than carrying on without a model. It
    used to log a line and leave the voice service up as ears and hands, and the
    result was a JARVIS that listened, transcribed, said nothing and looked
    entirely well - which is a worse thing to hand somebody than a process that
    refuses to start and says why.
    """
    model = Model(config.brain, terminal=terminal)
    if why := model.wait_until_available(config.brain.wait_for_model_seconds):
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
    if config.brain.images and model.can_see() is False:
        logger.warning(
            "brain.images is on but %s reports no vision, so look_at_image will send a "
            "picture to something with no eyes. Load a projector (--mmproj) or set "
            "brain.images false.",
            config.brain.url,
        )
    brain.thread = threading.Thread(target=brain.run_forever, name="jarvis-brain", daemon=True)
    brain.thread.start()
    return brain
