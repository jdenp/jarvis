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
        self.paused = False

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

    def pause(self) -> None:
        self.paused = True

    def resume(self) -> None:
        self.paused = False

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


def test_say_returns_before_the_speech_finishes():
    """say() used to block until playback ended, holding the agent for the
    length of the reply. SpeechEngine.say only queues; the waiting is now done
    on a background thread rather than in the caller."""
    service, _microphone, speech = make_service()
    playing = threading.Event()
    speech.wait = lambda timeout=None: playing.wait(timeout=5)

    began = time.monotonic()
    service.say("A reply long enough to take a while to read out.")
    assert time.monotonic() - began < 0.5, "returned without waiting for playback"
    assert speech.said == ["A reply long enough to take a while to read out."]
    playing.set()


def test_the_microphone_is_muted_for_the_whole_of_a_reply():
    service, microphone, speech = make_service()
    playing = threading.Event()
    speech.wait = lambda timeout=None: playing.wait(timeout=5)

    service.say("First sentence.")
    assert microphone.muted is True, "muted as soon as speaking starts"
    time.sleep(0.2)
    assert microphone.muted is True, "still muted while playing"

    playing.set()
    for _ in range(50):
        if not microphone.muted:
            break
        time.sleep(0.05)
    assert microphone.muted is False, "released once the queue drained"


def test_overlapping_replies_do_not_unmute_early():
    """Two replies in flight: the first finishing must not open the microphone
    while the second is still playing."""
    service, microphone, speech = make_service()
    drained = threading.Event()
    speech.wait = lambda timeout=None: drained.wait(timeout=5)

    service.say("First.")
    service.say("Second.")
    assert microphone.muted is True
    time.sleep(0.2)
    assert microphone.muted is True, "still speaking"

    drained.set()
    for _ in range(50):
        if not microphone.muted:
            break
        time.sleep(0.05)
    assert microphone.muted is False


def test_listen_while_speaking_leaves_the_microphone_open():
    config_audio = replace(Config().audio, listen_while_speaking=True)
    service, microphone, speech = make_service(audio=config_audio)
    service.say("Talking over myself.")
    assert microphone.muted is False, "full duplex: never muted"
    assert speech.said == ["Talking over myself."]


def test_pause_stops_recording():
    service, _, _ = make_service()
    assert service.transcript.paused is False
    result = service.pause()
    assert result is True
    assert service.transcript.paused is True
    service.transcript.add("paused utterance")
    assert [item.text for item in service.transcript.since(0)] == []


def test_pause_stops_the_microphone_not_just_the_delivery():
    """Gating only the transcript still captured, still ran Whisper, still
    logged the utterance and still wrote it to heard.jsonl. Paused has to mean
    the microphone is not being read."""
    service, microphone, _ = make_service()
    service.pause()
    assert microphone.paused is True
    service.resume()
    assert microphone.paused is False


def test_speaking_while_paused_does_not_resume_listening():
    """say() ends with unmute(). Sharing one flag would have a finished reply
    silently undo a pause the user asked for."""
    service, microphone, _ = make_service()
    service.pause()
    service.say("Still here, sir.")
    for _ in range(50):
        if not microphone.muted:
            break
        time.sleep(0.05)
    assert microphone.muted is False, "the echo gate released as usual"
    assert microphone.paused is True, "but the pause stands"


def test_resume_resumes_recording():
    service, _, _ = make_service()
    service.transcript.add("before pause")
    cursor = service.transcript.cursor
    service.pause()
    service.resume()
    service.transcript.add("after resume")
    assert [item.text for item in service.transcript.since(cursor)] == ["after resume"]


def test_status_includes_paused():
    service, _, _ = make_service()
    status = service.status()
    assert "paused" in status
    assert status["paused"] is False
