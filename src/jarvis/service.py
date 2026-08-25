"""The voice service.

One process owns the microphone, Whisper and the speakers, and exposes them over
loopback HTTP. The CLI and the MCP server are thin clients of it.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .config import Config
from .echo import EchoGuard
from .hotkey import HotkeyListener
from .microphone import Microphone
from .stt import Transcriber, build_transcriber
from .transcript import Transcript
from .tts import SpeechEngine, build_speaker

logger = logging.getLogger("jarvis.service")

# A client vanishing mid request. Normal here: /heard blocks for up to a minute,
# and MCP servers get killed with a request in flight.
GONE_AWAY = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)


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
        self._echo = EchoGuard()
        self._speaking = threading.Lock()
        self._speaking_count = 0
        self._running = threading.Event()
        self._listener: threading.Thread | None = None
        self._hotkey: HotkeyListener | None = None

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
        self._start_hotkey()

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
            utterance = self.transcript.add(heard)
            logger.info("[%d] %s", utterance.id, heard)

    def _start_hotkey(self) -> None:
        """Register the configured key to toggle transcription."""
        self._hotkey = HotkeyListener(
            on_pause=self.pause,
            on_resume=self.resume,
            key=self.config.service.hotkey,
        )
        self._hotkey.start()

    def pause(self) -> bool:
        """Pause transcription. Returns True if was not already paused."""
        if self.transcript.paused:
            return False
        self.transcript.pause()
        return True

    def resume(self) -> None:
        """Resume transcription."""
        self.transcript.resume()

    # ------------------------------------------------------------------ speak

    def say(self, text: str) -> None:
        """Queue speech and return, so a long reply does not block the agent."""
        text = text.strip()
        if not text or self.speech is None:
            return
        logger.info("say: %s", text)
        self._echo.remember(text)

        if self.config.audio.listen_while_speaking or self.microphone is None:
            self.speech.say(text)
            return

        with self._speaking:
            self.microphone.mute()
            self._speaking_count += 1
        self.speech.say(text)
        threading.Thread(target=self._unmute_when_done, name="jarvis-unmute", daemon=True).start()

    def _unmute_when_done(self) -> None:
        """Release the microphone once everything queued has been spoken.

        Counted rather than flagged - two replies can overlap, and the first to
        finish must not unmute while the second is still playing.
        """
        assert self.speech is not None
        self.speech.wait(timeout=180)
        with self._speaking:
            self._speaking_count -= 1
            if self._speaking_count > 0:
                return
        if self.microphone is not None:
            self.microphone.unmute()

    def status(self) -> dict:
        return {
            "listening": self._running.is_set(),
            "cursor": self.transcript.cursor,
            "stt": self.config.stt.backend,
            "tts": self.config.tts.engine,
            "paused": self.transcript.paused,
        }

    def stop(self) -> None:
        self._running.clear()
        if self._listener is not None:
            self._listener.join(timeout=5)
            self._listener = None
        if self._hotkey is not None:
            self._hotkey.stop()
            self._hotkey = None
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
        elif url.path == "/pause":
            self.service.pause()
            self._json(200, {"paused": True})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/say":
            self._do_say()
        elif path == "/resume":
            self.service.resume()
            self._json(200, {"paused": False})
        else:
            self._json(404, {"error": "not found"})

    def _do_say(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            text = str(payload["text"])
        except (ValueError, KeyError, TypeError):
            self._json(400, {"error": "expected a JSON body with a 'text' field"})
            return
        self.service.say(text)
        self._json(200, {"spoken": text})

    def _heard(self, query: dict) -> None:
        settings = self.service.config.service
        try:
            since = int(query.get("since", ["0"])[0])
            wait = float(query.get("wait", ["0"])[0])
        except ValueError:
            self._json(400, {"error": "since and wait must be numbers"})
            return

        wait = max(0.0, min(wait, settings.max_wait_seconds))
        transcript = self.service.transcript

        delivered = transcript.since(since)
        if not delivered and wait:
            delivered = transcript.wait_for(since, timeout=wait)

        # Only advance past what actually went out - reporting the
        # transcript's own cursor swallows anything that arrived during the wait.
        self._json(
            200,
            {
                "heard": [item.as_dict() for item in delivered],
                "cursor": delivered[-1].id if delivered else since,
            },
        )

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except GONE_AWAY:
            # Routine: /heard is held open for a minute at a time.
            logger.debug("Client hung up before the reply was written.")


class _Server(ThreadingHTTPServer):
    """Threaded, so a blocked /heard does not stop a /say arriving."""

    daemon_threads = True

    def handle_error(self, request, client_address) -> None:
        """Swallow disconnects rather than printing a stack trace for each."""
        error = sys.exc_info()[1]
        if isinstance(error, GONE_AWAY):
            logger.debug("Client %s went away mid request.", client_address)
            return
        logger.exception("Error handling a request from %s", client_address)


def build_server(service: VoiceService) -> ThreadingHTTPServer:
    """HTTP front end for a service."""
    handler = type("Handler", (_Handler,), {"service": service})
    address = (service.config.service.host, service.config.service.port)
    return _Server(address, handler)
