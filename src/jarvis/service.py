"""The voice service.

One process owns the microphone, Whisper and the speakers, and exposes them
over loopback HTTP. The CLI and the MCP server are thin clients, so `jarvis say`
from an agent's terminal mutes the same microphone that is doing the listening -
which is the whole reason this is a daemon and not three separate programs.

No LLM is involved. The agent on the other end is the brain.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .config import Config
from .echo import EchoGuard
from .microphone import Microphone
from .stt import Transcriber, build_transcriber
from .transcript import Transcript
from .tts import SpeechEngine, build_speaker
from .wake import WakeMatcher

logger = logging.getLogger("jarvis.service")


class VoiceService:
    """Captures speech into a transcript, and speaks on request."""

    def __init__(
        self,
        config: Config,
        microphone: Microphone | None = None,
        transcriber: Transcriber | None = None,
        speech: SpeechEngine | None = None,
        transcript: Transcript | None = None,
    ) -> None:
        self.config = config
        self.microphone = microphone
        self.transcriber = transcriber
        self.speech = speech
        self.transcript = transcript or Transcript(config.log_dir / config.service.transcript_file)
        self._wake = WakeMatcher(config.wake.words, config.wake.fuzzy, config.wake.fuzzy_threshold)
        self._echo = EchoGuard()
        self._running = threading.Event()
        self._listener: threading.Thread | None = None

    # ----------------------------------------------------------------- listen

    def start(self) -> None:
        """Open the microphone and begin transcribing into the transcript."""
        if self.microphone is None:
            self.microphone = Microphone(self.config.audio)
        if self.transcriber is None:
            self.transcriber = build_transcriber(self.config.stt)
        if self.speech is None:
            self.speech = SpeechEngine(lambda: build_speaker(self.config.tts))

        self.microphone.start()
        self._running.set()
        self._listener = threading.Thread(target=self._listen, name="jarvis-listen", daemon=True)
        self._listener.start()

    def _listen(self) -> None:
        assert self.microphone is not None and self.transcriber is not None
        while self._running.is_set():
            audio = self.microphone.listen(timeout=0.5)
            if audio is None:
                continue
            heard = self.transcriber.transcribe(audio)
            if not heard:
                continue
            if self._echo.is_echo(heard):
                logger.debug("Ignored JARVIS hearing itself: %s", heard)
                continue
            addressed, command = self._classify(heard)
            utterance = self.transcript.add(heard, addressed=addressed, command=command)
            if addressed:
                logger.info("[%d] %s", utterance.id, command)
            else:
                logger.info("[%d] (not addressed) %s", utterance.id, heard)

    def _classify(self, heard: str) -> tuple[bool, str]:
        """Whether this was aimed at JARVIS, and the instruction with its name removed.

        Everything is recorded either way. Dropping unaddressed speech loses the
        second half of any sentence that got split - say "jarvis", hesitate, and
        the phrase detector ends the phrase before the actual request arrives.
        """
        addressed, remainder = self._wake.split(heard)
        if not addressed:
            # Not required means every utterance counts as addressed to us.
            return (not self.config.wake.required), heard
        return True, remainder or heard

    # ------------------------------------------------------------------ speak

    def say(self, text: str) -> None:
        """Speak, with the microphone muted so it is not transcribed back."""
        text = text.strip()
        if not text or self.speech is None:
            return
        logger.info("say: %s", text)
        self._echo.remember(text)
        if self.microphone is not None:
            self.microphone.mute()
        try:
            self.speech.say(text)
            self.speech.wait(timeout=180)
        finally:
            if self.microphone is not None:
                self.microphone.unmute()

    def status(self) -> dict:
        return {
            "listening": self._running.is_set(),
            "cursor": self.transcript.cursor,
            "stt": self.config.stt.backend,
            "tts": self.config.tts.engine,
            "wake_word_required": self.config.wake.required,
            "wake_words": list(self.config.wake.words),
        }

    def stop(self) -> None:
        self._running.clear()
        if self._listener is not None:
            self._listener.join(timeout=5)
            self._listener = None
        if self.microphone is not None:
            self.microphone.stop()
        if self.speech is not None:
            self.speech.close()


class _Handler(BaseHTTPRequestHandler):
    """Loopback only. No auth, so do not bind this to anything routable."""

    service: VoiceService
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        logger.debug("http: " + fmt, *args)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if url.path == "/status":
            self._json(200, self.service.status())
        elif url.path == "/heard":
            self._heard(query)
        else:
            self._json(404, {"error": "not found"})

    def _heard(self, query: dict) -> None:
        settings = self.service.config.service
        try:
            since = int(query.get("since", ["0"])[0])
            wait = float(query.get("wait", ["0"])[0])
            addressed_only = query.get("addressed", ["0"])[0] not in {"0", "false", ""}
            settle = float(query.get("settle", [str(settings.settle_seconds)])[0])
        except ValueError:
            self._json(400, {"error": "since, wait and settle must be numbers"})
            return

        wait = max(0.0, min(wait, settings.max_wait_seconds))
        settle = max(0.0, min(settle, 10.0))
        transcript = self.service.transcript

        items = [i for i in transcript.since(since) if i.addressed or not addressed_only]
        if not items and wait:
            items = transcript.wait_for(since, timeout=wait, addressed_only=addressed_only)

        # Give a hesitating speaker a moment to finish. "Jarvis" then a pause is
        # two phrases, and returning after the first one acts on nothing.
        if items and settle:
            time.sleep(settle)

        delivered = transcript.since(since) if items else []
        # Only advance past what actually went out. Reporting the transcript's
        # own cursor on a timeout silently swallows everything unaddressed that
        # arrived during the wait - which is exactly the context the caller
        # needs to make sense of the next instruction.
        self._json(
            200,
            {
                "heard": [item.as_dict() for item in delivered],
                "cursor": delivered[-1].id if delivered else since,
            },
        )

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/say":
            self._json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            text = str(payload["text"])
        except (ValueError, KeyError, TypeError):
            self._json(400, {"error": "expected a JSON body with a 'text' field"})
            return
        self.service.say(text)
        self._json(200, {"spoken": text})

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def build_server(service: VoiceService) -> ThreadingHTTPServer:
    """HTTP front end for a service. Threaded, so a blocked /heard does not
    stop a /say arriving."""
    handler = type("Handler", (_Handler,), {"service": service})
    address = (service.config.service.host, service.config.service.port)
    server = ThreadingHTTPServer(address, handler)
    server.daemon_threads = True
    return server
