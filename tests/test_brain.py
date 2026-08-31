"""JARVIS's own agent loop.

The point of the whole thing is in one line of `turn()`: the model's reply is
what gets spoken. There is no say tool to forget, so most of these tests are
about the two ways a turn could still end in silence - the step budget running
out, and a model that returns nothing - and how each one is closed.
"""

from __future__ import annotations

import time
from dataclasses import replace

import pytest

from jarvis.brain import (
    ALREADY_SEEN,
    HERE_IT_IS,
    KEEP_WHOLE,
    NO_MODEL,
    NOTHING_TO_SAY,
    SQUASHED,
    Brain,
    Call,
    Model,
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

    def __init__(self, *replies: Reply, limit: int = 98304) -> None:
        self.replies = list(replies)
        self.limit = limit
        self.asked: list[tuple[int, bool]] = []  # (messages, tools offered)
        self.preloads = 0
        self.watched: list[bool] = []
        self.stopped: list = []
        self.limits: list[int | None] = []
        self.interrupt: list[list[str]] = []
        self.timeouts: list[float | None] = []
        self.seen: list[list[dict]] = []
        self.raise_next: Exception | None = None

    def context_limit(self) -> int:
        return self.limit

    def reply(
        self, messages, tools=None, limit=None, watch=None, think=None, stop=None, timeout=None
    ) -> Reply:
        if self.raise_next is not None:
            raise self.raise_next
        if limit == 1:
            self.preloads += 1
            return said("")
        self.watched.append(watch is not None)
        self.stopped.append(stop)
        self.limits.append(limit)
        self.timeouts.append(timeout)
        self.seen.append(messages)
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


def brain(*replies, voice=None, config=None, box=None, limit=98304) -> Brain:
    """A brain for testing the answering loop.

    Looking back is off. It is a separate concern with its own tests below, and
    left on it would make an extra model call after every turn here - and write
    into the repository's own context directory to do it.
    """
    config = config or Config()
    return Brain(
        replace(config, brain=replace(config.brain, consolidate=False)),
        voice or FakeVoice(),
        model=FakeModel(*replies, limit=limit),
        toolbox=box or toolbox(),
    )


def looking_back(tmp_path, *replies, box=None) -> Brain:
    """One that does look back, writing somewhere harmless."""
    config = replace(
        Config(),
        brain=replace(Config().brain, memories_file=str(tmp_path / "memories.md")),
    )
    return Brain(
        config,
        FakeVoice(),
        model=FakeModel(*replies),
        toolbox=box or refusing(),
    )


def quiet(it) -> bool:
    """Wind the clock so the conversation counts as having gone quiet.

    Which is when it looks back now, rather than on the end of every turn.
    """
    if it._quiet_at is not None:
        it._quiet_at -= it.settings.settle_seconds + 1
    return it.settle()


def refusing() -> Toolbox:
    """A toolbox whose one tool refuses, the way a stale target number does."""

    def refuse(**arguments):
        raise ValueError("Target 3 was 'Close', but something else is there now")

    return Toolbox([Tool(name="look_at_screen", description="look", run=refuse)])


# ------------------------------------------------------------------- seeing


def test_a_picture_reaches_the_model_as_the_message_after_the_tool():
    """No endpoint takes an image on a `tool` message, which is the whole reason
    looking is two calls - the first says one is coming, the second is where it
    can be described."""
    it = brain(calling("look_at_image"), said("Spotify is in front, sir."))
    it.toolbox = toolbox(look_at_image="shot.png is in front of you now")
    it.toolbox.images.append("data:image/png;base64,AAAA")

    assert it.turn(["what is on screen"]) == "Spotify is in front, sir."
    picture = it.messages[-2]
    assert picture["role"] == "user"
    assert picture["content"][0]["text"] == HERE_IT_IS
    assert picture["content"][1]["image_url"] == {"url": "data:image/png;base64,AAAA"}


def test_the_queue_is_emptied_so_it_is_not_sent_twice():
    it = brain(calling("look_at_image"), said("Done, sir."))
    it.toolbox = toolbox(look_at_image="looking")
    it.toolbox.images.append("data:image/png;base64,AAAA")
    it.turn(["what is on screen"])
    assert it.toolbox.images == []


def test_only_the_latest_picture_stays_attached():
    """Each one is a couple of thousand tokens and they live in the history, so
    a turn that looks twice would carry both for the rest of the conversation."""
    it = brain()
    it.messages.append({"role": "user", "content": [{"type": "text", "text": "old"}]})
    it.toolbox.images.append("data:image/png;base64,BBBB")
    it._show_the_pictures()

    assert it.messages[-2]["content"] == ALREADY_SEEN
    assert it.messages[-1]["content"][1]["image_url"]["url"].endswith("BBBB")


def test_nothing_is_appended_when_nothing_was_looked_at():
    it = brain(said("Half past two, sir."))
    before = len(it.messages)
    it._show_the_pictures()
    assert len(it.messages) == before


def test_the_endpoint_is_asked_whether_it_can_see():
    """Worth knowing at startup rather than the first time a picture goes out to
    something with no eyes."""
    import httpx

    def props(request):
        return httpx.Response(200, json={"modalities": {"vision": True, "audio": False}})

    model = Model(Config().brain, client=httpx.Client(transport=httpx.MockTransport(props)))
    assert model.can_see() is True


def refusing_until(answers_on: int):
    """A client that refuses the connection until the nth try."""
    import httpx

    tries = []

    def endpoint(request):
        tries.append(request)
        if len(tries) < answers_on:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json={"data": []})

    return tries, httpx.Client(transport=httpx.MockTransport(endpoint))


def test_a_model_that_is_not_up_yet_is_waited_for():
    """Both this and the model server start at login and nothing sequences
    them, so the ordinary case is JARVIS winning the race and a 35B model
    taking a minute or two to load off disk."""
    tries, client = refusing_until(3)
    model = Model(Config().brain, client=client)
    assert model.wait_until_available(seconds=5, every=0.01) == ""
    assert len(tries) == 3


def test_a_wait_that_runs_out_says_why_rather_than_going_on_forever():
    """A URL with a typo in it is not a model that is still loading, and the
    difference is only visible from how long it has been."""
    _, client = refusing_until(10_000)
    model = Model(Config().brain, client=client)
    assert "refused" in model.wait_until_available(seconds=0.05, every=0.01)


def test_the_waiting_can_be_switched_off():
    """Which is what `jarvis chat` gets: somebody sitting at a keyboard is
    better told at once than left in front of a prompt that never returns."""
    tries, client = refusing_until(10_000)
    model = Model(Config().brain, client=client)
    assert model.wait_until_available(seconds=0, every=0.01)
    assert len(tries) == 1, "asked once, and did not come back"


def test_a_model_that_is_already_up_is_not_waited_for():
    tries, client = refusing_until(1)
    model = Model(Config().brain, client=client)

    started = time.monotonic()
    assert model.wait_until_available(seconds=600) == ""
    assert time.monotonic() - started < 1, "no sleep before the first try"
    assert len(tries) == 1


def test_a_reason_is_the_one_line_that_says_what_happened():
    """httpx puts an MDN link on the second line of every status error, and
    that is not news to anybody reading a startup log."""
    import httpx

    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
    why = Model(Config().brain, client=client).available()
    assert "503" in why
    assert "\n" not in why and "mozilla" not in why


def sent(config, **kwargs) -> dict:
    """The body of one request, with a canned reply behind it."""
    import httpx

    seen: dict = {}

    def capture(request):
        seen.update(__import__("json").loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "yes"}}]}
        )

    model = Model(config, client=httpx.Client(transport=httpx.MockTransport(capture)))
    model.reply([{"role": "user", "content": "hello"}], **kwargs)
    return seen


def test_the_template_is_told_to_keep_earlier_reasoning():
    """Sent on every call and never conditionally. It is what renders an
    earlier turn's thinking back into the prompt, and it overrides
    llama-server's own --no-reasoning-preserve. Sending it only sometimes would
    rewrite the whole prefix and throw the server's cache of it away."""
    config = replace(Config().brain, stream=False)
    for think in (None, True, False):
        body = sent(config, think=think)
        assert body["chat_template_kwargs"]["preserve_thinking"] is True


def test_reasoning_is_asked_for_or_not_per_call():
    config = replace(Config().brain, stream=False, thinking=True)
    assert sent(config)["chat_template_kwargs"]["enable_thinking"] is True
    assert sent(config, think=False)["chat_template_kwargs"]["enable_thinking"] is False
    off = replace(config, thinking=False)
    assert sent(off)["chat_template_kwargs"]["enable_thinking"] is False


def test_an_endpoint_that_will_not_say_is_not_guessed_at():
    import httpx

    model = Model(
        Config().brain,
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={}))),
    )
    assert model.can_see() is None


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


def test_a_kept_block_comes_out_as_a_paragraph():
    """Cut out where the markers stood, it came back welded to the end of the
    sentence above it."""
    from jarvis.brain import with_ears

    body = "before\n<!-- ears -->\nyou can close your ears\n<!-- /ears -->\nafter"
    assert with_ears(body, True) == "before\n\nyou can close your ears\n\nafter"
    assert with_ears(body, False) == "before\n\nafter"


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
            self.paused = False

        def say(self, text):
            pass

    service = FakeService()
    voice = ServiceVoice(service)
    assert voice.waiting() == "listening"

    # The desk microphone, which is what the key shuts. Pausing the transcript
    # is not it any more, and reading that was a line saying it could hear.
    service.paused = True
    assert voice.waiting().startswith("desk mic off")
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


def test_reasoning_is_kept_on_the_message():
    """Measured against this machine's own server: a thought on the last
    assistant message renders into the prompt already, so the tool loop stops
    re-deriving at step seven what it worked out at step three."""
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
    assert reply.message == {
        "role": "assistant",
        "content": "Half past two.",
        "reasoning_content": "The user asked for the time. I should...",
    }


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
    assert "no tools left to call" in nudge["content"]


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


def test_where_the_context_stood_is_written_down_at_the_end_of_a_turn(caplog):
    """The corner of the terminal is gone the moment the window scrolls, and
    "was it already at 80k when that happened" is the first question worth
    asking about a turn that went strangely."""
    import logging

    it = brain(
        Reply(
            text="Half past two, sir.",
            tokens=(3000, 20),
            message={"role": "assistant", "content": "Half past two, sir."},
        ),
        box=toolbox(look_at_screen="Taskbar - 25 targets"),
    )
    with caplog.at_level(logging.INFO, logger="jarvis.brain"):
        it.turn(["what time is it"])

    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("Context: "))
    assert line == "Context: ctx 3.0k/98k - in 3.0k - out 20", "the status line, written down"


def test_it_is_written_down_even_when_the_turn_was_cancelled(caplog):
    """A turn that was thrown away is exactly the kind worth having the number
    for afterwards."""
    import logging

    from jarvis.brain import Cancelled

    it = brain(said("Half past two, sir."))
    it.model.raise_next = Cancelled()
    with caplog.at_level(logging.INFO, logger="jarvis.brain"):
        it.turn(["what time is it"])

    assert any(r.getMessage().startswith("Context: ") for r in caplog.records)


def scanned(brain, turns: int, size: int = 4000) -> None:
    """A conversation of `turns` turns, each one a scan and an answer."""
    for n in range(turns):
        brain.messages += [
            {"role": "user", "content": f"question {n}"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": f"c{n}", "function": {"name": "look_at_screen"}}],
            },
            {"role": "tool", "tool_call_id": f"c{n}", "content": "target " * (size // 7)},
            {"role": "assistant", "content": f"answer {n}"},
        ]


def test_old_scans_are_emptied_before_any_turn_is_dropped():
    """A crowded window is three thousand tokens of numbered targets that were
    stale the moment anything was clicked. Dropping the text keeps the
    conversation; dropping the turn does not."""
    it = brain()
    scanned(it, 8)
    it._spent = 75_000  # over 0.7 of a 98304 window
    it._squash()

    assert it._turns() == 8, "nothing was deleted"
    results = [m["content"] for m in it.messages if m.get("role") == "tool"]
    assert results[0] == SQUASHED.format(name="look_at_screen"), "named, so it can be run again"
    assert "target target" in results[-1], "the latest scan is untouched"


def test_the_last_two_turns_keep_their_results_whatever_the_pressure():
    """The last scan is what "no, the one below it" refers to."""
    it = brain()
    scanned(it, 6)
    it._spent = 10_000_000
    it._squash()

    results = [m["content"] for m in it.messages if m.get("role") == "tool"]
    assert len([kept for kept in results if "target" in kept]) == 2


def test_squashing_stops_as_soon_as_it_is_under():
    """Oldest first and no further. There is no reason to throw away the sixth
    scan back when emptying the tenth was enough."""
    it = brain()
    scanned(it, 10)
    it._spent = 69_500  # a few hundred over the 68.8k ceiling, one scan's worth
    it._squash()

    emptied = [m for m in it.messages if m.get("role") == "tool" and "target" not in m["content"]]
    assert len(emptied) == 1


def test_a_conversation_under_the_ceiling_is_left_alone():
    it = brain()
    scanned(it, 8)
    it._spent = 60_000
    it._squash()
    assert all("target" in m["content"] for m in it.messages if m.get("role") == "tool")


def test_short_results_are_not_worth_squashing():
    """Thirty tokens of apology in place of forty tokens of filename is a loss
    twice over."""
    it = brain()
    scanned(it, 8, size=80)
    it._spent = 10_000_000
    it._squash()
    assert all("target" in m["content"] for m in it.messages if m.get("role") == "tool")


def test_the_call_survives_its_result():
    """A tool result whose call is gone is rejected outright by some endpoints,
    which is the whole reason this is safe and dropping messages is not."""
    it = brain()
    scanned(it, 8)
    it._spent = 10_000_000
    it._squash()

    calls = {c["id"] for m in it.messages for c in m.get("tool_calls") or []}
    answers = {m["tool_call_id"] for m in it.messages if m.get("role") == "tool"}
    assert calls == answers


def test_squashing_twice_changes_nothing_the_second_time():
    it = brain()
    scanned(it, 8)
    it._spent = 10_000_000
    it._squash()
    before = [dict(m) for m in it.messages]
    it._squash()
    assert it.messages == before


def test_it_can_be_turned_off():
    config = replace(Config(), brain=replace(Config().brain, squash_fraction=0))
    it = brain(config=config)
    scanned(it, 8)
    it._spent = 10_000_000
    it._squash()
    assert all("target" in m["content"] for m in it.messages if m.get("role") == "tool")


# ----------------------------------------------------- the droppable half


def thought_out(brain, turns: int, thinking: int = 600) -> None:
    """A conversation of `turns` turns, each one reasoned about and answered.

    No tools, so the only thing here that can be emptied is the thinking.
    """
    for n in range(turns):
        brain.messages += [
            {"role": "user", "content": f"question {n}"},
            {
                "role": "assistant",
                "content": f"answer {n}",
                "reasoning_content": f"thought {n} " * (thinking // 10),
            },
        ]


def test_a_thought_is_emptied_the_way_a_result_is():
    """Both halves of the droppable side are worth the same nothing an hour
    later: the scan was stale the moment anything was clicked, and the thought
    that chose it was about a screen that has since changed."""
    it = brain()
    thought_out(it, 8)
    it._spent = 10_000_000
    it._squash()

    left = [m for m in it.messages if m.get("reasoning_content")]
    assert len(left) == KEEP_WHOLE, "only the two most recent turns keep their thinking"
    assert [m["content"] for m in it.messages if m["role"] == "assistant"] == [
        f"answer {n}" for n in range(8)
    ], "and every answer is still there"


def test_the_oldest_goes_first_whichever_kind_it_is():
    """One pass over the conversation, oldest first. A thought from turn two
    goes before a scan from turn six, because the scan is newer."""
    it = brain()
    for n in range(6):
        it.messages += [
            {"role": "user", "content": f"question {n}"},
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "thinking about it " * 60,
                "tool_calls": [{"id": f"c{n}", "function": {"name": "look_at_screen"}}],
            },
            {"role": "tool", "tool_call_id": f"c{n}", "content": "target " * 200},
            {"role": "assistant", "content": f"answer {n}"},
        ]
    # Room for exactly the first thought and then the first result, and no
    # more: the loop checks before each one, so a token over would take a third.
    room = 1080 // 4 + (1400 - len(SQUASHED.format(name="look_at_screen"))) // 4
    it._spent = it._ceiling(it.settings.squash_fraction) + room
    it._squash()

    thoughts = [bool(m.get("reasoning_content")) for m in it.messages if "reasoning_content" in m]
    results = ["target" in m["content"] for m in it.messages if m.get("role") == "tool"]
    assert thoughts.count(False) == 0, "a thought is popped, not blanked"
    assert len([m for m in it.messages if m.get("reasoning_content")]) == 5, "the oldest went"
    assert results == [False] + [True] * 5, "and then the result under it, and no further"


def test_a_short_thought_goes_too():
    """Unlike a result, nothing stands in for it, so emptying one always wins."""
    it = brain()
    thought_out(it, 8, thinking=20)
    it._spent = 10_000_000
    it._squash()
    assert len([m for m in it.messages if m.get("reasoning_content")]) == KEEP_WHOLE


# --------------------------------------------------------------- summarising


def long_session():
    """The static conversation in tests/fixtures, as a fresh list."""
    import json
    from pathlib import Path

    body = (Path(__file__).parent / "fixtures" / "a-long-session.json").read_text(encoding="utf-8")
    return json.loads(body)


def summarising(*replies, limit=2000, **overrides):
    """A brain holding the long session, against a window small enough to bite."""
    config = replace(Config(), brain=replace(Config().brain, context_limit=limit, **overrides))
    it = brain(*replies, config=config, limit=limit)
    it.messages = long_session()
    return it


def test_the_fixture_is_a_conversation_the_ladder_has_to_survive():
    """Static and committed rather than generated, so what is being asked of
    the compaction is readable in a diff."""
    it = summarising()
    assert it._turns() == 12
    assert len([m for m in it.messages if m.get("reasoning_content")]) == 22
    assert len([m for m in it.messages if m.get("role") == "tool"]) == 10


def test_emptying_comes_first_and_is_usually_enough():
    """Nothing is summarised while there is still something cheap to take."""
    it = summarising(said("a summary nobody asked for"), limit=3000)
    it._spent = 2200  # over the 2100 squash ceiling, under everything else
    it._trim()

    assert it._turns() == 12, "no turn was rewritten"
    assert it.model.asked == [], "and the model was never called"


def test_what_cannot_be_dropped_is_summarised():
    """By the time this runs, every result and every thought has already gone
    and there is nothing cheap left to take."""
    it = summarising(said("They had you working through Spotify and Outlook."))
    it._spent = 1400  # over the ceiling with nothing droppable left
    for message in it.messages:
        message.pop("reasoning_content", None)
    for message in it.messages:
        if message.get("role") == "tool":
            message["content"] = "(look_at_screen ran here.)"
    it._summarise()

    assert it._turns() == 7, "the oldest six became one"
    assert it.messages[1]["role"] == "user", "and it goes back as something they were told"
    assert "They had you working" in it.messages[1]["content"]
    assert it.messages[-1]["content"] == "Down a few notches.", "the recent half is untouched"


def test_the_story_carries_no_target_numbers_or_parameters():
    """A number written down here points at something else by the time it is
    read, and the exact arguments were about a screen that has changed."""
    from jarvis.brain import as_story

    story = as_story(long_session()[:9])
    assert "They said: what's playing" in story
    assert "You used look_at_screen." in story
    assert "Spotify Premium" not in story, "no arguments"
    assert "23 targets" not in story, "and no scan in it at all"


def test_summarising_leaves_the_conversation_alone_if_nothing_comes_back():
    it = summarising(said("   "))
    it._spent = 1400
    for message in it.messages:
        message.pop("reasoning_content", None)
    before = [dict(m) for m in it.messages]
    it._summarise()
    assert it.messages == before


def test_summarising_can_be_turned_off():
    it = summarising(said("a summary"), summarise_fraction=0)
    it._spent = 10_000_000
    before = [dict(m) for m in it.messages]
    it._summarise()
    assert it.messages == before
    assert it.model.asked == []


def test_a_short_conversation_is_not_worth_halving():
    """Two turns cut in half is one turn and a paragraph about one turn."""
    it = summarising(said("a summary"))
    it.messages = it.messages[:5]
    it._spent = 10_000_000
    it._summarise()
    assert it.model.asked == []


def test_the_whole_ladder_in_order():
    """Empty what can go, then summarise what is left, then drop turns. Each
    rung is only reached because the one before it was not enough."""
    it = summarising(said("They had you working through Spotify and Outlook."), limit=600)
    it._spent = 1547  # what the fixture weighs, against a 600 token window
    it._trim()

    assert it._turns() < 12, "it got smaller"
    thinking = [m for m in it.messages if m.get("reasoning_content")]
    assert len(thinking) == 4, "thinking went first, bar the two turns that are still current"
    calls = {c["id"] for m in it.messages for c in m.get("tool_calls") or []}
    answers = {m["tool_call_id"] for m in it.messages if m.get("role") == "tool"}
    assert calls == answers, "and it is still a conversation the endpoint would take"


def test_a_conversation_that_gets_too_big_loses_its_oldest_turn():
    """history_turns counts turns and turns are not the same size. Twenty that
    each scan a crowded window twice would overflow the window and fail the
    request outright rather than degrade."""
    config = replace(
        Config(),
        brain=replace(
            Config().brain,
            history_turns=20,
            max_context_fraction=0.7,
            # Off, so this is the backstop on its own. Summarising is tested below.
            summarise_fraction=0,
        ),
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
    from jarvis.memories import sections

    it = looking_back(
        tmp_path,
        calling("look_at_screen"),
        said("Teams is open, sir."),
        said("## Windows\n- Minimising takes win+down twice from a maximised window."),
    )
    it.turn(["open teams"])
    quiet(it)

    assert sections(tmp_path / "memories.md") == [
        ("Windows", ["Minimising takes win+down twice from a maximised window."])
    ]


def test_what_they_said_about_themselves_is_written_down_too(tmp_path):
    """Half of what is worth keeping is something they said about themselves,
    and nobody learns that by clicking."""
    from jarvis.memories import sections

    it = looking_back(
        tmp_path,
        said("Noted, sir."),
        said("## Personal\n- They ride on Sunday mornings."),
    )
    it.turn(["i ride every sunday morning"])
    quiet(it)

    assert sections(tmp_path / "memories.md") == [("Personal", ["They ride on Sunday mornings."])]


def test_a_turn_that_used_no_tools_is_looked_back_at_as_well(tmp_path):
    """It used to want a turn that had stumbled, which is a turn that used its
    hands. Nothing about a person is learned that way."""
    it = looking_back(tmp_path, said("Half past two, sir."))
    it.turn(["what time is it"])
    quiet(it)
    assert len(it.model.asked) == 2, "answered, and then asked what it learned"


def test_nothing_it_kept_quiet_about_is_looked_back_at(tmp_path):
    """A hyphen means it was not aimed at you. Somebody talking near the desk
    has not told you anything, and there is nothing to look back over."""
    it = looking_back(tmp_path, said("-"))
    it.turn(["...he moved to Perth last year"])
    assert quiet(it) is False, "nothing was said, so there is nothing to look at"
    assert len(it.model.asked) == 1
    assert not (tmp_path / "memories.md").exists()


def test_a_turn_does_not_stop_to_learn(tmp_path):
    """It used to happen on the end of every turn, which is a second model call
    on every single answer - most of them about nothing, and all of them on the
    thread that is meant to be listening."""
    it = looking_back(
        tmp_path,
        calling("look_at_screen"),
        said("Teams is open, sir."),
        said("## Windows\n- Something learned."),
    )
    it.turn(["open teams"])
    assert len(it.model.asked) == 2, "answered, and did not stop to learn"
    assert not (tmp_path / "memories.md").exists()

    assert quiet(it), "and then the room went quiet"
    assert len(it.model.asked) == 3


def test_a_run_of_turns_is_one_look_back(tmp_path):
    """The point of waiting: three answers cost three model calls rather than
    six, and the one that does happen sees what the exchange added up to."""
    it = looking_back(
        tmp_path,
        said("One, sir."),
        said("Two, sir."),
        said("Three, sir."),
        said("## Personal\n- They ask a lot of questions."),
    )
    for asked in ("first", "second", "third"):
        it.turn([asked])
    assert len(it.model.asked) == 3

    quiet(it)
    assert len(it.model.asked) == 4, "one look back, not three"
    assert "3 exchanges" in it.model.seen[-1][-1]["content"], "and it is told how far back"


def test_a_section_that_has_grown_is_tidied_once_something_is_added(tmp_path):
    """`remember` only appends, so a lesson learned three times in three wordings
    sits there three times - and this file is prompt, paid on every call."""
    from jarvis.memories import remember, sections

    path = tmp_path / "memories.md"
    for n in range(9):
        remember(path, "Windows", f"Alt tab does thing number {n}")

    it = looking_back(
        tmp_path,
        said("Done, sir."),
        said("## Windows\n- One more thing."),
        said("## Windows\n- Alt tab does several things.\n- One more thing."),
    )
    it.turn(["do something"])
    quiet(it)

    assert dict(sections(path)) == {"Windows": ["Alt tab does several things.", "One more thing."]}


def test_a_tidy_up_that_came_back_no_shorter_is_thrown_away(tmp_path):
    """A rewrite is the one thing here that can lose a line, so it only lands when
    it is genuinely shorter than what it replaces."""
    from jarvis.memories import remember, sections

    path = tmp_path / "memories.md"
    for n in range(9):
        remember(path, "Windows", f"Alt tab does thing number {n}")
    before = dict(sections(path))["Windows"]

    it = looking_back(
        tmp_path,
        said("Done, sir."),
        said("## Windows\n- One more thing."),
        said("## Personal\n- Under a heading nobody asked about."),
    )
    it.turn(["do something"])
    quiet(it)

    after = dict(sections(path))
    assert after["Windows"] == [*before, "One more thing."]
    assert "Personal" not in after, "and the stray heading was not written either"


def test_a_look_back_that_learned_nothing_does_not_tidy_either(tmp_path):
    """It would fail the same way every idle minute otherwise, over a file that
    has not changed since the last time it failed."""
    from jarvis.memories import remember

    path = tmp_path / "memories.md"
    for n in range(9):
        remember(path, "Windows", f"Alt tab does thing number {n}")

    it = looking_back(tmp_path, said("Done, sir."), said("Nothing worth keeping."))
    it.turn(["do something"])
    quiet(it)
    assert len(it.model.asked) == 2, "answered and looked back, and stopped there"


def test_a_memory_file_too_big_to_read_is_not_read_at_all(tmp_path):
    """Said in red at startup rather than quietly losing the top of the file,
    which is where the oldest and best worn lessons are."""
    from jarvis.memories import remember

    path = tmp_path / "memories.md"
    for n in range(20):
        remember(path, "Windows", f"Alt tab does thing number {n}")

    it = looking_back(tmp_path, said("Hello, sir."))
    assert it.memories_too_big() == 0
    assert "Alt tab" in it.system_prompt()

    it.settings = replace(it.settings, max_memory_chars=100)
    assert it.memories_too_big() > 100
    assert "Alt tab" not in it.system_prompt()


def test_a_stalled_look_back_gives_up_on_its_own(tmp_path):
    """It runs on the listening thread, so a call that hangs is a JARVIS that
    hears nothing until it stops hanging. Three minutes of that has happened,
    and nothing was learned at the end of it."""
    from jarvis.brain import LOOK_BACK_SECONDS

    it = looking_back(tmp_path, said("Done, sir."), said("Nothing worth keeping."))
    it.turn(["do something"])
    quiet(it)
    assert it.model.timeouts[-1] == LOOK_BACK_SECONDS
    assert it.model.timeouts[0] is None, "and the answer itself keeps the long one"


def test_most_turns_teach_nothing(tmp_path):
    """A list that fills up with "Teams was open" is worse than an empty one,
    and it is asked after everything now - so this is the common case."""
    it = looking_back(
        tmp_path,
        calling("look_at_screen"),
        said("Teams is open, sir."),
        said("Nothing worth writing down."),
    )
    it.turn(["open teams"])
    quiet(it)
    assert not (tmp_path / "memories.md").exists()


def test_at_most_three_lines_from_one_turn(tmp_path):
    """The ceiling is what stops one talkative afternoon filling the file."""
    from jarvis.memories import bullets

    it = looking_back(
        tmp_path,
        calling("look_at_screen"),
        said("Done, sir."),
        said("## Windows\n- One.\n- Two.\n- Three.\n- Four."),
    )
    it.turn(["do something"])
    quiet(it)
    assert bullets(tmp_path / "memories.md") == ["One.", "Two.", "Three."]


def test_lines_are_filed_under_the_headings_they_came_back_with(tmp_path):
    from jarvis.memories import sections

    it = looking_back(
        tmp_path,
        calling("look_at_screen"),
        said("Done, sir."),
        said(
            "## Applications\n- Teams is an MSIX package.\n\n## Personal\n- They are left handed."
        ),
    )
    it.turn(["do something"])
    quiet(it)
    assert sections(tmp_path / "memories.md") == [
        ("Applications", ["Teams is an MSIX package."]),
        ("Personal", ["They are left handed."]),
    ]


def test_a_line_with_no_heading_is_still_kept(tmp_path):
    """A line worth keeping is worth keeping badly filed."""
    from jarvis.memories import sections

    it = looking_back(
        tmp_path,
        calling("look_at_screen"),
        said("Done, sir."),
        said("- Something with no heading over it."),
    )
    it.turn(["do something"])
    quiet(it)
    assert sections(tmp_path / "memories.md") == [("Other", ["Something with no heading over it."])]


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
            "## Windows\n"
            "- File Explorer closes by clicking Close, target number 3.\n"
            "- Explorer opens straight from run_command with no path."
        ),
    )
    it.turn(["close file explorer"])
    quiet(it)

    assert bullets(tmp_path / "memories.md") == [
        "Explorer opens straight from run_command with no path."
    ]


def test_looking_back_never_touches_the_conversation(tmp_path):
    """The question is asked over a copy and the answer is thrown away."""
    it = looking_back(
        tmp_path,
        calling("look_at_screen"),
        said("Done, sir."),
        said("## Windows\n- Something learned."),
    )
    it.turn(["do something"])
    quiet(it)
    assert not any("Look back over" in str(m) for m in it.messages)
    assert it.messages[-1]["content"] == "Done, sir."


def test_a_failure_while_looking_back_does_not_lose_the_answer(tmp_path):
    it = looking_back(tmp_path, calling("look_at_screen"), said("Done, sir."))
    it.model.replies = [calling("look_at_screen"), said("Done, sir.")]

    def explode(messages, tools=None, limit=None, watch=None, think=None, stop=None, timeout=None):
        if len(it.model.asked) >= 2:
            raise RuntimeError("the endpoint fell over")
        return FakeModel.reply(it.model, messages, tools, limit, watch)

    it.model.reply = explode
    assert it.turn(["do something"]) == "Done, sir."
    quiet(it)


def test_it_can_be_switched_off(tmp_path):
    it = looking_back(tmp_path, calling("look_at_screen"), said("Done, sir."))
    it.settings = replace(it.settings, consolidate=False)
    it.turn(["do something"])
    assert quiet(it) is False
    assert len(it.model.asked) == 2, "answered, and did not look back"
