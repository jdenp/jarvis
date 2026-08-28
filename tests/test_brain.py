"""JARVIS's own agent loop.

The point of the whole thing is in one line of `turn()`: the model's reply is
what gets spoken. There is no say tool to forget, so most of these tests are
about the two ways a turn could still end in silence - the step budget running
out, and a model that returns nothing - and how each one is closed.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from jarvis.brain import (
    NO_MODEL,
    NOTHING_TO_SAY,
    Brain,
    Call,
    ModelUnavailable,
    Reply,
    ServiceVoice,
    is_loopback,
    is_silence,
)
from jarvis.config import Config
from jarvis.tools import Tool, Toolbox


class FakeModel:
    """Replies in the order given, and remembers what it was asked."""

    def __init__(self, *replies: Reply) -> None:
        self.replies = list(replies)
        self.asked: list[tuple[int, bool]] = []  # (messages, tools offered)
        self.preloads = 0
        self.watched: list[bool] = []
        self.stopped: list = []
        self.limits: list[int | None] = []
        self.interrupt: list[list[str]] = []
        self.raise_next: Exception | None = None

    def context_limit(self) -> int:
        return 98304

    def reply(self, messages, tools=None, limit=None, watch=None, think=None, stop=None) -> Reply:
        if self.raise_next is not None:
            raise self.raise_next
        if limit == 1:
            self.preloads += 1
            return said("")
        self.watched.append(watch is not None)
        self.stopped.append(stop)
        self.limits.append(limit)
        # Whatever is queued to be said over the top of this one.
        if self.interrupt and watch is not None:
            from jarvis.brain import Interrupted

            raise Interrupted(self.interrupt.pop(0))
        self.asked.append((len(messages), bool(tools)))
        if not self.replies:
            return said("")
        return self.replies.pop(0)


class FakeVoice:
    """Ears and mouth, scripted."""

    def __init__(self, *turns: list[str]) -> None:
        self.turns = list(turns)
        self.spoken: list[str] = []
        self.mid_task: list[list[str]] = []
        self.hushed = 0

    def hear(self, timeout: float) -> list[str]:
        if timeout == 0.0:
            return self.mid_task.pop(0) if self.mid_task else []
        return self.turns.pop(0) if self.turns else []

    def say(self, text: str) -> None:
        self.spoken.append(text)

    def hush(self) -> None:
        self.hushed += 1

    def waiting(self) -> str:
        return "listening"


def said(text: str) -> Reply:
    return Reply(text=text, message={"role": "assistant", "content": text})


def calling(name: str, text: str = "", **arguments) -> Reply:
    call = Call(id="c1", name=name, arguments=arguments)
    return Reply(
        text=text,
        calls=(call,),
        message={
            "role": "assistant",
            "content": text,
            "tool_calls": [{"id": "c1", "function": {"name": name, "arguments": "{}"}}],
        },
    )


def toolbox(**results) -> Toolbox:
    """A toolbox whose tools just report that they ran."""
    ran: list[str] = []

    def recorder(name, result):
        def run(**arguments):
            ran.append(name)
            return result

        return run

    tools = [
        Tool(name=name, description=name, run=recorder(name, result))
        for name, result in results.items()
    ]
    box = Toolbox(tools)
    box.ran = ran  # type: ignore[attr-defined]
    return box


def brain(*replies, voice=None, config=None, box=None) -> Brain:
    """A brain for testing the answering loop.

    Looking back is off. It is a separate concern with its own tests below, and
    left on it would make an extra model call after every turn here - and write
    into the repository's own context directory to do it.
    """
    config = config or Config()
    return Brain(
        replace(config, brain=replace(config.brain, consolidate=False)),
        voice or FakeVoice(),
        model=FakeModel(*replies),
        toolbox=box or toolbox(),
    )


def looking_back(tmp_path, *replies, box=None) -> Brain:
    """One that does look back, writing somewhere harmless.

    Its tool refuses, because a turn that went perfectly is no longer looked
    back at - there is nothing to learn from a route that worked first time.
    """
    config = replace(
        Config(),
        brain=replace(
            Config().brain,
            memories_file=str(tmp_path / "memories.md"),
            navigation_file=str(tmp_path / "navigation" / "user-navigation.md"),
        ),
    )
    return Brain(
        config,
        FakeVoice(),
        model=FakeModel(*replies),
        toolbox=box or refusing(),
    )


def refusing() -> Toolbox:
    """A toolbox whose one tool refuses, the way a stale target number does."""

    def refuse(**arguments):
        raise ValueError("Target 3 was 'Close', but something else is there now")

    return Toolbox([Tool(name="look_at_screen", description="look", run=refuse)])


# ------------------------------------------------------------------ escape


def test_escape_with_nothing_happening_does_nothing():
    """Which is what tells the terminal not to put a prompt up for no reason."""
    assert brain().cancel() is False


def test_a_cancelled_turn_says_nothing_and_leaves_no_trace():
    """The question goes with the answer. A half worked request left in the
    history is one they have already withdrawn, and an assistant message whose
    tool calls were never answered is one the endpoint refuses outright."""
    from jarvis.brain import Cancelled

    it = brain(said("Half past two, sir."))
    before = list(it.messages)
    it.model.raise_next = Cancelled()
    assert it.turn(["what time is it"]) == ""
    assert it.voice.spoken == []
    assert it.messages == before


def test_escape_lands_as_soon_as_the_tool_it_arrived_during_finishes():
    """Checked between steps as well as mid stream, so a cancel during a slow
    command costs nothing more than the rest of that command."""
    held: list[Brain] = []

    def press_escape(**arguments):
        assert held[0].cancel() is True, "there was something to stop"
        return "Taskbar - 25 targets"

    box = Toolbox([Tool(name="look_at_screen", description="look", run=press_escape)])
    it = brain(calling("look_at_screen"), said("Open, sir."), box=box)
    held.append(it)

    assert it.turn(["is spotify open"]) == ""
    assert it.voice.spoken == [], "the answer it was about to write is gone with it"
    assert it.voice.hushed == 1, "and it stopped talking"


def test_a_cancel_does_not_carry_into_the_next_turn():
    """Otherwise one escape kills every turn after it."""
    from jarvis.brain import Cancelled

    it = brain(said("Half past two, sir."))
    it.model.raise_next = Cancelled()
    it.turn(["what time is it"])
    it.model.raise_next = None
    it.model.replies = [said("Tuesday, sir.")]
    assert it.turn(["what day is it"]) == "Tuesday, sir."


def test_the_stream_is_given_a_way_to_be_stopped():
    """The flag has to reach the reading of the stream or escape only works
    between calls, which is most of a minute when it is thinking hard."""
    it = brain(said("Done, sir."))
    it.turn(["do something"])
    assert it.model.stopped and all(callable(check) for check in it.model.stopped)


# ------------------------------------------------------------ the reply is said


def test_the_reply_is_spoken_because_it_is_the_reply():
    """No say tool, nothing to forget. This is the entire reason 0.8.0 exists -
    DESIGN.md has the five mechanisms that tried to get here from the outside."""
    it = brain(said("Half past two, sir."))
    assert it.turn(["what time is it"]) == "Half past two, sir."
    assert it.voice.spoken == ["Half past two, sir."]


def test_markdown_and_emoji_are_stripped_on_the_way_out():
    """SAPI reads `**947**` as "asterisk asterisk nine four seven"."""
    it = brain(said("That is **done**, sir \U0001f44d"))
    assert it.turn(["is it done"]) == "That is done, sir"


def test_a_reply_with_no_letters_in_it_is_deliberate_silence():
    """No wake word, so some of what arrives is other people and videos. The
    prompt asks for a hyphen; models reach for an ellipsis or a dash instead."""
    it = brain(said("-"))
    assert it.turn(["...so anyway I told him no"]) == ""
    assert it.voice.spoken == []


@pytest.mark.parametrize("quiet", ["-", "...", "--", "   ", "—"])
def test_every_shape_of_staying_quiet_counts(quiet):
    assert is_silence(quiet) is True


def test_a_real_answer_is_never_mistaken_for_silence():
    assert is_silence("No.") is False
    assert is_silence("6") is False


# ------------------------------------------------------------------ using tools


def test_a_tool_runs_and_then_the_answer_is_spoken():
    box = toolbox(press_keys="Pressed playpause")
    it = brain(
        calling("press_keys", keys="playpause"),
        said("Paused, sir."),
        box=box,
    )
    assert it.turn(["pause the music"]) == "Paused, sir."
    assert box.ran == ["press_keys"]
    assert it.voice.spoken == ["Paused, sir."]


def test_the_tool_result_reaches_the_model():
    box = toolbox(look_at_screen="Spotify - 3 targets")
    it = brain(calling("look_at_screen"), said("Spotify is up, sir."), box=box)
    it.turn(["what is on screen"])
    results = [m for m in it.messages if m["role"] == "tool"]
    assert results == [{"role": "tool", "tool_call_id": "c1", "content": "Spotify - 3 targets"}]


def test_a_line_written_beside_the_first_tool_call_is_the_lead_in():
    """The work that follows is seconds of silence, and silence is
    indistinguishable from a crash to somebody who can only hear."""
    box = toolbox(look_at_screen="Taskbar - 25 targets")
    it = brain(
        calling("look_at_screen", text="Let me have a look, sir."),
        said("Spotify is open."),
        box=box,
    )
    it.turn(["is spotify open"])
    assert it.voice.spoken == ["Let me have a look, sir.", "Spotify is open."]


def test_nothing_is_narrated_after_the_first_step():
    """Saying something after every tool call is narration nobody asked for."""
    box = toolbox(look_at_screen="ok", click="clicked")
    it = brain(
        calling("look_at_screen", text="One moment."),
        calling("click", text="Now clicking play.", target=8, expecting="Play"),
        said("Playing, sir."),
        box=box,
    )
    it.turn(["play something"])
    assert it.voice.spoken == ["One moment.", "Playing, sir."]


def test_an_unknown_tool_comes_back_as_a_result_not_an_exception():
    box = toolbox(press_keys="pressed")
    it = brain(calling("play_music"), said("I could not do that, sir."), box=box)
    it.turn(["play music"])
    result = next(m for m in it.messages if m["role"] == "tool")
    assert "no tool called 'play_music'" in result["content"]
    assert "press_keys" in result["content"], "and what it does have"


def test_arguments_that_are_not_json_are_answered_rather_than_dropped():
    """A tool call with no result leaves the conversation unable to continue."""
    broken = Reply(
        text="",
        calls=(Call(id="c1", name="press_keys", broken="the arguments were not valid JSON"),),
        message={"role": "assistant", "content": ""},
    )
    it = brain(broken, said("Sorry sir, I fumbled that."), box=toolbox(press_keys="pressed"))
    it.turn(["pause"])
    result = next(m for m in it.messages if m["role"] == "tool")
    assert "not valid JSON" in result["content"]
    assert "JSON object" in result["content"], "and how to send it again"


# ----------------------------------------------------- the turn cannot go quiet


def test_running_out_of_steps_still_ends_in_an_answer():
    """The step budget used to be where a turn could die quietly. The last call
    is made with the tools taken away, so prose is the only move left."""
    config = replace(Config(), brain=replace(Config().brain, max_steps=2))
    box = toolbox(look_at_screen="ok")
    it = brain(
        calling("look_at_screen"),
        calling("look_at_screen"),
        said("I could not find it, sir."),
        box=box,
        config=config,
    )
    assert it.turn(["find the reply button"]) == "I could not find it, sir."
    assert it.model.asked[-1][1] is False, "the last call offered no tools"
    assert [offered for _, offered in it.model.asked[:-1]] == [True, True]


def test_work_done_and_nothing_to_say_is_reported_out_loud():
    """Silence after four tool calls reads as a crash. Saying so is worse than
    an answer and better than nothing."""
    box = toolbox(click="clicked")
    it = brain(calling("click", target=1, expecting="Play"), said(""), said(""), box=box)
    assert it.turn(["press play"]) == NOTHING_TO_SAY
    assert it.voice.spoken == [NOTHING_TO_SAY]


def test_a_model_that_says_nothing_at_all_gets_asked_once_more():
    it = brain(said(""), said("Sorry sir, I was miles away."))
    assert it.turn(["hello"]) == "Sorry sir, I was miles away."
    assert it.model.asked[-1][1] is False


# --------------------------------------------------------------- being cut into


def test_something_said_mid_task_reaches_the_model_before_it_carries_on():
    """What owning the loop buys. Nothing can preempt a turn from outside, so
    the turn looks - and "no, the other one" lands before the wrong thing is
    done rather than after."""
    voice = FakeVoice()
    voice.mid_task = [["no, the other one"]]
    box = toolbox(look_at_screen="two windows")
    it = brain(calling("look_at_screen"), said("The other one it is, sir."), box=box)
    it.voice = voice
    it.turn(["open the first one"])
    interruption = [m for m in it.messages if m["role"] == "user"][-1]
    assert "no, the other one" in interruption["content"]
    assert "while you were working" in interruption["content"]


# -------------------------------------------------------------------- history


def test_old_turns_are_dropped_whole_and_the_prompt_is_kept():
    """Cutting mid turn leaves a tool result whose call is gone, which some
    endpoints reject outright."""
    config = replace(Config(), brain=replace(Config().brain, history_turns=2))
    it = brain(*[said(f"reply {n}") for n in range(4)], config=config)
    for n in range(4):
        it.turn([f"question {n}"])

    assert it.messages[0]["role"] == "system"
    users = [m["content"] for m in it.messages if m["role"] == "user"]
    assert users == ["question 2", "question 3"]


def test_a_short_conversation_is_kept_entire():
    it = brain(said("one"), said("two"))
    it.turn(["first"])
    it.turn(["second"])
    assert [m["content"] for m in it.messages if m["role"] == "user"] == ["first", "second"]


def test_the_system_prompt_names_the_tools_that_exist():
    it = brain(box=toolbox(look_at_screen="", run_command=""))
    prompt = it.messages[0]["content"]
    assert "look_at_screen, run_command" in prompt
    assert "SPOKEN ALOUD" in prompt


def test_the_prompt_is_a_file_rather_than_a_string_in_the_code():
    """It is prose, tuned by reading it out loud and changing a word. There is
    no copy in Python for it to drift from."""
    from jarvis.brain import SOUL
    from jarvis.config import project_root

    assert (project_root() / SOUL).is_file()
    assert "SPOKEN ALOUD" in (project_root() / SOUL).read_text(encoding="utf-8")


def test_another_prompt_file_can_be_named(tmp_path):
    path = tmp_path / "prompt.md"
    path.write_text("You are a lamp.", encoding="utf-8")
    config = replace(Config(), brain=replace(Config().brain, system_prompt_file=str(path)))
    assert brain(config=config).messages[0]["content"] == "You are a lamp."


def test_a_missing_prompt_file_says_which_one(tmp_path):
    """No fallback: a JARVIS with a stand-in personality and no obvious cause is
    worse than one that will not start and names the file."""
    config = replace(
        Config(), brain=replace(Config().brain, system_prompt_file=str(tmp_path / "gone.md"))
    )
    with pytest.raises(OSError, match=r"gone\.md"):
        brain(config=config)


def test_the_microphone_paragraph_is_taken_out_when_there_is_none():
    """A prompt naming a tool that is not there invites a call that comes
    straight back as an error."""
    from jarvis.brain import with_ears

    body = "before\n<!-- ears -->\nyou can close your ears\n<!-- /ears -->\nafter"
    assert "close your ears" in with_ears(body, True)
    assert "close your ears" not in with_ears(body, False)
    assert with_ears(body, False).startswith("before")
    assert with_ears(body, False).endswith("after")


def test_a_prompt_with_no_markers_is_left_alone():
    from jarvis.brain import with_ears

    assert with_ears("just prose", True) == "just prose"
    assert with_ears("just prose", False) == "just prose"


# ----------------------------------------------------------------- the outer loop


def test_an_unreachable_model_is_reported_once_not_every_time():
    """A machine telling you the same bad news three times is worse than the
    news. It says it again once the endpoint has answered and failed afresh."""
    import threading

    voice = FakeVoice(["hello"], ["are you there"], ["hello?"])
    it = brain(voice=voice)
    it.model.raise_next = ModelUnavailable("no model at 127.0.0.1:8081")

    stop = threading.Event()
    original = voice.hear

    def hear(timeout):
        heard = original(timeout)
        if not voice.turns:
            stop.set()
        return heard

    voice.hear = hear
    it.run_forever(stop)
    assert voice.spoken == [NO_MODEL]


def test_a_failing_turn_does_not_take_the_loop_down():
    import threading

    voice = FakeVoice(["hello"])
    it = brain(voice=voice)
    it.model.raise_next = RuntimeError("something unexpected")
    stop = threading.Event()

    original = voice.hear

    def hear(timeout):
        heard = original(timeout)
        if not voice.turns:
            stop.set()
        return heard

    voice.hear = hear
    it.run_forever(stop)  # the assertion is that this returns at all
    assert voice.spoken == []


# ------------------------------------------------------------------ the plumbing


def test_the_service_voice_reads_forward_and_never_repeats():
    from jarvis.transcript import Transcript

    class FakeService:
        def __init__(self):
            self.transcript = Transcript()
            self.spoken = []

        def say(self, text):
            self.spoken.append(text)

    service = FakeService()
    voice = ServiceVoice(service)
    service.transcript.add("play some music")
    assert voice.hear(0.1) == ["play some music"]
    assert voice.hear(0.01) == [], "already read"
    service.transcript.add("louder")
    assert voice.hear(0.1) == ["louder"]

    voice.say("Turning it up, sir.")
    assert service.spoken == ["Turning it up, sir."]


def test_both_halves_of_a_split_request_arrive_together():
    """A phrase ends after a fixed silence rather than when a sentence does, so
    one request can land as two utterances."""
    from jarvis.transcript import Transcript

    class FakeService:
        def __init__(self):
            self.transcript = Transcript()

        def say(self, text):
            pass

    service = FakeService()
    voice = ServiceVoice(service)
    service.transcript.add("open spotify and")
    service.transcript.add("play something quiet")
    assert voice.hear(0.1) == ["open spotify and", "play something quiet"]


# ------------------------------------------------------------------- warming up


def test_the_first_answer_is_not_the_one_that_pays_for_the_prompt():
    """The system prompt and the tool schemas are most of every request and
    never change, so a server that reuses a cached prefix only processes them
    once. Doing it at startup spends nobody's time instead of the first answer's.
    """
    import threading

    voice = FakeVoice()
    it = brain(voice=voice)
    stop = threading.Event()
    stop.set()
    it.run_forever(stop)
    assert it.model.preloads == 1


def test_the_warm_up_is_not_part_of_the_conversation():
    it = brain()
    before = list(it.messages)
    it.preload()
    assert it.messages == before, "it never happened"


def test_a_model_that_is_not_up_yet_does_not_take_the_loop_with_it():
    it = brain()
    it.model.raise_next = ModelUnavailable("connection refused")
    it.preload()  # the assertion is that this returns


def test_warming_up_can_be_switched_off():
    import threading

    config = replace(Config(), brain=replace(Config().brain, preload=False))
    it = brain(config=config)
    stop = threading.Event()
    stop.set()
    it.run_forever(stop)
    assert it.model.preloads == 0


# --------------------------------------------------------------- what it is doing


def test_the_live_line_does_not_claim_to_be_listening_while_deaf():
    """A status line that says listening while the microphone is shut is how a
    working hotkey comes to look like a broken one."""
    from jarvis.transcript import Transcript

    class FakeService:
        def __init__(self):
            self.transcript = Transcript()
            self.config = Config()

        def say(self, text):
            pass

    service = FakeService()
    voice = ServiceVoice(service)
    assert voice.waiting() == "listening"

    service.transcript.pause()
    assert "not listening" in voice.waiting()
    assert Config().service.hotkey in voice.waiting(), "and which key brings it back"


# ------------------------------------------------------------- reading the wire


def test_a_llama_server_response_is_read():
    """The shape llama-server returns with --jinja, which is the part that
    parses tool calls on the OpenAI-compatible endpoint."""
    from jarvis.brain import _read

    reply = _read(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Let me have a look, sir.",
                        "tool_calls": [
                            {
                                "id": "call_abc",
                                "type": "function",
                                "function": {
                                    "name": "look_at_screen",
                                    "arguments": '{"window":"Taskbar"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }
    )
    assert reply.text == "Let me have a look, sir."
    assert reply.calls[0].name == "look_at_screen"
    assert reply.calls[0].arguments == {"window": "Taskbar"}
    assert reply.calls[0].broken == ""


def test_reasoning_is_not_echoed_back():
    """Qwen returns its thinking in a field of its own. Sending it back costs
    tokens on every subsequent call and the endpoint does not want it."""
    from jarvis.brain import _read

    reply = _read(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Half past two.",
                        "reasoning_content": "The user asked for the time. I should...",
                    }
                }
            ]
        }
    )
    assert reply.message == {"role": "assistant", "content": "Half past two."}


def test_malformed_arguments_are_carried_rather_than_thrown():
    from jarvis.brain import _read

    reply = _read(
        {
            "choices": [
                {
                    "message": {
                        "tool_calls": [{"function": {"name": "click", "arguments": "{target: 1"}}]
                    }
                }
            ]
        }
    )
    assert reply.calls[0].name == "click"
    assert reply.calls[0].broken
    assert reply.calls[0].id, "an id is invented if the endpoint omitted one"


def test_an_empty_response_is_not_a_crash():
    from jarvis.brain import _read

    assert _read({}).text == ""
    assert _read({"choices": []}).calls == ()


@pytest.mark.parametrize(
    "url,local",
    [
        ("http://127.0.0.1:8081/v1", True),
        ("http://localhost:8081/v1", True),
        ("https://api.example.com/v1", False),
        ("http://192.168.1.40:8081/v1", False),
    ],
)
def test_whether_the_model_is_on_this_machine(url, local):
    """It feeds the startup line that claims nothing leaves the machine, so it
    has to be right about an endpoint that is somebody else's."""
    assert is_loopback(url) is local


# ------------------------------------------------------------------- streaming


def test_a_streamed_reply_is_reassembled_into_one():
    """A half-read stream is a failure, not a short answer, so the caller gets
    the same Reply either way."""
    from jarvis.brain import _collect

    calls: dict[int, dict] = {}
    _collect(calls, [{"index": 0, "id": "c1", "function": {"name": "click"}}])
    _collect(calls, [{"index": 0, "function": {"arguments": '{"target":'}}])
    _collect(calls, [{"index": 0, "function": {"arguments": " 8}"}}])
    assert calls[0]["id"] == "c1"
    assert calls[0]["function"] == {"name": "click", "arguments": '{"target": 8}'}


def test_two_interleaved_calls_are_kept_apart():
    """Keyed by index rather than arrival, because a model can start a second
    call before finishing the first."""
    from jarvis.brain import _collect

    calls: dict[int, dict] = {}
    _collect(
        calls,
        [
            {"index": 0, "id": "a", "function": {"name": "click", "arguments": "{"}},
            {"index": 1, "id": "b", "function": {"name": "scroll", "arguments": "{"}},
        ],
    )
    _collect(calls, [{"index": 1, "function": {"arguments": "}"}}])
    _collect(calls, [{"index": 0, "function": {"arguments": "}"}}])
    assert [calls[i]["function"]["name"] for i in sorted(calls)] == ["click", "scroll"]
    assert all(calls[i]["function"]["arguments"] == "{}" for i in calls)


def test_rubbish_in_the_deltas_is_stepped_over():
    from jarvis.brain import _collect

    calls: dict[int, dict] = {}
    _collect(calls, ["not a dict", None])
    assert calls == {}


# --------------------------------------------- a call typed out instead of made


def test_a_tool_call_written_as_words_is_not_read_out():
    """Seen for real with reasoning off and ten tools in front of it. Unchecked,
    the answer that reaches them is `search_web(query="the weather")`, aloud."""
    box = toolbox(search_web="1. Melbourne weather - 15 degrees")
    it = brain(
        said('search_web(query="weather in melbourne")'),
        calling("search_web", query="weather in melbourne"),
        said("Fifteen degrees and overcast, sir."),
        box=box,
    )
    assert it.turn(["what is the weather"]) == "Fifteen degrees and overcast, sir."
    assert box.ran == ["search_web"]
    assert it.voice.spoken == ["Fifteen degrees and overcast, sir."]


def test_it_is_told_what_went_wrong():
    box = toolbox(press_keys="pressed")
    it = brain(said("press_keys(keys='playpause')"), said("Paused, sir."), box=box)
    it.turn(["pause the music"])
    nudge = [m for m in it.messages if m["role"] == "user"][-1]
    assert "press_keys written out as words" in nudge["content"]
    assert "It did not run" in nudge["content"]


@pytest.mark.parametrize(
    "text",
    [
        'search_web(query="x")',
        "  press_keys(keys='mute')  ",
        "`read_page(url='example.com')`",
        "look_at_screen",
    ],
)
def test_what_counts_as_a_call_typed_out(text):
    from jarvis.brain import written_as_words

    assert written_as_words(text, ["search_web", "press_keys", "read_page", "look_at_screen"])


@pytest.mark.parametrize(
    "text",
    [
        "I will use search_web to find that.",
        "Nothing on screen, sir.",
        "read_page did not work.",
        "",
    ],
)
def test_a_sentence_that_merely_mentions_a_tool_is_still_an_answer(text):
    from jarvis.brain import written_as_words

    assert written_as_words(text, ["search_web", "press_keys", "read_page"]) == ""


# ------------------------------------------------------------------- steering


def test_speaking_over_it_stops_the_reply_and_redirects():
    """What owning the loop and streaming buy together: the half written answer
    is abandoned where it stands, and the turn carries on knowing more."""
    box = toolbox(look_at_screen="two windows", focus_window="Chrome is up")
    it = brain(
        calling("focus_window", window="Chrome"),
        said("Chrome is up, sir."),
        box=box,
    )
    it.model.interrupt = [["actually, open Chrome instead"]]
    assert it.turn(["open spotify"]) == "Chrome is up, sir."

    steer = [m for m in it.messages if m["role"] == "user"][-1]
    assert "actually, open Chrome instead" in steer["content"]
    assert "spoke over you" in steer["content"]
    assert "discarded and never reached them" in steer["content"]


def test_what_was_found_before_the_interruption_is_kept():
    """ "No, the other one" should build on the look that already happened rather
    than start the turn from nothing."""
    box = toolbox(look_at_screen="Taskbar - 25 targets")
    it = brain(calling("look_at_screen"), said("Done, sir."), box=box)
    it.model.interrupt = [["no, the other one"]]
    it.turn(["open the first one"])
    assert any(m["role"] == "tool" for m in it.messages), "the scan is still there"


def test_the_last_call_of_a_turn_is_not_interruptible():
    """It is one sentence away from being spoken, and abandoning it loses the
    answer to work already done."""
    box = toolbox(look_at_screen="ok")
    it = brain(calling("look_at_screen"), said(""), said("All done, sir."), box=box)
    it.turn(["look at the screen"])
    assert it.model.watched == [True, True, False], "watched only while tools were offered"


def test_the_reasoning_goes_in_the_log(caplog):
    """It is the only record of why it did what it did, and a voice session has
    nowhere else to put it."""
    import logging

    thoughtful = Reply(
        text="Half past two, sir.",
        thinking="They asked the time.\nI should check rather than guess.",
        message={"role": "assistant", "content": "Half past two, sir."},
    )
    it = brain(thoughtful)
    with caplog.at_level(logging.INFO, logger="jarvis.brain"):
        it.turn(["what time is it"])
    assert "thought: They asked the time. I should check rather than guess." in caplog.text


def test_the_templates_own_markup_leaking_into_the_answer_is_caught():
    """Seen for real, and read out word for word: `<tool_call> <function=
    focus_window> <parameter=target> 11 </parameter>`. They answered with "11"."""
    from jarvis.brain import written_as_words

    leaked = "<tool_call> <function=focus_window> <parameter=target> 11 </parameter> </tool_call>"
    assert written_as_words(leaked, ["focus_window"]) == "focus_window"
    assert written_as_words("<tool_call>something unreadable</tool_call>", []) == "a tool"


def test_markup_typed_as_the_final_answer_is_not_read_out():
    """The step budget ran out after eight calls, the model carried on in the
    shape it had been writing in, and `<tool_call> <function=look_at_screen>`
    went through the speakers with the tags in it."""
    config = replace(Config(), brain=replace(Config().brain, max_steps=1))
    box = toolbox(look_at_screen="ok")
    it = brain(
        calling("look_at_screen"),
        said("<tool_call> <function=look_at_screen> </function> </tool_call>"),
        said("I could not find a way to restart it, sir."),
        box=box,
        config=config,
    )
    assert it.turn(["restart the terminal"]) == "I could not find a way to restart it, sir."
    assert "<tool_call>" not in " ".join(it.voice.spoken)

    nudge = [m for m in it.messages if m["role"] == "user"][-1]
    assert "markup, not an answer" in nudge["content"]
    assert "no tools left this turn" in nudge["content"]


def test_markup_twice_over_is_reported_rather_than_spoken():
    """One retry, not a loop. Silence after eight tool calls is worse than
    saying it could not manage."""
    config = replace(Config(), brain=replace(Config().brain, max_steps=1))
    box = toolbox(look_at_screen="ok")
    markup = said("<tool_call> <function=look_at_screen> </function> </tool_call>")
    it = brain(calling("look_at_screen"), markup, markup, box=box, config=config)
    assert it.turn(["restart the terminal"]) == NOTHING_TO_SAY


def test_running_out_of_room_mid_thought_is_asked_again_with_more():
    """2473 characters of reasoning stopped mid sentence against a cap of 600,
    with no answer written at all - reasoning and answer share the budget. It
    reached the speakers as "I could not put an answer together", with nothing
    in the log to say why."""
    config = replace(Config(), brain=replace(Config().brain, max_steps=1, max_tokens=600))
    box = toolbox(look_at_screen="ok")
    cut_off = Reply(
        text="",
        thinking="I should check whether the window has a maximise button and",
        truncated=True,
        message={"role": "assistant", "content": ""},
    )
    it = brain(
        calling("look_at_screen"),
        cut_off,
        said("I could not maximise it, sir."),
        box=box,
        config=config,
    )
    assert it.turn(["maximise teams"]) == "I could not maximise it, sir."
    assert it.model.limits[-1] == 1200, "asked again with twice the room"


def test_a_short_answer_that_hit_the_cap_is_still_an_answer():
    """Truncation only matters when it left nothing to say. A reply that was
    cut off after a full sentence is still the sentence."""
    it = brain(Reply(text="Half past two, sir.", truncated=True, message={"role": "assistant"}))
    assert it.turn(["what time is it"]) == "Half past two, sir."


# ------------------------------------------------------- how big it may get


class Meter:
    """A terminal that only remembers the numbers in the corner."""

    def __init__(self) -> None:
        self.readings: list[str] = []

    def meter(self, text: str) -> None:
        self.readings.append(text)

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def test_the_numbers_say_what_the_session_has_cost():
    """ctx is this call; in and out are every call so far. One request's usage
    says nothing about whether an afternoon of this has been expensive."""
    shown = Meter()
    it = brain(box=toolbox(look_at_screen="Taskbar - 25 targets"))
    it.ui = shown
    it.model.replies = [
        Reply(text="One.", tokens=(1000, 40), message={"role": "assistant", "content": "One."}),
        Reply(text="Two.", tokens=(1200, 60), message={"role": "assistant", "content": "Two."}),
    ]
    it.turn(["first"])
    it.turn(["second"])
    assert shown.readings == [
        "ctx 1.0k/98k - in 1.0k - out 40",
        "ctx 1.2k/98k - in 2.2k - out 100",
    ]


def test_ctx_holds_still_when_the_tools_come_off():
    """The last call of a turn drops about 1.8k of schemas, and a number that
    halves at the end of every turn reads as the conversation being thrown away
    rather than as two shapes of request. The totals still count it."""
    shown = Meter()
    it = brain(box=toolbox(look_at_screen="Taskbar - 25 targets"))
    it.ui = shown
    it.model.replies = [
        Reply(text="Open, sir.", tokens=(3000, 20), message={"role": "assistant", "content": "x"})
    ]
    it.turn(["is spotify open"])
    it.model.replies = [
        Reply(text="Still open.", tokens=(1200, 30), message={"role": "assistant", "content": "x"})
    ]
    it._ask([])
    assert [reading.split(" - ")[0] for reading in shown.readings] == ["ctx 3.0k/98k"] * 2
    assert shown.readings[-1].endswith("in 4.2k - out 50")


def test_a_conversation_that_gets_too_big_loses_its_oldest_turn():
    """history_turns counts turns and turns are not the same size. Twenty that
    each scan a crowded window twice would overflow the window and fail the
    request outright rather than degrade."""
    config = replace(
        Config(), brain=replace(Config().brain, history_turns=20, max_context_fraction=0.7)
    )
    it = brain(*[said(f"reply {n}") for n in range(6)], config=config)
    for n in range(3):
        it.turn([f"question {n}"])
    assert it._turns() == 3, "well under the turn count, so nothing is dropped"

    it._spent = 90_000  # measured prompt, against a 98304 window
    it.turn(["question 3"])
    assert it._turns() == 3, "the oldest went to make room for this one"


def test_the_ceiling_is_off_when_the_fraction_is_zero():
    config = replace(Config(), brain=replace(Config().brain, max_context_fraction=0))
    it = brain(*[said("ok") for _ in range(4)], config=config)
    it._spent = 10_000_000
    for n in range(3):
        it.turn([f"question {n}"])
    assert it._turns() == 3, "only the turn count applies"


def test_an_endpoint_that_will_not_say_how_big_it_is_leaves_the_turn_count():
    it = brain(said("ok"))
    it.model.context_limit = lambda: 0
    it._spent = 10_000_000
    it.turn(["hello"])
    assert it._turns() == 1, "nothing to measure against, so nothing dropped"


def test_an_interruption_gives_the_budget_back():
    """A live session spent eleven of twelve steps opening the wrong thing, was
    told "no, go in the taskbar", and had one step left to do it in."""
    config = replace(Config(), brain=replace(Config().brain, max_steps=2))
    box = toolbox(look_at_screen="ok", click="clicked")
    it = brain(
        calling("look_at_screen"),
        calling("click", target=1, expecting="Teams"),
        said("Teams is open, sir."),
        box=box,
        config=config,
    )
    it.model.interrupt = [["no, go in the taskbar"]]
    assert it.turn(["open teams"]) == "Teams is open, sir."
    # Two whole steps after the steer, out of a budget of two. Without the reset
    # the interruption itself would have spent one of them.
    assert box.ran == ["look_at_screen", "click"]


def test_the_lead_in_comes_back_after_an_interruption():
    """They have just asked for something else, so there is a first step again
    and something worth saying before the work."""
    box = toolbox(look_at_screen="ok")
    it = brain(
        calling("look_at_screen", text="Right you are, sir."),
        said("Done."),
        box=box,
    )
    it.model.interrupt = [["no, the taskbar"]]
    it.turn(["open teams"])
    assert it.voice.spoken == ["Right you are, sir.", "Done."]


def test_a_tool_result_goes_in_the_log_as_well_as_on_screen(caplog):
    """`start teams` returning nothing is the whole reason one session decided
    Teams was not installed, and the log did not say so."""
    import logging

    box = toolbox(run_command="The system cannot find the file teams.")
    it = brain(calling("run_command", command="start teams"), said("Not found, sir."), box=box)
    with caplog.at_level(logging.INFO, logger="jarvis.brain"):
        it.turn(["open teams"])
    assert "-> The system cannot find the file teams." in caplog.text


# ----------------------------------------------------------------- looking back


def test_what_a_turn_taught_is_written_down(tmp_path):
    """The only way a lesson outlives the conversation it was learned in
    without somebody typing it up."""
    from jarvis.memories import bullets

    it = looking_back(
        tmp_path,
        calling("look_at_screen"),
        said("Teams is open, sir."),
        said("- Minimising takes win+down twice from a maximised window."),
    )
    it.turn(["open teams"])

    written = bullets(tmp_path / "navigation" / "user-navigation.md")
    assert written == ["Minimising takes win+down twice from a maximised window."]


def test_it_happens_after_the_answer_has_gone_out(tmp_path):
    """Speech is queued and played on another thread, so a model call made now
    costs nobody anything. Made before, it would be a pause they can hear."""
    it = looking_back(
        tmp_path,
        calling("look_at_screen"),
        said("Teams is open, sir."),
        said("- Something learned."),
    )
    spoken_at = []
    it.voice.say = lambda text: spoken_at.append(len(it.model.asked))
    it.turn(["open teams"])
    assert spoken_at == [2], "spoken after two calls, and the third is the looking back"
    assert len(it.model.asked) == 3


def test_most_turns_teach_nothing(tmp_path):
    """A list that fills up with "Teams was open" is worse than an empty one."""
    from jarvis.memories import bullets

    it = looking_back(
        tmp_path,
        calling("look_at_screen"),
        said("Teams is open, sir."),
        said("Nothing worth writing down."),
    )
    it.turn(["open teams"])
    assert bullets(tmp_path / "navigation" / "user-navigation.md") == []


def test_at_most_two_lessons_from_one_turn(tmp_path):
    from jarvis.memories import bullets

    it = looking_back(
        tmp_path,
        calling("look_at_screen"),
        said("Done, sir."),
        said("- One.\n- Two.\n- Three.\n- Four."),
    )
    it.turn(["do something"])
    assert bullets(tmp_path / "navigation" / "user-navigation.md") == ["One.", "Two."]


def test_a_lesson_about_a_target_number_is_thrown_away(tmp_path):
    """Live session, written down verbatim: "the Close button is target number
    3 when focused". Every scan numbers what it finds again, so that is a click
    on something else tomorrow - worse than having learned nothing."""
    from jarvis.memories import bullets

    it = looking_back(
        tmp_path,
        calling("look_at_screen"),
        said("Closed, sir."),
        said(
            "- File Explorer closes by clicking Close, target number 3.\n"
            "- Explorer opens straight from run_command with no path."
        ),
    )
    it.turn(["close file explorer"])

    kept = bullets(tmp_path / "navigation" / "user-navigation.md")
    assert kept == ["Explorer opens straight from run_command with no path."]


def test_a_turn_that_went_perfectly_is_not_looked_back_at(tmp_path):
    """Asked after every turn that touched a tool, it felt obliged to produce
    something and wrote down what was on the taskbar and that Task Manager was
    open. A route that worked first time has nothing to teach."""
    it = looking_back(
        tmp_path,
        calling("look_at_screen"),
        said("Teams is open, sir."),
        said("- Something it would have learned."),
        box=toolbox(look_at_screen="Taskbar - 25 targets"),
    )
    it.turn(["open teams"])
    assert len(it.model.asked) == 2, "answered, and no third call to look back with"
    assert not (tmp_path / "navigation" / "user-navigation.md").exists()


def test_a_turn_that_hit_something_is(tmp_path):
    from jarvis.memories import bullets

    it = looking_back(
        tmp_path,
        calling("look_at_screen"),
        said("I could not, sir."),
        said("- The taskbar refuses a click while it is redrawing."),
    )
    it.turn(["open teams"])
    assert bullets(tmp_path / "navigation" / "user-navigation.md") == [
        "The taskbar refuses a click while it is redrawing."
    ]


def test_a_turn_that_used_no_tools_is_not_worth_looking_back_at(tmp_path):
    it = looking_back(tmp_path, said("Half past two, sir."))
    it.turn(["what time is it"])
    assert len(it.model.asked) == 1, "answered and stopped"


def test_looking_back_never_touches_the_conversation(tmp_path):
    """The question is asked over a copy and the answer is thrown away."""
    it = looking_back(
        tmp_path,
        calling("look_at_screen"),
        said("Done, sir."),
        said("- Something learned."),
    )
    it.turn(["do something"])
    assert not any("Look back at what you just did" in str(m) for m in it.messages)
    assert it.messages[-1]["content"] == "Done, sir."


def test_a_failure_while_looking_back_does_not_lose_the_answer(tmp_path):
    it = looking_back(tmp_path, calling("look_at_screen"), said("Done, sir."))
    it.model.replies = [calling("look_at_screen"), said("Done, sir.")]

    def explode(messages, tools=None, limit=None, watch=None, think=None, stop=None):
        if len(it.model.asked) >= 2:
            raise RuntimeError("the endpoint fell over")
        return FakeModel.reply(it.model, messages, tools, limit, watch)

    it.model.reply = explode
    assert it.turn(["do something"]) == "Done, sir."


def test_it_can_be_switched_off(tmp_path):
    it = looking_back(tmp_path, calling("look_at_screen"), said("Done, sir."))
    it.settings = replace(it.settings, consolidate=False)
    it.turn(["do something"])
    assert len(it.model.asked) == 2, "answered, and did not look back"
