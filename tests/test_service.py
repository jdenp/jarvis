"""The voice service and its HTTP face, with fakes in place of the hardware."""

from __future__ import annotations

import logging
import queue
import threading
import time
from contextlib import contextmanager
from dataclasses import replace

import httpx
import pytest

from jarvis import service as service_module
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
        self.deferred = False
        # A real one hands this to the web app's source to deliver into.
        self.sink: queue.Queue = queue.Queue(maxsize=16)

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

    def defer(self, elsewhere: bool) -> None:
        self.deferred = elsewhere

    def stop(self) -> None:
        self.started = False


class FakeSpeech:
    def __init__(self, microphone: FakeMicrophone | None = None) -> None:
        self.said: list[str] = []
        self.muted_while_speaking: list[bool] = []
        self._microphone = microphone
        self.is_local = True
        # Stays true until something interrupts it, which is the state anything
        # about being talked over needs to be in.
        self.speaking = False
        self.interrupts = 0
        # What render() hands back. None is every engine but Kokoro.
        self.wav: bytes | None = None

    def say(self, text: str) -> None:
        # Record whether the mic was muted at the moment we spoke.
        self.muted_while_speaking.append(bool(self._microphone and self._microphone.muted))
        self.said.append(text)
        self.speaking = True

    def render(self, text: str) -> bytes | None:
        return self.wav

    def wait(self, timeout=None) -> bool:
        return True

    def interrupt(self) -> None:
        self.said.clear()
        self.speaking = False
        self.interrupts += 1

    def close(self) -> None:
        pass


class Phrases:
    """A microphone and a transcriber in one, handing over canned phrases.

    The audio it hands over is the text, which saves a second fake whose only
    job would be turning one into the other. Muting does not stop it: a phrase
    already captured is exactly the case worth testing.
    """

    def __init__(self, *said: str) -> None:
        self.queued: queue.Queue[str] = queue.Queue()
        for phrase in said:
            self.queued.put(phrase)

    def listen(self, timeout=None):
        try:
            return self.queued.get(timeout=timeout or 0.05)
        except queue.Empty:
            return None

    def transcribe(self, audio):
        return audio

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def mute(self) -> None:
        pass

    def unmute(self) -> None:
        pass

    def pause(self) -> None:
        pass

    def resume(self) -> None:
        pass

    def defer(self, elsewhere: bool) -> None:
        pass


@contextmanager
def listening(**audio_overrides):
    """A service with its listen loop actually running on canned phrases."""
    config = replace(Config(), audio=replace(Config().audio, **audio_overrides))
    ears = Phrases()
    speech = FakeSpeech()
    service = VoiceService(
        config, microphone=ears, transcriber=ears, speech=speech, transcript=Transcript()
    )
    service._running.set()
    thread = threading.Thread(target=service._listen, name="test-listen", daemon=True)
    thread.start()
    try:
        yield service, ears, speech
    finally:
        service._running.clear()
        thread.join(timeout=2)


def waited(until, seconds: float = 2.0) -> bool:
    """Poll for something a background thread does. False if it never did."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if until():
            return True
        time.sleep(0.01)
    return False


def half_duplex(**overrides) -> tuple[VoiceService, FakeMicrophone, FakeSpeech]:
    """A service that mutes while speaking.

    Half duplex is the default, and this stays explicit anyway: anything testing
    the echo gate should say which mode it means rather than inherit it.
    """
    audio = replace(Config().audio, listen_while_speaking=False, **overrides)
    return make_service(audio=audio)


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
    service, microphone, speech = half_duplex()
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
    service, microphone, speech = half_duplex()
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
    service, microphone, speech = half_duplex()
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


def test_headphone_mode_starts_where_the_config_says():
    service, _microphone, _speech = make_service()
    assert service.headphones is False, "speakers, so it cannot hear itself"
    wearing = replace(Config().audio, listen_while_speaking=True)
    service, _microphone, _speech = make_service(audio=wearing)
    assert service.headphones is True


def test_headphone_mode_can_be_switched_without_a_restart():
    """Headphones go on and come off during a day, which a config read at
    startup cannot follow. It is the same switch, with a key on it."""
    service, _microphone, speech = half_duplex()
    assert service.wear_headphones(True) is True
    service.say("Talking over myself.")
    assert speech.muted_while_speaking == [False], "left open"

    assert service.wear_headphones(False) is False
    service.say("Not any more.")
    assert speech.muted_while_speaking == [False, True]


def test_holding_the_key_switches_headphone_mode(monkeypatch):
    """A tap shuts the microphone and the same key held is the other thing,
    because the moment you want it is the moment you have just put headphones
    on and are not sitting in front of a config file."""
    captured = {}

    class FakeListener:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def start(self):
            pass

    monkeypatch.setattr("jarvis.service.HotkeyListener", FakeListener)
    service, microphone, _speech = make_service()
    service._start_hotkey()

    captured["on_hold"]()
    assert service.headphones is True
    captured["on_hold"]()
    assert service.headphones is False
    assert microphone.paused is False, "and the microphone was never shut"


def test_pause_shuts_the_desk_microphone():
    service, microphone, _ = make_service()
    assert service.paused is False
    assert service.pause() is True
    assert service.paused is True
    assert microphone.paused is True
    assert service.pause() is False, "already shut"

    service.resume()
    assert service.paused is False
    assert microphone.paused is False


def test_pausing_the_desk_leaves_the_phone_listening(app):
    """Num Lock is a key on the desk. Somebody pressing it on their way out is
    muting the room they are leaving, not the phone in their pocket - which was
    not true while this stopped the transcript, and that is every source."""
    service, microphone, port = app
    get(port, "/spoken?since=0")
    service.live.settle()

    service.pause()
    assert microphone.paused, "the desk is shut"
    assert not service.transcript.paused, "and nothing else is"

    service.transcript.add("something the phone heard")
    assert [x.text for x in service.transcript.since(0)] == ["something the phone heard"]


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


def test_half_duplex_is_the_default():
    """The microphone shuts while JARVIS talks, because on speakers it hears
    its own voice and only echo.py stands between that and it answering
    itself."""
    service, _microphone, speech = make_service()
    assert service.headphones is False
    service.say("Opening it now.")
    assert speech.muted_while_speaking == [True]


def test_talking_over_a_reply_cuts_it_off():
    """Late by the length of the pause that ended the phrase, because nothing
    is heard until it has been said. It saves the rest of a wrong answer, which
    is the point, not the first syllable."""
    with listening(listen_while_speaking=True) as (service, ears, speech):
        service.say("A long answer to a question nobody asked.")
        ears.queued.put("no, stop, do the other one")

        assert waited(lambda: speech.interrupts == 1), "the reply was not cut off"
        assert waited(lambda: bool(service.transcript.since(0)))
        assert [item.text for item in service.transcript.since(0)] == ["no, stop, do the other one"]


def test_a_phrase_from_before_the_reply_does_not_cut_it_off():
    """Half duplex shuts the microphone while it talks, so a phrase landing now
    was recorded before the reply started. Nobody talked over anything."""
    with listening(listen_while_speaking=False) as (service, _ears, speech):
        service.say("Opening it now.")
        service.microphone.queued.put("open spotify")

        assert waited(lambda: bool(service.transcript.since(0)))
        assert speech.interrupts == 0
        assert speech.said == ["Opening it now."]


def test_typing_cuts_a_reply_off_whatever_the_audio_settings():
    """Nothing typed can be an echo and nothing typed arrives late, so this one
    does not need the microphone to have been open."""
    service, _, speech = half_duplex()
    service.say("A long answer to a question nobody asked.")
    service.typed("stop")
    assert speech.interrupts == 1
    assert speech.said == []


def test_nothing_is_cut_off_when_it_is_not_talking():
    service, _, speech = make_service()
    service.typed("what is the time")
    assert speech.interrupts == 0


def test_hushing_drops_what_was_queued_and_cuts_off_what_is_playing():
    """What escape reaches. Told to stop, a reply half way through a sentence
    has to stop there rather than finish the paragraph first."""
    service, _, speech = half_duplex()
    service.say("A rather long answer, sir.")
    assert speech.said == ["A rather long answer, sir."]
    service.hush()
    assert speech.said == []


# ------------------------------------------------------------------- web app


def webapp(on: bool = True, **overrides):
    """A service with the page switched on, behind a real HTTP server.

    Energy detection rather than Silero: this is about the plumbing, and there
    is no point loading a network to score buffers of zeroes with.
    """
    settings = ServiceConfig(port=0, start_webapp=on, **overrides)
    service, microphone, _speech = make_service(
        service=settings, audio=replace(Config().audio, vad="energy")
    )
    service._start_webapp_source()
    server = build_server(service)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return service, microphone, server.server_address[1], server


def get(port, path):
    return httpx.get(f"http://127.0.0.1:{port}{path}")


def post(port, path, **kwargs):
    return httpx.post(f"http://127.0.0.1:{port}{path}", **kwargs)


@pytest.fixture
def app():
    service, microphone, port, server = webapp()
    try:
        yield service, microphone, port
    finally:
        service.stop()
        server.shutdown()
        server.server_close()


def test_the_page_is_absent_when_it_is_switched_off():
    """On by default, because it opens nothing until somebody puts Tailscale in
    front of it. Off it is not there at all rather than present and refusing,
    which is how every other switch here behaves."""
    service, _microphone, port, server = webapp(on=False)
    try:
        assert get(port, "/").status_code == 404
        assert post(port, "/audio", content=b"\x00\x00").status_code == 404
        assert post(port, "/typed", json={"text": "hello"}).status_code == 404
        assert get(port, "/spoken").status_code == 404
    finally:
        service.stop()
        server.shutdown()
        server.server_close()


def test_the_page_is_served_when_it_is_on(app):
    _, _, port = app
    reply = get(port, "/")
    assert reply.status_code == 200
    assert "text/html" in reply.headers["content-type"]
    assert "<title>JARVIS</title>" in reply.text


def test_a_typed_line_arrives_as_though_it_had_been_heard(app):
    service, _, port = app
    assert post(port, "/typed", json={"text": "open spotify"}).status_code == 200
    assert [item.text for item in service.transcript.since(0)] == ["open spotify"]


def test_a_typed_line_needs_something_to_say(app):
    _, _, port = app
    assert post(port, "/typed", json={}).status_code == 400


def test_what_jarvis_said_is_readable_by_the_page(app):
    """The speech comes out of the speakers at the desk, so the page needs
    somewhere to read the other half of the conversation from."""
    service, _, port = app
    service.say("Spotify is open, sir.")

    body = get(port, "/spoken?since=0").json()
    assert [item["text"] for item in body["spoken"]] == ["Spotify is open, sir."]
    assert body["cursor"] == 1


def test_what_was_said_is_not_written_to_the_transcript_file(app):
    """heard.jsonl is what was heard. The other half is in memory and goes when
    the process does, which is all the page asks of it."""
    service, _, _ = app
    service.say("Spotify is open, sir.")
    assert service.transcript.since(0) == []
    assert service.spoken.path is None


def test_audio_reaches_the_capture_loop(app):
    service, _, port = app
    assert post(port, "/audio", content=b"\x11\x22" * 100).json() == {"samples": 100}
    assert service.stream is not None and service.stream.live


def test_half_a_sample_is_not_a_sample(app):
    """One odd byte through and every buffer after it is out of phase."""
    service, _, port = app
    assert post(port, "/audio", content=b"\x01\x02\x03").json() == {"samples": 1}
    assert service.stream is not None
    assert service.stream.read(1) == b"\x01\x02"


def test_the_desk_stands_down_for_as_long_as_the_page_is_open(app):
    """Not just while audio is arriving. Somebody holding a phone is not at the
    desk, so a desk microphone between two sentences is listening to a room
    nobody is in."""
    service, microphone, port = app
    assert not microphone.deferred

    post(port, "/audio", content=b"\x00\x00" * 100)
    assert microphone.deferred
    assert service.live.mic is service.remote

    # Quiet, but still there - which used to hand the desk its microphone back.
    service.stream._live.clear()
    service.live.settle()
    assert microphone.deferred, "still the page's floor between sentences"
    assert service.remote is not None and not service.remote._deferred.is_set()


def test_the_desk_gets_its_microphone_back_when_the_page_goes_away():
    """Nothing says goodbye. A tab that is closed and a phone that walks out of
    range both just stop, so the listen loop asks how long ago a page last
    spoke rather than waiting to be told."""
    service, microphone, port, server = webapp()
    try:
        post(port, "/audio", content=b"\x00\x00" * 100)
        assert microphone.deferred, "the page has the floor while it is here"

        service._page_seen -= service_module.PAGE_GONE + 1
        service._running.set()
        listener = threading.Thread(target=service._listen, daemon=True)
        listener.start()
        for _ in range(60):
            if not microphone.deferred:
                break
            time.sleep(0.05)
        service._running.clear()
        listener.join(timeout=2)
        assert not microphone.deferred
    finally:
        service.stop()
        server.shutdown()
        server.server_close()


def test_status_says_whether_the_page_is_there(app):
    service, _, port = app
    assert get(port, "/status").json()["webapp"] is True
    assert service.status()["streaming"] is False


def test_the_page_does_not_listen_without_a_queue_to_share(caplog):
    """A microphone that is not one - a stub, or a front end that has no
    hardware - has no queue for a second source to deliver into."""
    service, microphone, _ = make_service(service=ServiceConfig(start_webapp=True))
    microphone.sink = None
    with caplog.at_level(logging.WARNING, logger="jarvis.service"):
        service._start_webapp_source()
    assert service.stream is None
    assert "cannot listen" in caplog.text


def test_the_phone_is_muted_while_jarvis_talks_as_well(app):
    """A phone in the same room hears the reply coming out of the desk speakers
    as clearly as the desk does."""
    service, desk, _ = app
    phone = FakeMicrophone()
    service.live.web = phone

    service.say("Spotify is open, sir.")
    assert phone.mute_calls == 1
    assert desk.mute_calls == 1


# ------------------------------------------------- the reply, in the other room


def test_with_no_page_open_it_is_spoken_here_as_always(app):
    """Nothing about this changes for somebody sitting at the desk."""
    service, _, _ = app
    service.speech.wav = b"RIFFrendered"
    service.say("Spotify is open, sir.")
    assert service.speech.said == ["Spotify is open, sir."]
    assert service.clips == {}


def test_a_page_that_is_polling_gets_the_reply_instead(app):
    """A machine talking to an empty room is no use to somebody in another one."""
    service, _, port = app
    service.speech.wav = b"RIFFrendered"
    get(port, "/spoken?since=0")  # what the page does continuously

    service.say("Spotify is open, sir.")
    assert service.speech.said == [], "not out of the speakers here"
    assert list(service.clips.values()) == [b"RIFFrendered"]


def test_the_clip_is_fetched_by_the_id_of_the_line(app):
    service, _, port = app
    service.speech.wav = b"RIFFrendered"
    get(port, "/spoken?since=0")
    service.say("Spotify is open, sir.")

    line = get(port, "/spoken?since=0").json()["spoken"][0]
    clip = get(port, f"/voice/{line['id']}.wav")
    assert clip.status_code == 200
    assert clip.headers["content-type"] == "audio/wav"
    assert clip.content == b"RIFFrendered"


def test_a_line_with_no_clip_is_an_ordinary_404(app):
    """The page asks for every line it sees, and one spoken at the desk has
    nothing to fetch."""
    _, _, port = app
    assert get(port, "/voice/99.wav").status_code == 404
    assert get(port, "/voice/nonsense.wav").status_code == 400


def test_a_speaker_that_cannot_render_still_speaks_here(app):
    """Every engine but Kokoro. A phone that gets no audio is worse than audio
    that came out of the wrong room."""
    service, _, port = app
    service.speech.wav = None
    get(port, "/spoken?since=0")

    service.say("Spotify is open, sir.")
    assert service.speech.said == ["Spotify is open, sir."]
    assert service.clips == {}


def test_a_page_that_stopped_polling_hands_the_voice_back(app):
    """Closing the tab cannot say goodbye, so this is a timeout - and the desk
    is silent for that long after you walk away from the page."""
    service, _, port = app
    service.speech.wav = b"RIFFrendered"
    get(port, "/spoken?since=0")
    assert service.page_attached()

    service._page_seen -= service_module.PAGE_GONE + 1
    assert not service.page_attached()
    service.say("Spotify is open, sir.")
    assert service.speech.said == ["Spotify is open, sir."]


def test_only_the_last_few_clips_are_kept(app):
    """They are seconds old by the time the browser has them, and nothing ever
    asks for an old one twice."""
    service, _, port = app
    service.speech.wav = b"RIFFrendered"
    get(port, "/spoken?since=0")
    for n in range(service_module.KEEP_CLIPS + 4):
        service.say(f"Reply number {n}.")
    assert len(service.clips) == service_module.KEEP_CLIPS
    assert max(service.clips) == service_module.KEEP_CLIPS + 4


def test_the_desk_can_be_shut_from_the_page(app):
    """Num Lock is on the desk and a phone has not got one, so the page can say
    it too - and it means the same thing there: the desk, not this phone."""
    _, microphone, port = app
    assert post(port, "/pause").json() == {"paused": True}
    assert microphone.paused
    assert get(port, "/status").json()["paused"] is True

    assert post(port, "/resume").json() == {"paused": False}
    assert not microphone.paused


def test_headphone_mode_can_be_switched_from_the_page(app):
    """A phone has no Num Lock to hold, and the setting it flips is the desk's
    anyway - what plays on the phone plays out of the phone."""
    service, _microphone, port = app
    assert post(port, "/headphones", json={"on": True}).json() == {"headphones": True}
    assert service.headphones is True
    assert get(port, "/status").json()["headphones"] is True

    assert post(port, "/headphones", json={"on": False}).json() == {"headphones": False}
    assert service.headphones is False


def test_an_empty_headphone_request_is_a_toggle(app):
    """Which is what the key does, so curl may as well say the same."""
    service, _microphone, port = app
    assert post(port, "/headphones").json() == {"headphones": True}
    assert post(port, "/headphones").json() == {"headphones": False}
    assert service.headphones is False


def test_a_headphone_request_that_makes_no_sense_is_refused(app):
    _, _, port = app
    assert post(port, "/headphones", content=b"not json").status_code == 400
    assert post(port, "/headphones", content=b"[]").status_code == 400


def test_pausing_still_works_the_way_the_cli_asks_for_it(app):
    """`jarvis pause` has always been a GET, and it stays one."""
    _, microphone, port = app
    assert get(port, "/pause").json() == {"paused": True}
    assert microphone.paused


def test_everything_the_page_calls_at_the_end_exists():
    """A page that throws on load is a page with no conversation, no status and
    no sound, and the only symptom is that nothing happens. One rename during a
    refactor took `watchDoing` out from under the call at the bottom and the
    whole script stopped at that line."""
    import re

    from jarvis.webapp import PAGE

    script = re.search(r"<script>(.*)</script>", PAGE, re.S).group(1)
    defined = set(re.findall(r"^(?:async )?function (\w+)", script, re.M))
    defined |= set(re.findall(r"^(?:const|let|var) (\w+)", script, re.M))
    called = set(re.findall(r"^(\w+)\(", script, re.M))

    # The browser brings these; everything else has to be in the file.
    provided = {"addEventListener", "registerProcessor", "setTimeout", "fetch"}

    assert called, "the page should start something"
    missing = called - defined - provided
    assert not missing, f"called but never defined: {sorted(missing)}"


def test_a_goodbye_is_not_undone_by_what_was_already_in_flight(app):
    """A page closing has long polls open behind it, and those land a moment
    later. Taken as a page still being here, the floor went back and the
    handover was announced twice in a row."""
    service, _, port = app
    said = []
    service.ui = type("Watching", (), {"note": lambda self, text: said.append(text)})()

    get(port, "/spoken?since=0")
    service.live.settle()
    assert service.live.on_the_page

    post(port, "/gone")
    assert not service.live.on_the_page

    # The tail of the page that has already gone.
    get(port, "/spoken?since=0")
    get(port, "/live?since=0")
    service.live.settle()
    assert not service.live.on_the_page, "still gone"
    assert said == [
        "Listening through the web app. This microphone is off.",
        "The web app has gone. Listening on this microphone again.",
    ]


def test_a_page_that_comes_back_after_the_grace_is_here_again(app):
    """Backgrounding says goodbye, and coming back has to be believed."""
    service, _, port = app
    get(port, "/spoken?since=0")
    post(port, "/gone")
    assert not service.live.on_the_page

    service._page_left -= service_module.GOODBYE + 1
    get(port, "/spoken?since=0")
    assert service.live.on_the_page


def test_starting_up_announces_nothing(app):
    """The first settle is not a handover, it is a beginning."""
    service, _, _ = app
    said = []
    service.ui = type("Watching", (), {"note": lambda self, text: said.append(text)})()
    service.live._floor = None
    service.live.settle()
    assert said == []


def test_the_clip_is_ready_before_the_line_is_announced(app):
    """The page asks for the audio the instant it hears there is a reply. With
    the rendering afterwards it fetched a clip that did not exist yet, took the
    404 for "spoken at the desk" and gave up - a reply with no sound, and
    nothing in any log to say so. Test sound worked, because by then it was
    there."""
    service, _, port = app
    get(port, "/spoken?since=0")
    service.speech.wav = b"RIFFrendered"

    published = []
    rendering = service.speech.render
    service.speech.render = lambda text: (published.append(service.spoken.cursor), rendering(text))[
        1
    ]

    service.say("Spotify is open, sir.")
    assert published == [0], "the line was announced before its audio existed"
    assert service.clip(service.spoken.cursor) == b"RIFFrendered"


def test_a_clip_still_being_made_is_waited_for(app):
    """Announcing and storing are two statements and a page on loopback gets
    between them. The ordering is the fix; this is the guard on it."""
    service, _, _ = app
    service.spoken.add("Spotify is open, sir.", always=True)
    wanted = service.spoken.cursor

    threading.Timer(0.2, lambda: service._keep(wanted, b"RIFFlate")).start()
    assert service.clip(wanted, wait=3) == b"RIFFlate"


def test_only_the_newest_line_is_worth_waiting_for(app):
    """Anything older either has a clip or was spoken at the desk and never
    will have one, and the page asks about every line it sees."""
    service, _, _ = app
    for _ in range(3):
        service.spoken.add("Something said.", always=True)

    started = time.monotonic()
    assert service.clip(1, wait=5) is None
    assert time.monotonic() - started < 1, "an old line is answered at once"
