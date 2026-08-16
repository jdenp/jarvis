"""The agent-facing tools.

There is no wake word, so everything heard reaches the agent and it decides what
was meant for it. Staying silent is a correct outcome, which is why the server
reports an unanswered utterance as a note and never as a refusal.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from jarvis.config import Config
from jarvis.mcp_server import build_server


class FakeVoice:
    def __init__(self) -> None:
        self.said: list[str] = []
        self.next_heard = [{"text": "what time is it", "id": 1}]

    def status(self) -> dict:
        return {"cursor": 0}

    def heard(self, since=0, wait=0, settle=None) -> dict:
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
    assert "Meant for you?" in result
    assert "must be say()" in result, "the action, not just the nuance"


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
