"""The agent-facing tools.

Staying silent is a correct outcome, so an unanswered utterance is reported as a
note and never as a refusal.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from jarvis.config import Config
from jarvis.mcp_server import build_server


class FakeVoice:
    def __init__(self) -> None:
        self.said: list[str] = []
        self.next_heard = [{"text": "what time is it", "id": 1}]

    def status(self) -> dict:
        return {"cursor": 0}

    def heard(self, since=0, wait=0) -> dict:
        return {"heard": list(self.next_heard), "cursor": 1}

    def say(self, text: str) -> None:
        self.said.append(text)


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
    result = raw("wait_for_speech")
    assert "what time is it" in result
    assert "Can you answer right now? say() it." in result, "the decision, first"


def test_the_lead_in_rule_is_at_the_decision_point(rig):
    """The rule lived only in the instructions and jarvis.md - both read once and a
    long way back - while this result said "do the work, then call say() with the
    answer". The nearer text won, so the agent worked in silence instead."""
    _, _voice, raw = rig
    result = raw("wait_for_speech")
    assert "say one short line FIRST" in result, "before the work, not after"
    assert "in your own words" in result, "composed, not recited - one fixed line sounds robotic"
    assert "however many tool calls it takes" in result, "the work is not one call"
    assert "Do the work, then" not in result, "the contradiction is gone"


def test_the_instruction_is_short_and_read_last(rig):
    """A small model given five competing clauses picks whichever it read last, and
    `detail` used to be after this one."""
    _, _voice, raw = rig
    result = raw("wait_for_speech")
    start = result.index("Can you answer right now")
    end = result.index("wait_for_speech again.", start)
    assert end - start < 480, f"the instruction spans {end - start} characters"
    assert result.rindex("next_step") > result.rindex("detail")


def test_staying_silent_is_not_refused(rig):
    """The old behaviour blocked until say() was called. With no wake word that
    is wrong - most utterances deserve no reply, and refusing deadlocked the
    session against an agent that had correctly decided to keep quiet."""
    _, _voice, raw = rig
    raw("wait_for_speech")
    second = raw("wait_for_speech")
    assert "refused" not in second
    assert "what time is it" in second, "still listening, not blocked"


def test_silence_is_never_chased_up(rig):
    """A run of background chatter should not accumulate reminders - nagging an
    agent that is correctly keeping quiet only pushes it into replying."""
    _, _voice, raw = rig
    for _ in range(4):
        assert "did not reply" not in raw("wait_for_speech")


def test_an_unanswered_question_is_raised_once(rig):
    """The failure this guards: the agent works out an answer, then calls
    wait_for_speech instead of say, and the user hears nothing."""
    _, _voice, raw = rig
    raw("wait_for_speech")  # "what time is it" - a question
    second = raw("wait_for_speech")
    assert "never spoke an answer" in second
    third = raw("wait_for_speech")
    assert "never spoke an answer" in third, "still outstanding, still raised"


def test_answering_clears_it(rig):
    _, _voice, raw = rig
    raw("wait_for_speech")
    raw("say", {"text": "Half past two, sir."})
    assert "never spoke an answer" not in raw("wait_for_speech")


def test_silence_after_a_non_question_is_never_raised(rig):
    """Most silence is correct. Chasing it pushes the agent into answering
    things nobody asked."""
    _, voice, raw = rig
    voice.next_heard = [{"text": "and the other thing"}]
    raw("wait_for_speech")
    assert "never spoke an answer" not in raw("wait_for_speech")


def test_say_still_reaches_the_service(rig):
    _, voice, raw = rig
    assert "spoken" in raw("say", {"text": "Right you are."})
    assert voice.said == ["Right you are."]


def test_idle_returns_are_not_identical(rig):
    """Four identical empty results in a row read as a stuck loop to a client
    counting consecutive failures, which killed the session."""
    _, voice, raw = rig
    voice.next_heard = []

    results = [raw("wait_for_speech") for _ in range(3)]
    assert all("Not an error" in r for r in results)
    assert len(set(results)) == 3, "each idle result differs from the last"
    assert "waited_seconds" in results[0]


def test_the_idle_counter_resets_once_something_is_said(rig):
    _, voice, raw = rig
    voice.next_heard = []
    raw("wait_for_speech")
    raw("wait_for_speech")

    voice.next_heard = [{"text": "right, carry on", "id": 9}]
    assert "waited_seconds" not in raw("wait_for_speech")

    voice.next_heard = []
    first_idle_again = raw("wait_for_speech")
    assert '"waited_seconds": 240' in first_idle_again or "waited_seconds" in first_idle_again


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
    voice.cursor = 7  # spoken between launch and the first wait_for_speech

    asyncio.run(server.call_tool("wait_for_speech", {}))
    assert asked_from == [7], "listening starts from now, not from launch"

    # After that, nothing is skipped: a queued utterance is one spoken while the
    # agent was busy, which is exactly what it must not miss.
    voice.cursor = 12
    asyncio.run(server.call_tool("wait_for_speech", {}))
    assert asked_from[1] == 7


def _heard_at(text: str, ago: float) -> dict:
    at = datetime.now(UTC) - timedelta(seconds=ago)
    return {"text": text, "id": 1, "at": at.isoformat(timespec="seconds")}


def test_an_old_utterance_is_flagged_as_a_leftover(rig):
    _, voice, raw = rig
    voice.next_heard = [_heard_at("thank you", ago=1200)]
    result = raw("wait_for_speech")
    assert "stale" in result
    assert "1200s ago" in result
    assert "said_seconds_ago" in result


def test_something_just_said_is_not_flagged(rig):
    _, voice, raw = rig
    voice.next_heard = [_heard_at("what time is it", ago=2)]
    result = raw("wait_for_speech")
    assert "stale" not in result
    assert "said_seconds_ago" in result


def test_speaking_does_not_end_the_conversation(rig):
    """Ending the turn after say() looks like walking off mid sentence."""
    _, _voice, raw = rig
    result = raw("say", {"text": "Half past two, sir."})
    assert "wait_for_speech" in result
    assert "do NOT finish the task" in result
