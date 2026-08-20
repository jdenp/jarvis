"""A page on the LAN you can talk to from a phone.

Runs inside `jarvis serve` rather than beside it, because it needs the transcript
and the Whisper model that are already there - a second process would mean a
second copy of the model in VRAM.

Three things make this different from the service API in service.py, which is
loopback and deliberately authless:

*Routable, so a token is required.* Every request carries it, in the query string
on the first load and in a cookie after that.

*HTTPS, because a browser will not open a microphone without a secure context.* A
self signed certificate is generated on first run. Phones warn about it once and
then remember. Without a certificate the page still works for typing.

*The reply is spoken by the phone, not sent to it.* SAPI renders to the desktop's
speakers, and streaming audio back would mean encoding, buffering and drift. The
page has the text already, so it hands it to the browser's own synthesiser -
which also keeps the promise that no audio leaves the machine.
"""

from __future__ import annotations

import json
import logging
import secrets
import socket
import ssl
import sys
import threading
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import Config

logger = logging.getLogger("jarvis.web")

COOKIE = "jarvis_web"
MAX_UPLOAD = 8 * 1024 * 1024  # a held button, not a podcast
# Refusing before reading leaves the sender writing into a closed socket, which it
# reports as a network failure rather than the status. So an oversized body is read
# and discarded first - up to a point, past which the connection is simply dropped.
MAX_DRAIN = 4 * MAX_UPLOAD
DRAIN_CHUNK = 64 * 1024
KEEPALIVE_SECONDS = 15.0
POLL_SECONDS = 0.25
GONE_AWAY = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)
PAGE = Path(__file__).parent / "webui" / "index.html"


@dataclass(frozen=True)
class Reply:
    """Something JARVIS said, so the page can show and speak it."""

    id: int
    text: str
    at: str


class Replies:
    """Recent replies, with ids so a reconnecting phone can catch up.

    The transcript covers what was heard; this is the other half of the
    conversation, which nothing else needed until there was a screen to show it on.
    """

    def __init__(self, keep: int = 100) -> None:
        self._items: list[Reply] = []
        self._keep = keep
        self._next_id = 1
        self._lock = threading.Lock()

    def add(self, text: str) -> Reply:
        with self._lock:
            reply = Reply(
                id=self._next_id,
                text=text,
                at=datetime.now(UTC).isoformat(timespec="seconds"),
            )
            self._next_id += 1
            self._items.append(reply)
            del self._items[: -self._keep]
            return reply

    @property
    def cursor(self) -> int:
        with self._lock:
            return self._next_id - 1

    def since(self, cursor: int) -> list[Reply]:
        with self._lock:
            return [item for item in self._items if item.id > cursor]


def local_addresses() -> list[str]:
    """Every IPv4 address this machine answers on, for the certificate and the log."""
    found = {"127.0.0.1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:  # pragma: no cover - depends on name resolution
        logger.debug("Could not resolve this host's own addresses.", exc_info=True)
    return sorted(found)


def ensure_certificate(certificate: Path, key: Path) -> bool:
    """Generate a self signed certificate if there is not one already.

    Returns whether HTTPS can be served. Named for every address this machine has,
    because the phone connects by IP and a certificate for `localhost` would not
    cover it.
    """
    if certificate.is_file() and key.is_file():
        return True
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError:
        logger.warning(
            "No certificate and cryptography is not installed, so the page will be "
            "served over plain HTTP. Typing works; the microphone will not, because "
            "browsers refuse it outside a secure context. Fix: uv sync --extra web"
        )
        return False

    from datetime import timedelta

    addresses = local_addresses()
    names: list[x509.GeneralName] = [x509.DNSName("localhost"), x509.DNSName(socket.gethostname())]
    for address in addresses:
        import ipaddress

        names.append(x509.IPAddress(ipaddress.ip_address(address)))

    key_object = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "JARVIS on this machine")])
    now = datetime.now(UTC)
    certificate_object = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key_object.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=825))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key_object, hashes.SHA256())
    )

    certificate.parent.mkdir(parents=True, exist_ok=True)
    certificate.write_bytes(certificate_object.public_bytes(serialization.Encoding.PEM))
    key.write_bytes(
        key_object.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    logger.info("Wrote a self signed certificate for %s to %s", ", ".join(addresses), certificate)
    return True


class _Handler(BaseHTTPRequestHandler):
    """Routable, so every route checks the token first."""

    config: Config
    service: object
    replies: Replies
    token: str
    secure: bool
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        logger.debug("web: " + fmt, *args)

    # ------------------------------------------------------------------- auth

    def _offered_token(self, query: dict) -> str:
        if supplied := query.get("t", [""])[0]:
            return supplied
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        return cookie[COOKIE].value if COOKIE in cookie else ""

    def _authorised(self, query: dict) -> bool:
        return secrets.compare_digest(self._offered_token(query), self.token)

    # ------------------------------------------------------------------ verbs

    def do_GET(self) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path == "/health":
            self._json(200, {"ok": True})
            return
        if not self._authorised(query):
            self._json(403, {"error": "a token is required"})
            return
        if url.path == "/":
            self._page(query)
        elif url.path == "/events":
            self._events(query)
        elif url.path == "/state":
            self._json(200, self._state())
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if not self._authorised(query):
            self._json(403, {"error": "a token is required"})
            return
        if url.path == "/heard":
            self._typed()
        elif url.path == "/audio":
            self._recorded()
        elif url.path == "/say":
            self._say()
        else:
            self._json(404, {"error": "not found"})

    # ------------------------------------------------------------------ pages

    def _page(self, query: dict) -> None:
        try:
            body = PAGE.read_bytes()
        except OSError:
            logger.exception("Could not read %s", PAGE)
            self._json(500, {"error": "the page is missing from this install"})
            return
        # Set on first load so EventSource and fetch carry it without the token
        # staying in the address bar, where it would end up in history.
        cookie = f"{COOKIE}={self.token}; Path=/; Max-Age=31536000; SameSite=Strict"
        if self.secure:
            cookie += "; Secure"
        self._respond(200, "text/html; charset=utf-8", body, extra={"Set-Cookie": cookie})

    def _state(self) -> dict:
        transcript = self.service.transcript  # type: ignore[attr-defined]
        return {
            "heard": [item.as_dict() for item in transcript.since(0)],
            "said": [asdict(item) for item in self.replies.since(0)],
            "heard_cursor": transcript.cursor,
            "said_cursor": self.replies.cursor,
            "can_record": hasattr(self.service, "transcriber"),
            "secure": self.secure,
            "max_upload": MAX_UPLOAD,
        }

    # ----------------------------------------------------------------- stream

    def _events(self, query: dict) -> None:
        """Server sent events, polled rather than pushed.

        Two sources with independent conditions, and a quarter of a second of
        latency on a phone is not worth the machinery to wait on both at once.
        """
        try:
            heard_at = int(query.get("heard", ["0"])[0])
            said_at = int(query.get("said", ["0"])[0])
        except ValueError:
            self._json(400, {"error": "cursors must be numbers"})
            return

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            # No length is known in advance, so the response ends when the
            # connection does. EventSource reconnects on its own.
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
        except GONE_AWAY:
            return

        transcript = self.service.transcript  # type: ignore[attr-defined]
        deadline = time.monotonic() + self.config.web.stream_seconds
        last_word = time.monotonic()
        try:
            while time.monotonic() < deadline:
                sent = False
                for item in transcript.since(heard_at):
                    heard_at = item.id
                    self._event("heard", item.as_dict())
                    sent = True
                for reply in self.replies.since(said_at):
                    said_at = reply.id
                    self._event("said", asdict(reply))
                    sent = True
                if sent:
                    last_word = time.monotonic()
                elif time.monotonic() - last_word > KEEPALIVE_SECONDS:
                    # Comment frames stop a phone or a router deciding the
                    # connection is idle and dropping it.
                    self.wfile.write(b": still here\n\n")
                    self.wfile.flush()
                    last_word = time.monotonic()
                time.sleep(POLL_SECONDS)
        except GONE_AWAY:
            logger.debug("A phone closed the transcript stream.")

    def _event(self, name: str, payload: dict) -> None:
        body = f"event: {name}\ndata: {json.dumps(payload)}\n\n"
        self.wfile.write(body.encode("utf-8"))
        self.wfile.flush()

    # ------------------------------------------------------------------ input

    def _body(self) -> bytes | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(400, {"error": "a length is required"})
            return None
        if length > MAX_UPLOAD:
            self._refuse_body(length)
            return None
        return self.rfile.read(length)

    def _refuse_body(self, length: int) -> None:
        """Take the body only so the refusal can be read, then say no."""
        self.close_connection = True
        if length <= MAX_DRAIN:
            left = length
            while left > 0:
                chunk = self.rfile.read(min(DRAIN_CHUNK, left))
                if not chunk:
                    break
                left -= len(chunk)
        else:
            logger.warning("Dropped a connection declaring %d bytes.", length)
            return
        self._json(413, {"error": f"at most {MAX_UPLOAD} bytes", "sent": length})

    def _typed(self) -> None:
        body = self._body()
        if body is None:
            return
        try:
            text = str(json.loads(body or b"{}")["text"]).strip()
        except (ValueError, KeyError, TypeError):
            self._json(400, {"error": "expected a JSON body with a 'text' field"})
            return
        if not text:
            self._json(400, {"error": "nothing to record"})
            return
        utterance = self.service.transcript.add(text)  # type: ignore[attr-defined]
        logger.info("[%d] %s (typed)", utterance.id, text)
        self._json(200, utterance.as_dict())

    def _recorded(self) -> None:
        body = self._body()
        if body is None:
            return
        transcriber = getattr(self.service, "transcriber", None)
        transcribe = getattr(transcriber, "transcribe_file", None)
        if transcribe is None:
            self._json(501, {"error": "this speech backend cannot take a recording"})
            return

        started = time.monotonic()
        text = transcribe(body)
        took = time.monotonic() - started
        if not text:
            logger.info("A recording of %d bytes came back empty after %.2fs.", len(body), took)
            self._json(200, {"heard": None, "seconds": round(took, 2)})
            return
        utterance = self.service.transcript.add(text)  # type: ignore[attr-defined]
        logger.info("[%d] %s (phone, %.2fs)", utterance.id, text, took)
        self._json(200, {"heard": utterance.as_dict(), "seconds": round(took, 2)})

    def _say(self) -> None:
        """Speak from the phone, out of the desktop's speakers."""
        body = self._body()
        if body is None:
            return
        try:
            text = str(json.loads(body or b"{}")["text"]).strip()
        except (ValueError, KeyError, TypeError):
            self._json(400, {"error": "expected a JSON body with a 'text' field"})
            return
        if not text:
            self._json(400, {"error": "nothing to say"})
            return
        self.service.say(text)  # type: ignore[attr-defined]
        self._json(200, {"spoken": text})

    # --------------------------------------------------------------- plumbing

    def _json(self, status: int, payload: dict) -> None:
        self._respond(status, "application/json", json.dumps(payload).encode("utf-8"))

    def _respond(self, status: int, kind: str, body: bytes, extra: dict | None = None) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)
        except GONE_AWAY:
            logger.debug("A phone hung up before the reply was written.")


class _Server(ThreadingHTTPServer):
    """Threaded: every connected phone holds an event stream open."""

    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, GONE_AWAY | ssl.SSLError):
            logger.debug("Phone %s went away mid request.", client_address)
            return
        logger.exception("Error handling a request from %s", client_address)


@dataclass
class WebServer:
    """A running page, and how to reach it."""

    server: ThreadingHTTPServer
    token: str
    secure: bool
    port: int
    serving: bool = False

    def links(self) -> list[str]:
        """One URL per address the phone might use, token included."""
        scheme = "https" if self.secure else "http"
        return [
            f"{scheme}://{address}:{self.port}/?t={self.token}"
            for address in local_addresses()
            if address != "127.0.0.1"
        ] or [f"{scheme}://127.0.0.1:{self.port}/?t={self.token}"]

    def serve_in_background(self) -> threading.Thread:
        self.serving = True
        thread = threading.Thread(target=self.server.serve_forever, name="jarvis-web", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        """Release the port, whether or not it was ever served.

        shutdown() waits on the serve loop to acknowledge, so calling it on a
        server that was only bound blocks for good.
        """
        if self.serving:
            self.server.shutdown()
            self.serving = False
        self.server.server_close()


def build_server(service, config: Config | None = None) -> WebServer:
    """Bind the page to the LAN and wire it to a running service."""
    config = config or service.config
    settings = config.web

    token = settings.token.strip() or secrets.token_urlsafe(16)
    replies = Replies()
    service.on_say.append(lambda text: replies.add(text))

    secure = False
    if settings.https:
        certificate = config.config_dir / settings.certificate_file
        key = config.config_dir / settings.private_key_file
        secure = ensure_certificate(certificate, key)

    handler = type(
        "Handler",
        (_Handler,),
        {
            "config": config,
            "service": service,
            "replies": replies,
            "token": token,
            "secure": secure,
        },
    )
    server = _Server((settings.host, settings.port), handler)

    if secure:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(
            config.config_dir / settings.certificate_file,
            config.config_dir / settings.private_key_file,
        )
        server.socket = context.wrap_socket(server.socket, server_side=True)

    # Read the port back rather than trusting the config, so port 0 works.
    return WebServer(server=server, token=token, secure=secure, port=server.server_address[1])
