"""The nudge that gets an agent to speak, and its escape hatch.

An agent that answers in chat has said nothing the user can hear. The server
notices and pushes back once - but only once, because refusing forever is a
livelock: the agent keeps writing text, the client counts consecutive tool
failures, and the session dies.
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
        self.next_heard = [{"text": "jarvis hey", "command": "hey", "addressed": True, "id": 1}]

    def status(self) -> dict:
        return {"cursor": 0}

    def heard(self, since=0, wait=0, addressed_only=False, settle=None) -> dict:
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


def test_answering_in_text_gets_pushed_back_once(rig):
    _, _voice, raw = rig
    assert "refused" not in raw("wait_for_speech")
    # Agent wrote its answer in chat instead of speaking, then listened again.
    second = raw("wait_for_speech")
    assert "refused" in second
    assert "jarvis hey" in second or "hey" in second


def test_it_gives_up_rather_than_deadlocking_the_session(rig):
    """The bug this guards: three refusals in a row tripped Cline's consecutive
    mistake limit and killed the run."""
    _, _voice, raw = rig
    raw("wait_for_speech")
    assert "refused" in raw("wait_for_speech"), "one nudge"
    third = raw("wait_for_speech")
    assert "refused" not in third, "must let it through rather than loop"
    assert "hey" in third


def test_speaking_clears_it_and_no_nudge_follows(rig):
    _, voice, raw = rig
    raw("wait_for_speech")
    raw("say", {"text": "Hello there."})
    assert voice.said == ["Hello there."]
    assert "refused" not in raw("wait_for_speech")


def test_the_nudge_is_armed_again_for_the_next_utterance(rig):
    _, _voice, raw = rig
    raw("wait_for_speech")
    raw("say", {"text": "answered"})
    raw("wait_for_speech")  # picks up the next utterance
    assert "refused" in raw("wait_for_speech"), "each utterance gets its own nudge"
