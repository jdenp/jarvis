"""The voice service and its HTTP face, with fakes in place of the hardware."""

from __future__ import annotations

import threading
import time
from dataclasses import replace

import pytest

from jarvis.client import ServiceUnavailable, VoiceClient
from jarvis.config import Config, ServiceConfig
from jarvis.service import VoiceService, build_server
from jarvis.transcript import Transcript


class FakeMicrophone:
    def __init__(self) -> None:
        self.muted = False
        self.mute_calls = 0
        self.started = False

    def start(self) -> None:
        self.started = True

    def listen(self, timeout=None):
        time.sleep(0.01)
        return None

    def mute(self) -> None:
        self.muted = True
        self.mute_calls += 1

    def unmute(self) -> None:
        self.muted = False

    def stop(self) -> None:
        self.started = False


class FakeSpeech:
    def __init__(self, microphone: FakeMicrophone | None = None) -> None:
        self.said: list[str] = []
        self.muted_while_speaking: list[bool] = []
        self._microphone = microphone
        self.is_local = True

    def say(self, text: str) -> None:
        # Record whether the mic was muted at the moment we spoke.
        self.muted_while_speaking.append(bool(self._microphone and self._microphone.muted))
        self.said.append(text)

    def wait(self, timeout=None) -> bool:
        return True

    def close(self) -> None:
        pass


def make_service(**config_overrides) -> tuple[VoiceService, FakeMicrophone, FakeSpeech]:
    config = replace(Config(), **config_overrides)
    microphone = FakeMicrophone()
    speech = FakeSpeech(microphone)
    service = VoiceService(
        config,
        microphone=microphone,
        transcriber=object(),
        speech=speech,
        transcript=Transcript(),
    )
    return service, microphone, speech


# ------------------------------------------------------------------ wake word


def test_wake_word_is_stripped_before_the_agent_sees_it():
    service, _, _ = make_service()
    assert service._apply_wake_word("Jarvis, open the config file") == "open the config file"


def test_utterance_without_the_wake_word_is_dropped():
    service, _, _ = make_service()
    assert service._apply_wake_word("just muttering to myself") is None


def test_everything_gets_through_when_the_wake_word_is_not_required():
    config_wake = replace(Config().wake, required=False)
    service, _, _ = make_service(wake=config_wake)
    assert service._apply_wake_word("just muttering to myself") == "just muttering to myself"


def test_bare_wake_word_is_kept_rather_than_sent_as_an_empty_command():
    service, _, _ = make_service()
    assert service._apply_wake_word("jarvis") == "jarvis"


# ---------------------------------------------------------------------- speak


def test_say_mutes_the_microphone_while_speaking():
    service, microphone, speech = make_service()
    service.say("Opening it now.")
    assert speech.said == ["Opening it now."]
    assert speech.muted_while_speaking == [True], "mic must be muted at the moment of speaking"
    assert microphone.muted is False, "and released afterwards"


def test_say_ignores_empty_text():
    service, microphone, speech = make_service()
    service.say("   ")
    assert speech.said == []
    assert microphone.mute_calls == 0


def test_status_reports_the_backends_in_use():
    service, _, _ = make_service()
    status = service.status()
    assert status["stt"] == "whisper"
    assert status["wake_word_required"] is True
    assert status["cursor"] == 0


# ----------------------------------------------------------------------- http


@pytest.fixture
def running():
    """A service behind a real HTTP server on an ephemeral port."""
    service, _microphone, speech = make_service(service=ServiceConfig(port=0))
    server = build_server(service)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = VoiceClient(replace(service.config.service, port=port))
    try:
        yield service, client, speech
    finally:
        client.close()
        server.shutdown()
        server.server_close()


def test_status_over_http(running):
    _, client, _ = running
    assert client.status()["listening"] is False


def test_heard_returns_what_was_recorded(running):
    service, client, _ = running
    service.transcript.add("open the config file")
    result = client.heard(since=0)
    assert [item["text"] for item in result["heard"]] == ["open the config file"]
    assert result["cursor"] == 1


def test_heard_with_a_cursor_does_not_repeat_itself(running):
    service, client, _ = running
    service.transcript.add("first")
    cursor = client.heard(since=0)["cursor"]
    assert client.heard(since=cursor)["heard"] == []
    service.transcript.add("second")
    assert [i["text"] for i in client.heard(since=cursor)["heard"]] == ["second"]


def test_heard_blocks_until_something_is_said(running):
    """The interrupt an agent waits on, end to end over HTTP."""
    service, client, _ = running
    threading.Timer(0.3, lambda: service.transcript.add("spoken while blocked")).start()

    started = time.monotonic()
    result = client.heard(since=0, wait=10)
    elapsed = time.monotonic() - started

    assert [item["text"] for item in result["heard"]] == ["spoken while blocked"]
    assert 0.2 < elapsed < 3.0, "should return on notify, not on timeout"


def test_heard_times_out_quietly_when_nothing_is_said(running):
    _, client, _ = running
    assert client.heard(since=0, wait=0.3)["heard"] == []


def test_say_over_http_reaches_the_speaker(running):
    _, client, speech = running
    assert client.say("Understood.")["spoken"] == "Understood."
    assert speech.said == ["Understood."]


def test_client_says_how_to_fix_it_when_nothing_is_listening():
    client = VoiceClient(ServiceConfig(port=9))
    with pytest.raises(ServiceUnavailable, match="jarvis serve"):
        client.status()
