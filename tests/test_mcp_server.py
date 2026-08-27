"""The agent-facing tools.

One tool is the whole loop. converse() speaks and then listens, so there is no
second call to forget and no other tool to pick - the failure being designed
against is a model that has just listened writing its reply as prose and ending
its turn. Keeping quiet is still a correct outcome, so nothing here can deadlock
an agent that has chosen it: the most an unanswered utterance costs is one
bounced call.
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

# Listening without speaking. Both arguments are required, so even saying
# nothing is something the caller has to state.
LISTEN = {"say": "", "then": "listen"}


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


def test_speech_comes_back_with_the_instruction_attached(rig):
    _, _voice, raw = rig
    result = raw("converse", LISTEN)
    assert "what time is it" in result
    assert "EMIT converse()" in result, "the imperative, and it is the last thing there"


def test_the_result_is_an_instruction_and_nothing_else(rig):
    """Three things were wrong with the version before this one, and all three
    were about what the model reads last. `detail` sat between the words and the
    decision - eight lines of id and timestamp for a two word greeting. The
    instruction opened with a question, which a model answers in prose because
    that is what questions are for. And it ended on the name of the tool that
    does not speak."""
    _, _voice, raw = rig
    result = raw("converse", LISTEN)

    assert "detail" not in result, "no metadata competing with the instruction"
    assert "said_seconds_ago" not in result
    assert "Can you answer right now" not in result, "it no longer opens with a question"
    marker = "EMIT converse()"
    tail = result[result.rindex(marker) + len(marker) :]
    assert "converse" not in tail, "the imperative is the last thing there"


def test_converse_will_not_run_without_something_to_say(rig):
    """`say` is required rather than defaulted, so the call cannot be made
    without confronting what is being said - even if the answer is nothing."""
    server, voice, _raw = rig
    with pytest.raises(Exception, match="say"):
        asyncio.run(server.call_tool("converse", {"then": "listen"}))
    assert voice.said == []


def test_converse_will_not_run_without_saying_what_happens_next(rig):
    """A holding line that blocks for the reply stalls the work for as long as
    the poll lasts, and an answer that does not listen ends the conversation.
    Both directions matter, so it is required."""
    server, voice, _raw = rig
    with pytest.raises(Exception, match="then"):
        asyncio.run(server.call_tool("converse", {"say": "Half past two, sir."}))
    assert voice.said == [], "nothing was spoken either"


def test_only_the_two_endings_are_accepted(rig):
    server, _voice, _raw = rig
    with pytest.raises(Exception, match="listen"):
        asyncio.run(server.call_tool("converse", {"say": "Right.", "then": "finish"}))


def test_there_is_only_one_tool_for_a_turn(rig):
    """The failure that drove this: speaking and listening were two tools, and a
    model that had just listened had to notice it needed a different one. Now
    every turn is the same call, including the first, so there is no switch to
    miss."""
    server, _voice, _raw = rig
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert "converse" in names
    assert "say" not in names and "stay_silent" not in names


def test_answering_speaks_and_listens_in_one_call(rig):
    """The loop closes inside the tool. There is no second call to forget."""
    _, voice, raw = rig
    voice.next_heard = [{"text": "and what about tomorrow", "id": 2}]
    result = raw("converse", {"say": "Half past two, sir.", "then": "listen"})
    assert voice.said == ["Half past two, sir."]
    assert "spoke" in result
    assert "and what about tomorrow" in result, "their reply, in the same result"


def test_a_lead_in_speaks_and_gets_out_of_the_way(rig):
    """The other half of the fork: a holding line must not block on the reply,
    or the work never starts."""
    _, voice, raw = rig
    result = raw("converse", {"say": "Let me have a look, sir.", "then": "keep_working"})
    assert voice.said == ["Let me have a look, sir."]
    assert "what time is it" not in result, "it did not listen"
    assert "NOT listening" in result and "go and do it now" in result


def test_a_lead_in_points_back_at_the_same_tool(rig):
    _, _voice, raw = rig
    result = raw("converse", {"say": "One moment.", "then": "keep_working"})
    assert "converse(say=" in result and "listen" in result


def test_saying_nothing_listens(rig):
    """Entering voice mode, and hearing something meant for somebody else."""
    _, voice, raw = rig
    result = raw("converse", LISTEN)
    assert voice.said == [], "nothing spoken"
    assert "what time is it" in result, "but it listened"


def test_an_empty_say_is_refused_while_a_reply_is_owed(rig):
    """The one checkable claim. Writing the reply out as text and then coming
    back here is a claim to have answered, and nothing went through the
    speakers - so it is refused and handed the call to make instead."""
    _, _voice, raw = rig
    raw("converse", LISTEN)  # "what time is it" - now owed an answer
    bounced = raw("converse", LISTEN)
    assert "did not listen" in bounced
    assert "converse(say=" in bounced, "the way out is offered, not just the complaint"
    assert "spoke" in bounced and "false" in bounced


def test_the_refusal_is_one_bounce_not_a_deadlock(rig):
    """Refusing outright wedges a session against an agent that has correctly
    decided to keep quiet. The claim clears itself, so the next call goes
    through either way."""
    _, _voice, raw = rig
    raw("converse", LISTEN)
    assert "did not listen" in raw("converse", LISTEN)
    assert "what time is it" in raw("converse", LISTEN), "still listening, not blocked"


def test_a_greeting_is_chased_as_readily_as_a_question(rig):
    """It used to chase only what parsed as a question. "Hey Jarvis" is not a
    question, and it is exactly what went unanswered in practice."""
    _, voice, raw = rig
    voice.next_heard = [{"text": "Hey Jarvis", "id": 1}]
    raw("converse", LISTEN)
    bounced = raw("converse", LISTEN)
    assert "Hey Jarvis" in bounced and "did not listen" in bounced
    assert "said" in bounced, "said, not asked - it was not a question"


def test_a_question_is_worded_as_one(rig):
    _, _voice, raw = rig
    raw("converse", LISTEN)  # "what time is it"
    assert "asked" in raw("converse", LISTEN)


def test_answering_clears_it(rig):
    _, voice, raw = rig
    raw("converse", LISTEN)  # "what time is it" - now owed an answer
    voice.next_heard = []  # answered into a quiet room, so no new debt
    raw("converse", {"say": "Half past two, sir.", "then": "listen"})
    assert "did not listen" not in raw("converse", LISTEN)


def test_anything_heard_owes_a_reply_until_one_is_spoken(rig):
    """The cost of chasing greetings as well as questions: anything heard is
    owed something, so listening again without speaking always costs one bounce.
    Cheap, it clears itself, and the message offers both ways out rather than
    insisting on an answer - answering what nobody asked is the worse failure of
    the two."""
    _, voice, raw = rig
    voice.next_heard = [{"text": "somebody else entirely", "id": 3}]
    raw("converse", LISTEN)
    bounced = raw("converse", LISTEN)
    assert "not for you, call this again" in bounced, "keeping quiet is still allowed"
    assert "somebody else entirely" in raw("converse", LISTEN), "and it goes through"


def test_a_lead_in_does_not_settle_it(rig):
    """A "let me have a look" is not an answer, and the tool was told as much by
    `then`. A lead-in followed by silence is the worst outcome of the lot."""
    _, _voice, raw = rig
    raw("converse", LISTEN)
    raw("converse", {"say": "Let me check, sir.", "then": "keep_working"})
    assert "did not listen" in raw("converse", LISTEN)


def test_idle_returns_are_not_identical(rig):
    """Four identical empty results in a row read as a stuck loop to a client
    counting consecutive failures, which killed the session."""
    _, voice, raw = rig
    voice.next_heard = []

    results = [raw("converse", LISTEN) for _ in range(3)]
    assert all("Not an error" in r for r in results)
    assert len(set(results)) == 3, "each idle result differs from the last"
    assert "waited_seconds" in results[0]


def test_the_idle_counter_resets_once_something_is_said(rig):
    _, voice, raw = rig
    voice.next_heard = []
    raw("converse", LISTEN)
    raw("converse", LISTEN)

    voice.next_heard = [{"text": "right, carry on", "id": 9}]
    assert "waited_seconds" not in raw("converse", LISTEN)

    # Speaking settles what was just heard, so the room reads as idle again.
    voice.next_heard = []
    raw("converse", {"say": "Right you are, sir.", "then": "listen"})
    assert "waited_seconds" in raw("converse", LISTEN)


def test_a_quiet_answer_still_returns_something_useful(rig):
    """Speaking into a quiet room must read as idle, not as a failure."""
    _, voice, raw = rig
    voice.next_heard = []
    result = raw("converse", {"say": "Ten thousand.", "then": "listen"})
    assert voice.said == ["Ten thousand."]
    assert "spoke" in result
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
    voice.cursor = 7  # spoken between launch and the first converse

    asyncio.run(server.call_tool("converse", LISTEN))
    assert asked_from == [7], "listening starts from now, not from launch"

    # After that, nothing is skipped: a queued utterance is one spoken while the
    # agent was busy, which is exactly what it must not miss.
    voice.cursor = 12
    asyncio.run(server.call_tool("converse", {"say": "Right you are.", "then": "listen"}))
    assert asked_from[1] == 7


def _heard_at(text: str, ago: float) -> dict:
    at = datetime.now(UTC) - timedelta(seconds=ago)
    return {"text": text, "id": 1, "at": at.isoformat(timespec="seconds")}


def test_an_old_utterance_is_flagged_as_a_leftover(rig):
    _, voice, raw = rig
    voice.next_heard = [_heard_at("thank you", ago=1200)]
    result = raw("converse", LISTEN)
    assert "stale" in result
    assert "1200s ago" in result


def test_something_just_said_is_not_flagged(rig):
    _, voice, raw = rig
    voice.next_heard = [_heard_at("what time is it", ago=2)]
    assert "stale" not in raw("converse", LISTEN)


def test_a_reply_heard_after_speaking_carries_the_same_notes(rig):
    """listen() is one function, so nothing is only true of the silent path."""
    _, voice, raw = rig
    voice.next_heard = [_heard_at("are you still there", ago=1200)]
    result = raw("converse", {"say": "Done, sir.", "then": "listen"})
    assert "stale" in result
    assert "EMIT converse()" in result


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
    key = Config().service.hotkey
    assert f"{key} key" in pause.description, "the configured key, not a hardcoded one"


def test_a_real_answer_then_a_quiet_room_is_not_punished(rig):
    """The case a refusal must never catch: it did speak, nobody replied, and
    listening again is exactly right."""
    _, voice, raw = rig
    raw("converse", LISTEN)
    voice.next_heard = []
    raw("converse", {"say": "Half past two, sir.", "then": "listen"})
    assert "did not listen" not in raw("converse", LISTEN)


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


def test_the_client_capabilities_are_logged_once(rig, caplog):
    """`sampling` decides whether the server can obtain model output at all
    rather than wait to be called, so it is worth knowing which clients offer
    it. Logged once - it does not change mid session."""
    import logging

    _, _voice, raw = rig
    with caplog.at_level(logging.INFO, logger="jarvis.mcp"):
        raw("converse", LISTEN)
        raw("converse", LISTEN)
    lines = [r.message for r in caplog.records if "Client capabilities" in r.message]
    assert len(lines) == 1


def test_asking_for_capabilities_outside_a_request_does_not_break_the_tool(rig):
    """Called directly, there is no request context and the lookup throws. That
    must not take the tool with it."""
    from jarvis.mcp_server import _capabilities

    class Hostile:
        @property
        def client_capabilities(self):
            raise RuntimeError("Context is not available outside of a request")

    assert "unavailable" in _capabilities(Hostile())
    assert "what time is it" in rig[2]("converse", LISTEN), "and the tool still works"


def test_entering_voice_mode_says_nothing():
    """Asked for explicitly: "jarvis" starts it listening, it does not answer
    back. An earlier version greeted on entry to establish the call pattern
    before the first real reply."""
    from jarvis.mcp_server import INSTRUCTIONS

    voice = FakeVoice()
    server = build_server(Config(), client=voice)
    asyncio.run(server.call_tool("converse", LISTEN))
    assert voice.said == [], "entering is silent"
    assert 'converse(say="", then="listen") at once' in INSTRUCTIONS
    assert "Yes sir?" not in INSTRUCTIONS, "no greeting is suggested either"


# ----------------------------------------------- what the live session got wrong


def screen_text(server, name, args=None):
    return text_of(asyncio.run(server.call_tool(name, args or {})))


def test_a_minimised_window_names_the_tool_that_fixes_it():
    """A live session hit this refusal four times in a row and never called
    focus_window. The message described the fix in prose - restore it, bring it
    forward - and never said which tool did that."""
    server, backend = screen_rig(True)
    backend._minimised = True
    result = screen_text(server, "look_at_screen")
    assert "minimised" in result
    assert "focus_window" in result, "the tool, by name"
    assert "will refuse again" in result, "and that retrying is pointless"


def test_with_control_off_it_says_nobody_here_can_restore_it():
    server, backend = screen_rig(False)
    backend._minimised = True
    result = screen_text(server, "look_at_screen")
    assert "focus_window" not in result, "that tool is not registered"
    assert "Ask the user" in result


def test_an_identical_scan_says_so():
    """Four identical scans of Spotify and five of the taskbar in one session,
    with nothing in the result to say that looking again had changed nothing."""
    server, _backend = screen_rig(True)
    first = screen_text(server, "look_at_screen")
    again = screen_text(server, "look_at_screen")
    assert "unchanged" not in first
    assert "identical to the last scan" in again
    assert "Do not call this again unchanged" in again


def test_a_changed_screen_is_not_called_a_loop():
    from conftest import button

    server, backend = screen_rig(True)
    screen_text(server, "look_at_screen")
    backend._elements = [button("Send"), button("Cancel", top=40)]
    assert "identical to the last scan" not in screen_text(server, "look_at_screen")


def test_a_filter_that_found_almost_nothing_says_to_widen_it():
    """The taskbar was scanned five times running at 2 targets out of 25. The
    word was wrong, not the window, and nothing said which."""
    server, _backend = screen_rig(True)
    result = screen_text(server, "look_at_screen", {"matching": "reply"})
    assert "narrowed this to 1" in result
    assert "without matching" in result


def test_acting_is_on_by_default():
    """It was off, on the argument that moving someone's pointer should be opted
    into. An agent cannot discover the flag, and the failure when it is off looks
    exactly like the feature being broken."""
    assert Config().screen.control is True
    names = {t.name for t in asyncio.run(build_server(Config(), client=FakeVoice()).list_tools())}
    assert {"click", "type_text", "scroll", "press_keys", "focus_window"} <= names


def test_the_read_only_half_is_still_available_on_request():
    """A read-only mode is a useful thing to be able to ask for. It is just no
    longer what you get without asking."""
    server, _backend = screen_rig(False)
    names = {t.name for t in asyncio.run(server.list_tools())}
    assert {"look_at_screen", "screenshot"} <= names
    assert not {"click", "type_text", "scroll", "press_keys", "focus_window"} & names


def test_typing_needs_no_target_when_the_caret_is_already_there(monkeypatch):
    """The Start menu after press_keys("win"): the search box has focus and there
    is nothing to click. target and expecting were both required, so this was
    unrepresentable and three attempts were refused instead."""
    from jarvis import hands

    typed: list = []
    monkeypatch.setattr(hands, "click", lambda *a, **k: typed.append("CLICK"))
    monkeypatch.setattr(hands, "type_text", lambda text: typed.append(text))
    monkeypatch.setattr(hands, "press", lambda keys: typed.append(f"<{keys}>"))

    server, _backend = screen_rig(True)
    result = screen_text(server, "type_text", {"text": "spotify", "then": "press_enter"})

    assert typed == ["spotify", "<enter>"], "no click, because no target was named"
    assert "keyboard focus" in result
    assert "escape" in result, "and a reminder to close what it opened"


def test_naming_a_target_without_saying_what_it_is_is_refused(monkeypatch):
    """`expecting` cannot be made conditionally required in a JSON schema, so
    leaving it out alongside a target would be the guard switched off."""
    from jarvis import hands

    monkeypatch.setattr(hands, "type_text", lambda text: pytest.fail("typed anyway"))
    server, _backend = screen_rig(True)
    result = screen_text(server, "type_text", {"text": "x", "then": "leave_it", "target": 1})
    assert "without `expecting`" in result


def test_naming_a_target_still_clicks_and_still_checks(monkeypatch):
    from jarvis import hands

    typed: list = []
    monkeypatch.setattr(hands, "click", lambda *a, **k: typed.append("CLICK"))
    monkeypatch.setattr(hands, "type_text", lambda text: typed.append(text))
    monkeypatch.setattr(hands, "press", lambda keys: typed.append(f"<{keys}>"))

    server, _backend = screen_rig(True)
    asyncio.run(server.call_tool("look_at_screen", {}))
    args = {"text": "hello", "then": "leave_it", "target": 1, "expecting": "Reply"}
    asyncio.run(server.call_tool("type_text", args))
    assert typed == ["CLICK", "hello"]

    args["expecting"] = "Delete"
    assert "not 'Delete'" in screen_text(server, "type_text", args)


def test_a_window_with_a_dead_tree_says_so_instead_of_offering_it():
    """The Start menu: one element, covering itself. It used to arrive looking
    like a window with a single button in it."""
    from conftest import FakeDesktop
    from jarvis.config import ScreenConfig
    from jarvis.screen import Element, Screen

    backend = FakeDesktop(
        [Element("Search box", "Edit", 0, 0, 800, 600)], title="Search", rect=(0, 0, 800, 600)
    )
    config = replace(Config(), screen=ScreenConfig(marks_file="", focus_settle_seconds=0.0))
    server = build_server(config, client=FakeVoice(), screen=Screen(config.screen, backend))
    result = screen_text(server, "look_at_screen")

    assert "nothing_clickable" in result
    assert "never populated" in result
    assert "type_text with no target" in result


def test_the_same_refusal_twice_says_something_different(monkeypatch):
    """Three identical refusals in a row, with a rescan between each, because the
    message said to look again and the new numbers were the same numbers."""
    from jarvis import hands

    monkeypatch.setattr(hands, "click", lambda *a, **k: None)
    server, backend = screen_rig(True)
    asyncio.run(server.call_tool("look_at_screen", {}))
    backend._elements = [button("Something else")]

    first = screen_text(server, "click", {"target": 1, "expecting": "Reply"})
    second = screen_text(server, "click", {"target": 1, "expecting": "Reply"})
    assert "identical refusal" not in first
    assert "identical refusal" in second
    assert "rather than trying a third time" in second


def test_a_window_still_building_itself_says_so():
    """Spotify had just launched: 25 elements, none usable. Ten seconds later the
    same window scanned 1741 elements down to 124 targets."""
    from jarvis.config import ScreenConfig
    from jarvis.screen import Element, Screen

    scenery = [Element("", "Pane", 0, 0, 800, 600), Element("", "Image", 0, 0, 4, 4)]
    config = replace(Config(), screen=ScreenConfig(marks_file="", focus_settle_seconds=0.0))
    backend = FakeDesktop(scenery, title="Spotify Premium")
    server = build_server(config, client=FakeVoice(), screen=Screen(config.screen, backend))

    result = text_of(asyncio.run(server.call_tool("look_at_screen", {})))
    assert "still_loading" in result
    assert "still building itself" in result
    assert "do not conclude it has nothing in it" in result


def test_the_capability_line_leads_with_sampling():
    """It is the one capability that decides whether the server can ever obtain
    model output rather than wait to be called, so the log says so plainly rather
    than listing names to be read."""
    from mcp_types import ClientCapabilities, SamplingCapability

    from jarvis.mcp_server import _capabilities

    class Ctx:
        def __init__(self, caps):
            self._caps = caps

        @property
        def client_capabilities(self):
            return self._caps

    assert "SAMPLING=yes" in _capabilities(Ctx(ClientCapabilities(sampling=SamplingCapability())))
    assert "SAMPLING=no" in _capabilities(Ctx(ClientCapabilities()))
    assert "SAMPLING=no" in _capabilities(Ctx(None))


def test_capabilities_are_read_as_a_property_not_called():
    """Calling it raised TypeError on every real session while the guard reported
    "unavailable", so three sessions went by without answering the one question
    this exists to answer."""
    from jarvis.mcp_server import _capabilities

    class Ctx:
        called = False

        @property
        def client_capabilities(self):
            return None

    assert "SAMPLING=no" in _capabilities(Ctx())
