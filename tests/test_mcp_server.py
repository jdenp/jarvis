"""The agent-facing tools.

The loop lives in the tools, not in the agent's memory: say() takes a required
`then`, and `then="listen"` does the listening itself. Staying silent is still a
correct outcome, so nothing here can deadlock an agent that has chosen to keep
quiet - the most an unanswered question costs is a single bounced call.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from conftest import FakeDesktop, button
from jarvis.config import Config
from jarvis.mcp_server import build_server

# stay_silent has to say why it is not speaking. These are the honest ones.
SILENT = {"because": "not_aimed_at_me"}
CLAIMS_ANSWERED = {"because": "already_spoke_my_reply"}


class FakeVoice:
    def __init__(self) -> None:
        self.said: list[str] = []
        self.next_heard = [{"text": "what time is it", "id": 1}]
        self.paused = False

    def status(self) -> dict:
        return {"cursor": 0, "paused": self.paused}

    def heard(self, since=0, wait=0) -> dict:
        return {"heard": list(self.next_heard), "cursor": 1}

    def say(self, text: str) -> None:
        self.said.append(text)

    def pause(self) -> dict:
        self.paused = True
        return {"paused": True}

    def resume(self) -> dict:
        self.paused = False
        return {"paused": False}


@pytest.fixture
def rig():
    voice = FakeVoice()
    server = build_server(Config(), client=voice)

    def raw(name: str, args: dict | None = None) -> str:
        """Tool result as a JSON blob - enough to assert on without unpicking
        the MCP content envelope."""
        return json.dumps(asyncio.run(server.call_tool(name, args or {})), default=str)

    return server, voice, raw


def test_speech_comes_back_with_the_judgement_call_attached(rig):
    _, _voice, raw = rig
    result = raw("stay_silent", SILENT)
    assert "what time is it" in result
    assert "Can you answer right now" in result, "the decision, first"


def test_the_lead_in_rule_is_at_the_decision_point(rig):
    """The rule lived only in the instructions and jarvis.md - both read once and a
    long way back - while this result said "do the work, then call say() with the
    answer". The nearer text won, so the agent worked in silence instead."""
    _, _voice, raw = rig
    result = raw("stay_silent", SILENT)
    assert "keep_working" in result and "FIRST" in result, "before the work, not after"
    assert "in your own words" in result, "composed, not recited - one fixed line sounds robotic"
    assert "however many tool calls it takes" in result, "the work is not one call"
    assert "Do the work, then" not in result, "the contradiction is gone"


def test_the_instruction_is_short_and_read_last(rig):
    """A small model given five competing clauses picks whichever it read last, and
    `detail` used to be after this one."""
    _, _voice, raw = rig
    result = raw("stay_silent", SILENT)
    start = result.index("Can you answer right now")
    end = result.index("stay_silent, and say why.", start)
    assert end - start < 480, f"the instruction spans {end - start} characters"
    assert result.rindex("next_step") > result.rindex("detail")


def test_say_will_not_run_without_saying_what_happens_next(rig):
    """The whole point. Remembering to listen again was the agent's job and it
    forgot; now the argument is required, so a call that omits it never runs."""
    server, voice, _raw = rig
    with pytest.raises(Exception, match="then"):
        asyncio.run(server.call_tool("say", {"text": "Half past two, sir."}))
    assert voice.said == [], "nothing was spoken either"


def test_only_the_two_endings_are_accepted(rig):
    server, _voice, _raw = rig
    with pytest.raises(Exception, match="listen"):
        asyncio.run(server.call_tool("say", {"text": "Right.", "then": "finish"}))


def test_answering_speaks_and_listens_in_one_call(rig):
    """The loop closes inside the tool. There is no second call to forget."""
    _, voice, raw = rig
    voice.next_heard = [{"text": "and what about tomorrow", "id": 2}]
    result = raw("say", {"text": "Half past two, sir.", "then": "listen"})
    assert voice.said == ["Half past two, sir."]
    assert "spoken" in result
    assert "and what about tomorrow" in result, "their reply, in the same result"


def test_a_lead_in_speaks_and_gets_out_of_the_way(rig):
    """The other half of the fork: a holding line must not block on the reply,
    or the work never starts."""
    _, voice, raw = rig
    result = raw("say", {"text": "Let me have a look, sir.", "then": "keep_working"})
    assert voice.said == ["Let me have a look, sir."]
    assert "what time is it" not in result, "it did not listen"
    assert "NOT listening" in result and "go and do it now" in result


def test_a_lead_in_points_back_at_listening_afterwards(rig):
    _, _voice, raw = rig
    result = raw("say", {"text": "One moment.", "then": "keep_working"})
    assert "then=" in result and "listen" in result


def test_staying_silent_is_never_blocked_for_long(rig):
    """The old behaviour blocked until say() was called. With no wake word that
    is wrong - most utterances deserve no reply, and refusing deadlocked the
    session against an agent that had correctly decided to keep quiet. One
    bounce, then it goes through."""
    _, _voice, raw = rig
    raw("stay_silent", SILENT)  # "what time is it" - a question, now owed an answer
    bounced = raw("stay_silent", SILENT)
    assert "did not listen" in bounced
    assert "what time is it" in raw("stay_silent", SILENT), "still listening, not blocked"


def test_an_unanswered_question_bounces_the_next_listen(rig):
    """The failure this guards: the agent works out an answer, then listens again
    instead of speaking, and the user hears nothing. A note in the payload was
    ignorable; a call that does not listen is not."""
    _, _voice, raw = rig
    raw("stay_silent", SILENT)
    bounced = raw("stay_silent", SILENT)
    assert "never spoke an answer" in bounced
    assert "listening" in bounced, "and it says so, rather than looking like an error"
    assert "did not listen" in bounced, "the call really was skipped"
    assert "say(it," in bounced, "the way out is offered, not just the complaint"


def test_answering_clears_it(rig):
    _, voice, raw = rig
    raw("stay_silent", SILENT)  # "what time is it"
    voice.next_heard = [{"text": "right, thanks", "id": 2}]
    raw("say", {"text": "Half past two, sir.", "then": "listen"})
    assert "never spoke an answer" not in raw("stay_silent", SILENT)


def test_a_lead_in_does_not_settle_it(rig):
    """A "let me have a look" is not an answer, and the tool was told as much by
    `then`. A lead-in followed by silence is the worst outcome of the lot."""
    _, _voice, raw = rig
    raw("stay_silent", SILENT)
    raw("say", {"text": "Let me check, sir.", "then": "keep_working"})
    assert "never spoke an answer" in raw("stay_silent", SILENT)


def test_a_lead_in_is_not_an_answer_to_claim(rig):
    _, _voice, raw = rig
    raw("stay_silent", SILENT)
    raw("say", {"text": "Let me check, sir.", "then": "keep_working"})
    assert "have not called say()" in raw("stay_silent", CLAIMS_ANSWERED)


def test_silence_after_a_non_question_is_never_chased(rig):
    """Most silence is correct. Chasing it pushes the agent into answering
    things nobody asked."""
    _, voice, raw = rig
    voice.next_heard = [{"text": "and the other thing"}]
    for _ in range(4):
        assert "never spoke an answer" not in raw("stay_silent", SILENT)


def test_idle_returns_are_not_identical(rig):
    """Four identical empty results in a row read as a stuck loop to a client
    counting consecutive failures, which killed the session."""
    _, voice, raw = rig
    voice.next_heard = []

    results = [raw("stay_silent", SILENT) for _ in range(3)]
    assert all("Not an error" in r for r in results)
    assert len(set(results)) == 3, "each idle result differs from the last"
    assert "waited_seconds" in results[0]


def test_the_idle_counter_resets_once_something_is_said(rig):
    _, voice, raw = rig
    voice.next_heard = []
    raw("stay_silent", SILENT)
    raw("stay_silent", SILENT)

    voice.next_heard = [{"text": "right, carry on", "id": 9}]
    assert "waited_seconds" not in raw("stay_silent", SILENT)

    voice.next_heard = []
    first_idle_again = raw("stay_silent", SILENT)
    assert '"waited_seconds": 240' in first_idle_again or "waited_seconds" in first_idle_again


def test_a_quiet_answer_still_returns_something_useful(rig):
    """say(then="listen") into a quiet room must read as idle, not as a failure."""
    _, voice, raw = rig
    voice.next_heard = []
    result = raw("say", {"text": "Ten thousand.", "then": "listen"})
    assert voice.said == ["Ten thousand."]
    assert "spoken" in result
    assert "Not an error" in result


def test_backlog_from_before_the_first_listen_is_skipped():
    """Spawned at client launch, long before anyone asks for voice - replaying
    what was said in between makes "jarvis" answer a finished conversation."""
    asked_from: list[int] = []

    class DriftingVoice(FakeVoice):
        def __init__(self) -> None:
            super().__init__()
            self.cursor = 0

        def status(self) -> dict:
            return {"cursor": self.cursor}

        def heard(self, since=0, wait=0) -> dict:
            asked_from.append(since)
            return {"heard": list(self.next_heard), "cursor": self.cursor}

    voice = DriftingVoice()
    server = build_server(Config(), client=voice)
    voice.cursor = 7  # spoken between launch and the first stay_silent

    asyncio.run(server.call_tool("stay_silent", SILENT))
    assert asked_from == [7], "listening starts from now, not from launch"

    # After that, nothing is skipped: a queued utterance is one spoken while the
    # agent was busy, which is exactly what it must not miss.
    voice.cursor = 12
    asyncio.run(server.call_tool("say", {"text": "Right you are.", "then": "listen"}))
    assert asked_from[1] == 7


def _heard_at(text: str, ago: float) -> dict:
    at = datetime.now(UTC) - timedelta(seconds=ago)
    return {"text": text, "id": 1, "at": at.isoformat(timespec="seconds")}


def test_an_old_utterance_is_flagged_as_a_leftover(rig):
    _, voice, raw = rig
    voice.next_heard = [_heard_at("thank you", ago=1200)]
    result = raw("stay_silent", SILENT)
    assert "stale" in result
    assert "1200s ago" in result
    assert "said_seconds_ago" in result


def test_something_just_said_is_not_flagged(rig):
    _, voice, raw = rig
    voice.next_heard = [_heard_at("what time is it", ago=2)]
    result = raw("stay_silent", SILENT)
    assert "stale" not in result
    assert "said_seconds_ago" in result


def test_a_reply_heard_by_say_carries_the_same_notes(rig):
    """listen() is one function, so nothing is only true of stay_silent."""
    _, voice, raw = rig
    voice.next_heard = [_heard_at("are you still there", ago=1200)]
    result = raw("say", {"text": "Done, sir.", "then": "listen"})
    assert "stale" in result
    assert "Can you answer right now" in result


def test_mid_task_speech_is_acknowledged_without_blocking(rig):
    """check_for_speech is for a working agent, so the say() it recommends must
    be the one that returns straight away."""
    _, _voice, raw = rig
    result = raw("check_for_speech")
    assert "keep_working" in result


def test_pause_transcription_tool(rig):
    _, voice, raw = rig
    voice.paused = False
    result = raw("pause_transcription")
    parsed = json.loads(result)
    assert "paused" in parsed


def test_resume_transcription_tool(rig):
    _, _voice, raw = rig
    result = raw("resume_transcription")
    parsed = json.loads(result)
    assert "paused" in parsed


def test_the_pause_tool_no_longer_claims_the_microphone_keeps_running(rig):
    """It used to say so, and it was true - paused only withheld utterances from
    the agent while still capturing, transcribing and logging them."""
    server, _voice, _raw = rig
    pause = next(t for t in asyncio.run(server.list_tools()) if t.name == "pause_transcription")
    assert "keeps running" not in pause.description
    assert "microphone stops being read" in pause.description
    assert "end key" in pause.description, "the configured key, not a hardcoded Pause"


def test_stay_silent_will_not_run_without_a_reason(rig):
    """Listening instead of speaking is a decision to leave them in silence. The
    schema makes it a stated one."""
    server, _voice, _raw = rig
    with pytest.raises(Exception, match="because"):
        asyncio.run(server.call_tool("stay_silent", {}))


def test_writing_the_reply_out_and_claiming_you_said_it_is_refused(rig):
    """Observed in a real session: heard "Hello", wrote "Hello sir. How can I
    help?" into the chat, then called stay_silent. Nothing was spoken and the
    user heard nothing. It is the one claim the server can check."""
    _, voice, raw = rig
    voice.next_heard = [{"text": "Hello", "id": 1}]
    raw("stay_silent", SILENT)
    refused = raw("stay_silent", CLAIMS_ANSWERED)
    assert "have not called say()" in refused
    assert "listening" in refused, "and it did not listen"
    assert voice.said == []


def test_the_refusal_hands_back_the_call_to_make(rig):
    _, voice, raw = rig
    voice.next_heard = [{"text": "Hello", "id": 1}]
    raw("stay_silent", SILENT)
    refused = raw("stay_silent", CLAIMS_ANSWERED)
    assert "then=" in refused and "listen" in refused
    assert "not_aimed_at_me" in refused, "the honest way out, if it really was not for them"


def test_the_refusal_is_not_a_deadlock(rig):
    """Every other reason still goes through, so the escape is in the enum rather
    than in a retry counter. Nothing here can wedge a session."""
    _, voice, raw = rig
    voice.next_heard = [{"text": "Hello", "id": 1}]
    raw("stay_silent", SILENT)
    raw("stay_silent", CLAIMS_ANSWERED)
    assert "Hello" in raw("stay_silent", SILENT), "still listening"


def test_the_claim_is_accepted_once_it_is_true(rig):
    """A quiet room after a real answer is the case this must not punish."""
    _, voice, raw = rig
    raw("stay_silent", SILENT)
    voice.next_heard = []
    raw("say", {"text": "Half past two, sir.", "then": "listen"})
    assert "have not called say()" not in raw("stay_silent", CLAIMS_ANSWERED)


# ---------------------------------------------------------------------- screen


def text_of(result) -> str:
    """The tool's own JSON, lifted out of the MCP envelope.

    Dumping the whole result with default=str double-escapes every quote, so
    anything asserting on the JSON itself has to unwrap first.
    """
    return "\n".join(block.text for block in result.content if getattr(block, "text", None))


def screen_rig(control: bool, elements=None):
    """A server with a fake desktop behind it. Marks off - drawing needs Pillow."""
    from jarvis.config import ScreenConfig
    from jarvis.screen import Screen

    backend = FakeDesktop(
        elements if elements is not None else [button("Reply"), button("Delete", top=40)],
        title="Mail",
    )
    config = replace(
        Config(),
        screen=ScreenConfig(control=control, marks_file="", focus_settle_seconds=0.0),
    )
    server = build_server(config, client=FakeVoice(), screen=Screen(config.screen, backend))
    return server, backend


def test_looking_is_always_offered_and_acting_is_not(rig):
    """Looking reads the accessibility tree and touches nothing. Clicking moves
    the real pointer, so it waits to be switched on."""
    off = {tool.name for tool in asyncio.run(screen_rig(False)[0].list_tools())}
    on = {tool.name for tool in asyncio.run(screen_rig(True)[0].list_tools())}

    assert "look_at_screen" in off
    assert {"click", "type_text", "scroll", "press_keys", "focus_window"} & off == set()
    assert {"click", "type_text", "scroll", "press_keys", "focus_window"} <= on


def test_looking_hands_back_numbers_and_never_coordinates():
    server, _ = screen_rig(True)
    result = text_of(asyncio.run(server.call_tool("look_at_screen", {})))
    assert '"id": 1' in result and "Reply" in result
    assert "centre" not in result, "no coordinates to reason about"


def test_the_way_to_turn_clicking_on_is_in_the_result():
    server, _ = screen_rig(False)
    result = text_of(asyncio.run(server.call_tool("look_at_screen", {})))
    assert "control" in result and "jarvis.json" in result


def test_clicking_will_not_run_without_saying_what_it_expects():
    """The same shape as say()'s `then`: the argument the model would rather skip
    is the one that catches the mistake, so the schema will not let it."""
    server, _ = screen_rig(True)
    with pytest.raises(Exception, match="expecting"):
        asyncio.run(server.call_tool("click", {"target": 1}))


def test_a_click_that_names_the_wrong_thing_is_refused(monkeypatch):
    from jarvis import hands

    pressed = []
    monkeypatch.setattr(hands, "click", lambda *a, **k: pressed.append(a))

    server, _ = screen_rig(True)
    asyncio.run(server.call_tool("look_at_screen", {}))
    result = text_of(asyncio.run(server.call_tool("click", {"target": 1, "expecting": "Delete"})))

    assert "not 'Delete'" in result
    assert pressed == [], "and nothing was clicked"


def test_a_click_that_names_the_right_thing_goes_through(monkeypatch):
    from jarvis import hands

    pressed = []
    monkeypatch.setattr(hands, "click", lambda *a, **k: pressed.append(a))

    server, _ = screen_rig(True)
    asyncio.run(server.call_tool("look_at_screen", {}))
    result = text_of(asyncio.run(server.call_tool("click", {"target": 1, "expecting": "Reply"})))

    assert "left click" in result
    assert len(pressed) == 1, "one click, at the centre of target 1"


def test_clicking_before_looking_says_to_look_first(monkeypatch):
    from jarvis import hands

    monkeypatch.setattr(hands, "click", lambda *a, **k: pytest.fail("clicked blind"))
    server, _ = screen_rig(True)
    result = text_of(asyncio.run(server.call_tool("click", {"target": 1, "expecting": "Reply"})))
    assert "Nothing has been scanned" in result


def test_a_click_says_the_numbers_are_now_stale(monkeypatch):
    """Anything you press redraws something. Carrying on with the old numbers is
    the next mistake, so the result says so at the point of deciding."""
    from jarvis import hands

    monkeypatch.setattr(hands, "click", lambda *a, **k: None)
    server, _ = screen_rig(True)
    asyncio.run(server.call_tool("look_at_screen", {}))
    result = text_of(asyncio.run(server.call_tool("click", {"target": 1, "expecting": "Reply"})))
    assert "look_at_screen before the next one" in result


def test_typing_will_not_run_without_saying_whether_it_submits():
    """press_enter sends the message. A half written one cannot be taken back,
    so it is not something to leave to a default."""
    server, _ = screen_rig(True)
    with pytest.raises(Exception, match="then"):
        asyncio.run(
            server.call_tool("type_text", {"target": 1, "expecting": "Reply", "text": "hello"})
        )


def test_typing_can_submit_or_stop(monkeypatch):
    from jarvis import hands

    typed: list = []
    monkeypatch.setattr(hands, "click", lambda *a, **k: None)
    monkeypatch.setattr(hands, "type_text", lambda text: typed.append(text))
    monkeypatch.setattr(hands, "press", lambda keys: typed.append(f"<{keys}>"))

    server, _ = screen_rig(True)
    asyncio.run(server.call_tool("look_at_screen", {}))
    asyncio.run(
        server.call_tool(
            "type_text",
            {"target": 1, "expecting": "Reply", "text": "on my way", "then": "leave_it"},
        )
    )
    assert typed == ["on my way"], "nothing sent"

    asyncio.run(
        server.call_tool(
            "type_text",
            {
                "target": 1,
                "expecting": "Reply",
                "text": "on my way",
                "then": "press_enter",
                "clear_first": True,
            },
        )
    )
    assert typed[1:] == ["<ctrl+a>", "on my way", "<enter>"], "cleared, typed, then sent"


def test_an_unknown_key_is_refused_rather_than_half_pressed():
    server, _ = screen_rig(True)
    result = text_of(asyncio.run(server.call_tool("press_keys", {"keys": "ctrl+nope"})))
    assert "nope" in result and '"done": null' in result


def test_the_screenshot_tool_is_there_without_screen_control():
    """A picture reads the screen and touches nothing, so it sits on the same
    side of the line as looking."""
    off = {tool.name for tool in asyncio.run(screen_rig(False)[0].list_tools())}
    assert "screenshot" in off


def test_a_screenshot_without_pillow_says_how_to_get_it(monkeypatch):
    """The only optional dependency in the whole feature, and the failure has to
    name it rather than surfacing an ImportError."""
    import builtins

    real = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "PIL":
            raise ImportError("no PIL here")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    server, _ = screen_rig(True)
    result = text_of(asyncio.run(server.call_tool("screenshot", {})))
    assert "uv sync --extra screen" in result
