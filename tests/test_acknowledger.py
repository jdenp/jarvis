"""The holding line that fills dead air while an answer is being worked out."""

from __future__ import annotations

import time
from dataclasses import replace

from jarvis.config import ServiceConfig
from jarvis.mcp_server import Acknowledger


class FakeVoice:
    def __init__(self) -> None:
        self.said: list[str] = []

    def say(self, text: str) -> None:
        self.said.append(text)


def make(**overrides) -> tuple[Acknowledger, FakeVoice]:
    overrides.setdefault("acknowledge_after", 0.15)
    config = replace(ServiceConfig(), **overrides)
    voice = FakeVoice()
    return Acknowledger(voice, config), voice


def test_a_slow_answer_gets_a_holding_line():
    ack, voice = make()
    ack.arm()
    time.sleep(0.4)
    assert voice.said == ["Let me have a look."]


def test_a_quick_answer_gets_no_holding_line():
    """The common case - do not put "one moment" in front of an instant reply."""
    ack, voice = make()
    ack.arm()
    time.sleep(0.05)
    ack.cancel()
    time.sleep(0.3)
    assert voice.said == []


def test_phrases_rotate_rather_than_repeating():
    ack, voice = make()
    for _ in range(3):
        ack.arm()
        time.sleep(0.3)
    assert len(voice.said) == 3
    assert len(set(voice.said)) == 3, "hearing the same holding line every time grates"
    assert set(voice.said) <= set(ServiceConfig().acknowledgements)


def test_arming_again_replaces_the_pending_line():
    ack, voice = make()
    ack.arm()
    ack.arm()
    time.sleep(0.4)
    assert len(voice.said) == 1, "re-arming should not queue a second line"


def test_zero_disables_it():
    ack, voice = make(acknowledge_after=0)
    assert ack.enabled is False
    ack.arm()
    time.sleep(0.3)
    assert voice.said == []


def test_no_phrases_disables_it():
    ack, voice = make(acknowledgements=())
    assert ack.enabled is False
    ack.arm()
    time.sleep(0.3)
    assert voice.said == []


def test_cancel_is_safe_when_nothing_is_armed():
    ack, _ = make()
    ack.cancel()
    ack.cancel()


def test_a_failing_voice_does_not_escape():
    class Broken(FakeVoice):
        def say(self, text: str) -> None:
            raise RuntimeError("service went away")

    config = replace(ServiceConfig(), acknowledge_after=0.15)
    ack = Acknowledger(Broken(), config)
    ack.arm()
    time.sleep(0.4)  # must not raise on the timer thread
