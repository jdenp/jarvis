"""The page on the LAN, against a real socket and a fake service.

Started on port 0 and over plain HTTP, so nothing here needs a certificate or a
fixed port. One test does generate a certificate, since the whole point of it is
that a phone will not open a microphone without one.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from dataclasses import replace

import pytest

from jarvis.config import Config, WebConfig
from jarvis.transcript import Transcript
from jarvis.web import COOKIE, MAX_UPLOAD, Replies, build_server, ensure_certificate

TOKEN = "test-token"


class FakeTranscriber:
    def __init__(self, answer: str | None = "hello from the phone") -> None:
        self.answer = answer
        self.uploads: list[bytes] = []

    def transcribe_file(self, data: bytes) -> str | None:
        self.uploads.append(data)
        return self.answer


class FakeService:
    def __init__(self, config: Config, transcriber=None) -> None:
        self.config = config
        self.transcript = Transcript()
        self.transcriber = transcriber if transcriber is not None else FakeTranscriber()
        self.on_say: list = []
        self.said: list[str] = []

    def say(self, text: str) -> None:
        self.said.append(text)
        for listener in self.on_say:
            listener(text)


class Rig:
    """A running page, and the least client that can talk to it."""

    def __init__(self, service, web) -> None:
        self.service = service
        self.web = web

    def request(self, method: str, path: str, body=None, token=TOKEN, kind="application/json"):
        connection = http.client.HTTPConnection("127.0.0.1", self.web.port, timeout=10)
        headers = {}
        if token is not None:
            headers["Cookie"] = f"{COOKIE}={token}"
        if body is not None:
            headers["Content-Type"] = kind
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        return response, payload, connection

    def get(self, path: str, token=TOKEN):
        response, payload, connection = self.request("GET", path, token=token)
        connection.close()
        return response, payload

    def post(self, path: str, body, token=TOKEN, kind="application/json"):
        response, payload, connection = self.request("POST", path, body, token, kind)
        connection.close()
        return response, payload

    def json(self, path: str, token=TOKEN) -> dict:
        _response, payload = self.get(path, token)
        return json.loads(payload)


@pytest.fixture
def rig(tmp_path):
    config = replace(
        Config(),
        web=WebConfig(enabled=True, host="127.0.0.1", port=0, token=TOKEN, https=False),
    )
    service = FakeService(config)
    web = build_server(service, config)
    web.serve_in_background()
    try:
        yield Rig(service, web)
    finally:
        web.stop()


# ------------------------------------------------------------------------ auth


def test_a_request_without_a_token_is_refused(rig):
    """The service API is loopback and authless; this one is routable."""
    response, _ = rig.get("/state", token=None)
    assert response.status == 403


def test_a_wrong_token_is_refused(rig):
    response, _ = rig.get("/state", token="not-the-token")
    assert response.status == 403


def test_health_needs_no_token(rig):
    """So the service can be checked without handing the link around."""
    response, payload = rig.get("/health", token=None)
    assert response.status == 200
    assert json.loads(payload)["ok"] is True


def test_the_token_may_arrive_in_the_query(rig):
    """The first load has no cookie yet, so the link has to carry it."""
    response, _ = rig.get(f"/?t={TOKEN}", token=None)
    assert response.status == 200


# ------------------------------------------------------------------------ page


def test_the_page_is_served_with_the_token_as_a_cookie(rig):
    """Set once, so EventSource and fetch carry it - neither can set a header."""
    response, payload = rig.get(f"/?t={TOKEN}", token=None)
    assert b"<title>JARVIS</title>" in payload
    assert f"{COOKIE}={TOKEN}" in response.getheader("Set-Cookie", "")


def test_an_unknown_path_is_a_404(rig):
    response, _ = rig.get("/wp-login.php")
    assert response.status == 404


# ----------------------------------------------------------------------- input


def test_typing_reaches_the_transcript(rig):
    response, payload = rig.post("/heard", json.dumps({"text": "turn on the lights"}))
    assert response.status == 200
    assert json.loads(payload)["text"] == "turn on the lights"
    assert [item.text for item in rig.service.transcript.since(0)] == ["turn on the lights"]


def test_empty_text_is_not_recorded(rig):
    response, _ = rig.post("/heard", json.dumps({"text": "   "}))
    assert response.status == 400
    assert rig.service.transcript.since(0) == []


def test_a_body_without_text_is_a_400(rig):
    response, _ = rig.post("/heard", json.dumps({"words": "wrong field"}))
    assert response.status == 400


def test_saying_something_from_the_phone_reaches_the_service(rig):
    """Typing with 'also on desktop' ticked, so the room hears it too."""
    response, _ = rig.post("/say", json.dumps({"text": "Right away, sir."}))
    assert response.status == 200
    assert rig.service.said == ["Right away, sir."]


# ------------------------------------------------------------------- recording


def test_a_recording_is_transcribed_and_recorded(rig):
    response, payload = rig.post("/audio", b"pretend this is webm", kind="audio/webm")
    assert response.status == 200
    assert json.loads(payload)["heard"]["text"] == "hello from the phone"
    assert rig.service.transcriber.uploads == [b"pretend this is webm"]


def test_a_recording_of_nothing_is_not_recorded(rig):
    """Whisper on a held button that caught only room noise. Reported, not stored,
    so the phone can say so rather than showing an empty bubble."""
    rig.service.transcriber.answer = None
    response, payload = rig.post("/audio", b"only room noise", kind="audio/webm")
    assert response.status == 200
    assert json.loads(payload)["heard"] is None
    assert rig.service.transcript.since(0) == []


def test_a_backend_that_cannot_take_a_recording_says_so(tmp_path):
    """GoogleSTT has no file path, and the page needs to know to hide the button
    rather than failing on every press."""

    class NoFiles:
        def transcribe(self, audio):  # pragma: no cover - never called here
            return None

    config = replace(Config(), web=WebConfig(host="127.0.0.1", port=0, token=TOKEN, https=False))
    service = FakeService(config, transcriber=NoFiles())
    web = build_server(service, config)
    web.serve_in_background()
    try:
        rig = Rig(service, web)
        response, _ = rig.post("/audio", b"anything", kind="audio/webm")
        assert response.status == 501
    finally:
        web.stop()


def test_an_oversized_upload_is_refused_with_a_status_not_a_reset(rig):
    """Refusing before reading the body leaves the phone writing into a closed
    socket, which it reports as a network failure rather than the reason."""
    response, payload = rig.post("/audio", b"x" * (MAX_UPLOAD + 1024), kind="audio/webm")
    assert response.status == 413
    assert json.loads(payload)["sent"] == MAX_UPLOAD + 1024


# ---------------------------------------------------------------------- stream


def read_events(rig, wanted: int, timeout: float = 5.0) -> list[str]:
    """Collect complete `event:`/`data:` pairs from the stream."""
    lines: list[str] = []
    connection = http.client.HTTPConnection("127.0.0.1", rig.web.port, timeout=timeout)
    connection.request("GET", "/events?heard=0&said=0", headers={"Cookie": f"{COOKIE}={TOKEN}"})
    response = connection.getresponse()

    def collect() -> None:
        while len(lines) < wanted:
            line = response.readline()
            if not line:
                return
            text = line.decode("utf-8").strip()
            if text.startswith(("event:", "data:")):
                lines.append(text)

    reader = threading.Thread(target=collect, daemon=True)
    reader.start()
    return lines, connection, reader


def test_the_stream_carries_both_halves_of_the_conversation(rig):
    lines, connection, reader = read_events(rig, wanted=4)
    time.sleep(0.4)  # let the stream open before there is anything to miss

    rig.service.transcript.add("what is the disk usage")
    rig.service.say("Sixty seven percent, sir.")

    reader.join(timeout=5)
    connection.close()

    joined = "\n".join(lines)
    assert "event: heard" in joined
    assert "what is the disk usage" in joined
    assert "event: said" in joined
    assert "Sixty seven percent, sir." in joined


def test_the_stream_needs_a_token_too(rig):
    response, _ = rig.get("/events", token=None)
    assert response.status == 403


def test_state_hands_over_everything_needed_to_catch_up(rig):
    rig.service.transcript.add("first")
    rig.service.say("second")
    state = rig.json("/state")
    assert [item["text"] for item in state["heard"]] == ["first"]
    assert [item["text"] for item in state["said"]] == ["second"]
    assert state["heard_cursor"] == 1
    assert state["said_cursor"] == 1
    assert state["can_record"] is True
    assert state["max_upload"] == MAX_UPLOAD


# --------------------------------------------------------------------- replies


def test_replies_hand_out_increasing_ids():
    replies = Replies()
    assert [replies.add(text).id for text in ("one", "two", "three")] == [1, 2, 3]
    assert replies.cursor == 3
    assert [item.text for item in replies.since(1)] == ["two", "three"]


def test_replies_are_bounded_but_ids_carry_on():
    """A phone that has been asleep asks from an old cursor, and must not be sent
    a hundred replies it will never read."""
    replies = Replies(keep=3)
    for index in range(10):
        replies.add(f"reply {index}")
    assert len(replies.since(0)) == 3
    assert replies.cursor == 10
    assert replies.since(9)[0].text == "reply 9"


# ----------------------------------------------------------------- certificate


def test_a_certificate_is_generated_once_and_reused(tmp_path):
    certificate = tmp_path / "web-cert.pem"
    key = tmp_path / "web-key.pem"

    assert ensure_certificate(certificate, key) is True
    assert certificate.is_file() and key.is_file()
    assert certificate.read_bytes().startswith(b"-----BEGIN CERTIFICATE-----")

    stamp = certificate.read_bytes()
    assert ensure_certificate(certificate, key) is True
    assert certificate.read_bytes() == stamp, "an existing certificate is left alone"


def test_the_certificate_covers_the_address_a_phone_would_use(tmp_path):
    """A certificate for localhost is no use: the phone connects by IP."""
    from cryptography import x509

    certificate = tmp_path / "cert.pem"
    ensure_certificate(certificate, tmp_path / "key.pem")
    loaded = x509.load_pem_x509_certificate(certificate.read_bytes())
    names = loaded.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    addresses = {str(address) for address in names.get_values_for_type(x509.IPAddress)}
    assert "127.0.0.1" in addresses
    assert "localhost" in names.get_values_for_type(x509.DNSName)


# ------------------------------------------------------------------------ link


def test_the_link_carries_the_token_and_prefers_a_routable_address(rig):
    links = rig.web.links()
    assert links
    assert all(f"t={TOKEN}" in link for link in links)
    assert all(str(rig.web.port) in link for link in links)


def test_a_generated_token_is_not_predictable(tmp_path):
    config = replace(Config(), web=WebConfig(host="127.0.0.1", port=0, token="", https=False))
    first = build_server(FakeService(config), config)
    second = build_server(FakeService(config), config)
    try:
        assert first.token != second.token
        assert len(first.token) >= 16
    finally:
        first.stop()
        second.stop()
