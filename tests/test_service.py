"""The voice service and its HTTP face, with fakes in place of the hardware."""

from __future__ import annotations

import logging
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
    assert service._classify("Jarvis, open the config file") == (True, "open the config file")


def test_speech_without_the_name_is_still_passed_on():
    """No wake word by default - the agent judges what was meant for it."""
    service, _, _ = make_service()
    assert service._classify("just muttering to myself") == (True, "just muttering to myself")


def test_the_name_can_be_made_mandatory_again():
    service, _, _ = make_service(wake=replace(Config().wake, required=True))
    assert service._classify("just muttering to myself") == (False, "just muttering to myself")
    assert service._classify("jarvis open it") == (True, "open it")


def test_bare_wake_word_is_kept_rather_than_sent_as_an_empty_command():
    service, _, _ = make_service()
    assert service._classify("jarvis") == (True, "jarvis")


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
    assert status["wake_word_required"] is False
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


def test_overheard_chatter_does_not_wake_a_waiting_agent(running):
    service, client, _ = running
    service.transcript.add("something about the weather", addressed=False)
    assert client.heard(since=0, wait=0.3, addressed_only=True)["heard"] == []


def test_being_addressed_wakes_it_and_brings_the_chatter_along(running):
    """The split-request case: unaddressed speech is context, not an instruction."""
    service, client, _ = running
    service.transcript.add("open the config file", addressed=False)
    service.transcript.add("jarvis", addressed=True, command="jarvis")

    result = client.heard(since=0, wait=5, addressed_only=True, settle=0)
    texts = [item["text"] for item in result["heard"]]
    assert texts == ["open the config file", "jarvis"], "both, in order"
    assert [item["addressed"] for item in result["heard"]] == [False, True]


def test_the_settle_window_catches_a_hesitation_after_the_wake_word(running):
    """Say "jarvis", pause, then the request. Returning on the first phrase
    alone would hand the agent nothing to act on."""
    service, client, _ = running
    service.transcript.add("jarvis", addressed=True, command="jarvis")
    threading.Timer(
        0.2, lambda: service.transcript.add("what is the weather", addressed=False)
    ).start()

    result = client.heard(since=0, wait=5, addressed_only=True, settle=1.0)
    assert [item["text"] for item in result["heard"]] == ["jarvis", "what is the weather"]


def test_a_timeout_does_not_swallow_the_chatter_it_waited_through(running):
    """The bug: /heard reported the transcript's cursor even when it returned
    nothing, so unaddressed speech that arrived during a wait was skipped and
    never reached the agent as context."""
    service, client, _ = running
    start = client.status()["cursor"]

    service.transcript.add("the one in the parser", addressed=False)
    timed_out = client.heard(since=start, wait=0.3, addressed_only=True)
    assert timed_out["heard"] == []
    assert timed_out["cursor"] == start, "cursor must not move past undelivered context"

    service.transcript.add("jarvis fix it", addressed=True, command="fix it")
    result = client.heard(since=timed_out["cursor"], wait=5, addressed_only=True, settle=0)
    assert [i["text"] for i in result["heard"]] == ["the one in the parser", "jarvis fix it"]


def test_the_cursor_only_advances_past_what_was_delivered(running):
    service, client, _ = running
    start = client.status()["cursor"]
    first = service.transcript.add("jarvis one", addressed=True, command="one")
    result = client.heard(since=start, wait=2, addressed_only=True, settle=0)
    assert result["cursor"] == first.id


def test_speech_during_a_long_task_is_waiting_at_the_next_checkpoint(running):
    """Nothing is missed while the agent is busy - it is queued behind the cursor."""
    service, client, _ = running
    cursor = client.status()["cursor"]
    for text in ("and check the tests too", "actually never mind that"):
        service.transcript.add(text, addressed=False)
    service.transcript.add("jarvis are you there", addressed=True, command="are you there")

    result = client.heard(since=cursor, wait=5, addressed_only=True, settle=0)
    assert len(result["heard"]) == 3, "everything said while busy is still there"


def test_a_client_hanging_up_does_not_print_a_traceback(running, caplog):
    """A long poll held open for a minute means clients disappear mid request
    all the time. socketserver prints the whole stack for each one, which in the
    service's own window looks like something broke."""
    service, _client, _ = running
    server = build_server(service)
    try:
        with caplog.at_level(logging.DEBUG, logger="jarvis.service"):
            try:
                raise ConnectionResetError(10054, "forcibly closed by the remote host")
            except ConnectionResetError:
                server.handle_error(object(), ("127.0.0.1", 61004))
    finally:
        server.server_close()

    assert "Traceback" not in caplog.text
    assert "went away mid request" in caplog.text
    assert not any(r.levelno >= logging.WARNING for r in caplog.records), "not an error"


def test_a_real_error_is_still_reported(running, caplog):
    service, _client, _ = running
    server = build_server(service)
    try:
        with caplog.at_level(logging.DEBUG, logger="jarvis.service"):
            try:
                raise ValueError("something genuinely broke")
            except ValueError:
                server.handle_error(object(), ("127.0.0.1", 61004))
    finally:
        server.server_close()

    assert "something genuinely broke" in caplog.text
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


def test_client_says_how_to_fix_it_when_nothing_is_listening():
    client = VoiceClient(ServiceConfig(port=9))
    with pytest.raises(ServiceUnavailable, match="jarvis serve"):
        client.status()
