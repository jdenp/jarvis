"""The voice service.

One process owns the microphone, Whisper and the speakers, and exposes them over
loopback HTTP. The CLI is a thin client of it.

With `service.start_webapp` it serves a page as well, so that a phone on the
same tailnet can be the microphone. That audio is not a second pipeline: it is a
second source feeding the one phrase splitter, so everything written about the
room applies to it - see RemoteStream in microphone.py.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import ui as terminal
from .config import Config
from .echo import EchoGuard
from .hotkey import HotkeyListener
from .microphone import Microphone, RemoteStream
from .stt import Transcriber, build_transcriber
from .transcript import ToolCall, Transcript
from .tts import SpeechEngine, build_speaker

logger = logging.getLogger("jarvis.service")

# A client vanishing mid request. Normal here: /heard blocks for up to a minute,
# so a caller that gives up in the meantime is the ordinary case.
GONE_AWAY = (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)

# How long after a page last polled it still counts as open. Long, because a
# page that is really closing says so - this only covers one that vanished
# without saying it, and the common reason a phone goes quiet is a screen that
# went off in somebody's pocket. At forty seconds every lock and unlock handed
# the floor back and took it again, announcing both ways each time.
PAGE_GONE = 600.0

# How long a goodbye is believed for. A page closing has requests already in
# flight behind it, and those land a moment later and would put the floor
# straight back - announcing the handover twice and undoing it in between.
GOODBYE = 2.0

# How long /voice waits for a clip that is still being rendered. Only the gap
# between publishing a line and storing its audio, which is one statement -
# but the page is on loopback and gets between them.
CLIP_WAIT = 2.0

# Rendered replies kept for the page to fetch. Two seconds of speech is about
# 100KB and the browser asks for one the moment it hears about it.
KEEP_CLIPS = 8


class Doing:
    """The one line the terminal draws under the conversation, for the page.

    A current value rather than a stream, so it is read with a version beside
    it: the page asks what has changed since the one it last saw and blocks
    until something has. Polling it on a timer would either lag behind what
    JARVIS is doing or cost a request a second from a phone all day.
    """

    def __init__(self) -> None:
        self._text = ""
        self._version = 0
        self._changed = threading.Condition()

    def set(self, text: str) -> None:
        """What is happening now. Called by the terminal, for everyone else."""
        with self._changed:
            if text == self._text:
                return
            self._text = text
            self._version += 1
            self._changed.notify_all()

    def read(self) -> tuple[int, str]:
        with self._changed:
            return self._version, self._text

    def wait(self, since: int, timeout: float) -> tuple[int, str]:
        """The line, once it differs from the one they have."""
        with self._changed:
            self._changed.wait_for(lambda: self._version != since, timeout)
            return self._version, self._text


class LiveHardware:
    """Which microphone is listened to, and which room the voice comes out in.

    Two of each - the desk and a browser - and exactly one of each is live. A
    page being open is the whole test: somebody holding a phone is not at the
    desk, so a desk microphone there is listening to a room nobody is in, and a
    desk speaker is talking to it.

    It exists because that decision used to be made twice and differently. The
    ears switched on whether audio was arriving this second and the voice on
    whether a page was open, so a phone that had stopped talking for four
    seconds handed the desk its microphone back while still holding the voice.
    """

    def __init__(self, desk=None, web=None, attached=None, told=None) -> None:
        self.desk = desk
        self.web = web
        self._attached = attached or (lambda: False)
        self._told = told or (lambda page: None)
        # None until the first settle, so starting up announces nothing.
        self._floor: bool | None = None

    @property
    def on_the_page(self) -> bool:
        """Whether a browser has the floor, ears and voice together."""
        return self.web is not None and self._attached()

    @property
    def mic(self):
        """The microphone being listened to. The other one is deferred."""
        return self.web if self.on_the_page else self.desk

    @property
    def ears(self) -> list:
        """Both of them, live or not, for the echo gate to shut."""
        return [one for one in (self.desk, self.web) if one is not None]

    def settle(self) -> None:
        """Give the floor to whichever one should have it.

        Cheap and idempotent, so it can be called from the listen loop rather
        than tracked as a transition somebody has to remember to fire.
        """
        page = self.on_the_page
        if self.desk is not None:
            self.desk.defer(page)
        if self.web is not None:
            self.web.defer(not page)
        if self._floor is None:
            self._floor = page
        elif page != self._floor:
            self._floor = page
            self._told(page)


class VoiceService:
    """Captures speech into a transcript, and speaks on request."""

    def __init__(
        self,
        config: Config,
        microphone: Microphone | None = None,
        transcriber: Transcriber | None = None,
        speech: SpeechEngine | None = None,
        transcript: Transcript | None = None,
        ui=None,
    ) -> None:
        self.config = config
        # Both microphones, and which of them has the floor. `microphone` and
        # `remote` below are the two halves of it under their old names.
        self.live = LiveHardware(
            desk=microphone,
            attached=lambda: self.page_attached(),
            told=lambda page: self._floor_changed(page),
        )
        # Both paths pass through here, so this is where the conversation is
        # drawn: everything heard goes through _listen and everything spoken
        # through say(). Rendering anywhere else would double up.
        self.ui = ui or terminal.Silent()
        self.transcriber = transcriber
        self.speech = speech
        self.transcript = transcript or Transcript(config.log_dir / config.service.transcript_file)
        # What JARVIS said, in memory only: the file beside it is what was heard,
        # and the web app needs somewhere to read the other half of it from.
        self.spoken = Transcript()
        # And what it did in between. Nothing here writes to it - the terminal
        # does, through `ran` below.
        self.calls = Transcript(item=ToolCall)
        self.stream: RemoteStream | None = None
        # Fed by the terminal once there is one - see cli.run_serve.
        self.doing = Doing()
        # Replies rendered for the page to play, newest last, keyed by the id of
        # the line in `spoken`. A handful: they are seconds old by the time the
        # browser has them and nothing ever asks for an old one twice.
        self.clips: OrderedDict[int, bytes] = OrderedDict()
        self._rendered = threading.Condition()
        self._page_seen = 0.0
        self._page_left = 0.0
        self._echo = EchoGuard()
        # Where listen_while_speaking starts. It moves, so it cannot live in the
        # config - the hotkey held down and the button on the page both flip it.
        self._headphones = threading.Event()
        if config.audio.listen_while_speaking:
            self._headphones.set()
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
        self._start_webapp_source()
        self._running.set()
        self._listener = threading.Thread(target=self._listen, name="jarvis-listen", daemon=True)
        self._listener.start()
        self._start_hotkey()

    def _start_webapp_source(self) -> None:
        """A second capture source for the page, delivering into the same queue.

        Sharing the queue is what makes this small: `listen` merges the two
        without knowing there are two, and the service never learns where a
        phrase came from. Its own detector, though - Silero carries state across
        buffers, and two sources through one would be scoring a mix of rooms.
        """
        if not self.config.service.start_webapp:
            return
        sink = getattr(self.microphone, "sink", None)
        if sink is None:
            logger.warning("No queue to share, so the web app cannot listen.")
            return
        self.stream = RemoteStream()
        self.live.web = Microphone(self.config.audio, source=self.stream, sink=sink)
        self.live.web.start()
        self.live.settle()

    @property
    def microphone(self):
        """The desk. `live.mic` is whichever one is actually being listened to."""
        return self.live.desk

    @microphone.setter
    def microphone(self, one) -> None:
        self.live.desk = one

    @property
    def remote(self):
        """The browser's, if the web app is switched on."""
        return self.live.web

    def _floor_changed(self, on_the_page: bool) -> None:
        """Say which microphone is live, because nothing else shows it.

        Written rather than spoken. JARVIS does not say things nobody asked
        for - see DESIGN - and a machine announcing itself to an empty room is
        the exact thing this whole switch exists to avoid.
        """
        if on_the_page:
            logger.info("The web app has the floor; this microphone stands down.")
            self.ui.note("Listening through the web app. This microphone is off.")
        else:
            logger.info("The web app has gone; listening here again.")
            self.ui.note("The web app has gone. Listening on this microphone again.")

    def page_gone(self) -> None:
        """A page saying goodbye on its way out, so the wait is not needed."""
        self._page_left = time.monotonic()
        if self._page_seen:
            self._page_seen = 0.0
            self.live.settle()

    def page_here(self) -> None:
        """A browser just asked for something. Called from the handler.

        Ignored for a moment after a goodbye. What arrives in that window is
        the tail of a page that has already gone - its long polls were open
        when it went - and treating those as a page still being here is how
        the handover came to be announced twice in a row.
        """
        if time.monotonic() - self._page_left < GOODBYE:
            return
        self._page_seen = time.monotonic()

    def page_attached(self) -> bool:
        """Whether a page is open, from how recently one polled.

        The page long polls `/spoken` continuously, so a poll inside the last
        `PAGE_GONE` seconds is a browser that is still there. Closing the tab
        therefore leaves the desk silent for up to that long, which is the price
        of not needing the page to say goodbye - and nothing else here can,
        because a phone that walks out of range never says it either.
        """
        return time.monotonic() - self._page_seen < PAGE_GONE

    def clip(self, spoken_id: int, wait: float = 0.0) -> bytes | None:
        """The audio for one line of `spoken`, if it was rendered for the page.

        Waits a moment for one that is on its way. Publishing the line and
        keeping the clip are two statements and the page is quick enough to get
        between them, which reads as a reply with no audio - so the ordering is
        the fix and this is the guard on it.
        """
        with self._rendered:
            # Only the newest line can still be on its way. Anything older either
            # has a clip or was spoken at the desk and never will have one, and
            # the page asks about every line it sees.
            if wait and spoken_id >= self.spoken.cursor:
                self._rendered.wait_for(lambda: spoken_id in self.clips, timeout=wait)
            return self.clips.get(spoken_id)

    def _keep(self, spoken_id: int, wav: bytes) -> None:
        with self._rendered:
            self.clips[spoken_id] = wav
            while len(self.clips) > KEEP_CLIPS:
                self.clips.popitem(last=False)
            self._rendered.notify_all()

    def feed(self, pcm: bytes) -> None:
        """Take a chunk of audio from the page, as though it were the room.

        The desk is already standing down by the time this is called - the
        handler counts the request as a page being here first - but settling
        before the write rather than after it is what stops the first buffer of
        a newly connected phone landing while its own microphone is still
        deferred.
        """
        if self.stream is None:
            return
        self.live.settle()
        self.stream.write(pcm)

    def _listen(self) -> None:
        assert self.microphone is not None and self.transcriber is not None
        while self._running.is_set():
            audio = self.microphone.listen(timeout=0.5)
            # Cheap, and this loop wakes at least twice a second, so the desk has
            # its microphone back within that of the page going away.
            self.live.settle()
            if audio is None:
                continue
            heard = self.transcriber.transcribe(audio)
            if not heard:
                continue
            if self._echo.is_echo(heard):
                logger.debug("Ignored JARVIS hearing itself: %s", heard)
                continue
            # Only when the microphone was open through the reply. See _stop_talking.
            if self.headphones:
                self._stop_talking()
            utterance = self.transcript.add(heard)
            logger.info("[%d] %s", utterance.id, heard)
            self.ui.heard(heard)

    def ran(self, called: str, gave: str) -> None:
        """A finished tool call, for the page to draw beside the conversation.

        Reported by the terminal rather than by the brain, which is the route
        the live line already takes - see Doing. The brain does not know whether
        anything is drawing it, and code mode has no service to tell.

        Published once the call is finished, so every row arrives complete. What
        is happening while it runs is on the live line already, and a row that
        fills itself in afterwards is more machinery than a phone needs.
        """
        self.calls.add(called, gave=gave)

    def typed(self, text: str) -> None:
        """Take a typed line as though it had been heard.

        Same transcript, same line on screen, same everything downstream - the
        brain cannot tell the difference, and should not. Two things speech
        goes through that this does not: the echo guard,
        because nothing typed can be JARVIS hearing itself, and the pause,
        because pausing shuts the microphone and somebody typing has plainly
        chosen to say something. For that same reason it stops a reply in
        progress whatever the audio settings say.
        """
        text = text.strip()
        if not text:
            return
        self._stop_talking()
        utterance = self.transcript.add(text, always=True)
        logger.info("[%d] %s (typed)", utterance.id, text)
        self.ui.heard(text)

    def _start_hotkey(self) -> None:
        """Register the configured key to toggle transcription."""
        self._hotkey = HotkeyListener(
            on_pause=self.pause,
            on_resume=self.resume,
            key=self.config.service.hotkey,
            # Held rather than pressed. Two things one key does, because the
            # moment you want the second one is the moment you have just put
            # headphones on and are not at the keyboard to go looking.
            on_hold=lambda: self.wear_headphones(not self.headphones),
        )
        self._hotkey.start()

    @property
    def paused(self) -> bool:
        """Whether the desk microphone is shut."""
        return self.microphone is not None and self.microphone.paused

    def pause(self) -> bool:
        """Shut the desk microphone. Returns True if it was not already shut.

        The desk only. Num Lock is a key on the desk, and somebody pressing it
        on their way out of the room is muting the room they are leaving - not
        the phone in their pocket, which has a button of its own. It used to
        stop the transcript as well, which is every source at once, so leaving
        the house with JARVIS muted also took the phone down with it.

        The microphone stops being read, so a shut desk spends no CPU on
        transcription and writes nothing to the log or the transcript file.
        """
        if self.microphone is None or self.microphone.paused:
            return False
        self.microphone.pause()
        # Said on screen, not only logged. This has no sound and no visible
        # effect of its own, so with nothing drawn a working hotkey is
        # indistinguishable from a dead one.
        key = self.config.service.hotkey or "resume_transcription"
        self.ui.note(f"This microphone is off. {key} to start it again.")
        return True

    def resume(self) -> None:
        """Open the desk microphone again."""
        if self.microphone is None or not self.microphone.paused:
            return
        self.microphone.resume()
        self.ui.note("This microphone is listening again.")

    def page_microphone(self, on: bool) -> bool:
        """The page's own microphone, opened or shut, because the page said so.

        Not the same as the page merely stopping sending, which is how this used
        to end. The stream goes quiet, the splitter keeps the phrase it was in
        the middle of, and that half sentence comes back out the moment anything
        arrives again - said before the button was pressed and delivered after it
        was pressed a second time.

        The desk has never behaved that way: shutting it drains what is queued
        and abandons what is mid phrase. This is the same thing said over HTTP,
        because this microphone is in another room.
        """
        if self.remote is None:
            return False
        if on:
            self.remote.resume()
        else:
            self.remote.pause()
        return on

    @property
    def headphones(self) -> bool:
        """Whether the microphone stays open while JARVIS is talking."""
        return self._headphones.is_set()

    def wear_headphones(self, on: bool) -> bool:
        """Listen while speaking, or stop. Returns where it landed.

        `audio.listen_while_speaking` says where this starts and this is the
        same switch with a key and a button on it, because which answer is
        right changes during the day rather than between installs.

        It used to be about headphones: on speakers the microphone heard the
        reply and the only thing between that and JARVIS answering its own
        voice was the text comparison in echo.py, which a long reply had
        already beaten. With `audio.echo_cancellation` on it is not about
        headphones any more - the reply goes to the speakers like anything
        else, so it is cancelled like anything else.

        Nothing is unmuted here. A reply already being spoken was started on the
        old answer and finishes on it, which is a second or two rather than a
        state to reconcile.
        """
        if on:
            self._headphones.set()
        else:
            self._headphones.clear()
        logger.info("listen_while_speaking %s.", "on" if on else "off")
        self.ui.note(
            "Listening while I talk - say something and I stop."
            if on
            else "Not listening while I talk - this microphone shuts until I finish."
        )
        return on

    # ------------------------------------------------------------------ speak

    def say(self, text: str) -> None:
        """Queue speech and return, so a long reply does not block the agent."""
        text = text.strip()
        if not text or self.speech is None:
            return
        logger.info("say: %s", text)
        self.ui.spoke(text)
        self._echo.remember(text)

        # A page is open, so the reply belongs in the room the page is in.
        # Nothing is played here at all, which also means nothing to mute: the
        # browser's own echo cancellation is what keeps it from hearing itself,
        # and the page stops sending while a clip is playing.
        #
        # Rendered before the line is published, because the page asks for the
        # audio the instant it hears there is a reply - and half a second of
        # Kokoro later is far too late. A clip that has not been made yet looks
        # exactly like one that never will be.
        wav = self.speech.render(text) if self.live.on_the_page else None
        line = self.spoken.add(text, always=True)
        if wav is not None:
            self._keep(line.id, wav)
            logger.info("Sent to the web app rather than the speakers.")
            return

        if self.headphones or self.microphone is None:
            self.speech.say(text)
            return

        with self._speaking:
            # Both, not just the live one. A phone in the same room hears the
            # reply off the desk speakers as clearly as the desk does.
            for ears in self.live.ears:
                ears.mute()
            self._speaking_count += 1
        self.speech.say(text)
        threading.Thread(target=self._unmute_when_done, name="jarvis-unmute", daemon=True).start()

    def hush(self) -> None:
        """Stop talking now - drop what is queued and cut off what is playing."""
        if self.speech is not None:
            self.speech.interrupt()

    def _stop_talking(self) -> None:
        """Cut the reply off, because something was said over the top of it.

        Late, and unavoidably so: a phrase does not exist until it has ended,
        so this lands about two seconds after they began talking rather than on
        the first syllable. What it buys is the rest of a long wrong answer,
        which is worth having. It is not the same thing as stopping the moment
        somebody opens their mouth, and it does not pretend to be.

        Only worth doing where the microphone was open through the reply. With
        it shut, a phrase arriving now was recorded before the reply started
        and is nobody talking over anything.
        """
        if self.speech is None or not self.speech.speaking:
            return
        logger.info("Something was said over the reply, so it was cut off.")
        self.ui.note("Interrupted.")
        self.speech.interrupt()

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
        for ears in self.live.ears:
            ears.unmute()

    def status(self) -> dict:
        return {
            "listening": self._running.is_set(),
            "cursor": self.transcript.cursor,
            "stt": self.config.stt.backend,
            "tts": self.config.tts.engine,
            "paused": self.paused,
            "headphones": self.headphones,
            "webapp": self.stream is not None,
            "streaming": self.stream is not None and self.stream.live,
            "attached": self.live.on_the_page,
        }

    def stop(self) -> None:
        self._running.clear()
        if self._listener is not None:
            self._listener.join(timeout=5)
            self._listener = None
        if self._hotkey is not None:
            self._hotkey.stop()
            self._hotkey = None
        if self.stream is not None:
            self.stream.close()
        if self.remote is not None:
            self.remote.stop()
        if self.microphone is not None:
            self.microphone.stop()
        if self.speech is not None:
            self.speech.close()


class _Handler(BaseHTTPRequestHandler):
    """Loopback only. No auth, so do not bind this to anything routable.

    That holds with the web app on. It is reached from a phone by putting
    `tailscale serve` in front of this, which terminates the TLS and does the
    authenticating, and leaves the socket here exactly as private as it was.
    """

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
            self._stream(self.service.transcript, "heard", query)
        elif url.path == "/pause":
            self.service.pause()
            self._json(200, {"paused": True})
        elif not self._webapp():
            self._json(404, {"error": "not found"})
        elif url.path in ("/", "/index.html"):
            self._page()
        elif url.path == "/live":
            self.service.page_here()
            self._live(query)
        elif url.path == "/spoken":
            # Polled continuously by the page, so this is what tells the service
            # a browser is open and the reply belongs there rather than here.
            self.service.page_here()
            self._stream(self.service.spoken, "spoken", query)
        elif url.path == "/calls":
            # Not a page-here signal. One poll saying so is enough, and this one
            # is quiet for whole conversations at a time.
            self._stream(self.service.calls, "calls", query)
        elif url.path.startswith("/voice/"):
            self._voice(url.path)
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/say":
            self._do_say()
        elif path == "/pause":
            # Also a GET, which is what the CLI has always used. A browser is a
            # different sort of client though, and something that deafens JARVIS
            # should not be reachable by anything that follows links for fun.
            self.service.pause()
            self._json(200, {"paused": True})
        elif path == "/resume":
            self.service.resume()
            self._json(200, {"paused": False})
        elif path == "/headphones":
            self._do_headphones()
        elif not self._webapp():
            self._json(404, {"error": "not found"})
        elif path == "/typed":
            self._do_typed()
        elif path == "/microphone":
            self._do_microphone()
        elif path == "/audio":
            self._do_audio()
        elif path == "/gone":
            # A beacon on the way out. Without it the desk waits for the poll
            # to stop arriving, which is most of a minute of nobody listening
            # to the room somebody is actually standing in.
            self.service.page_gone()
            self._json(200, {"gone": True})
        else:
            self._json(404, {"error": "not found"})

    def _webapp(self) -> bool:
        """Whether the page and its endpoints exist at all.

        Off they are absent rather than present and refusing, which is how every
        other switch here behaves - and this one opens a microphone.
        """
        return self.service.config.service.start_webapp

    def _page(self) -> None:
        from .webapp import page

        body = page()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except GONE_AWAY:
            logger.debug("Client hung up before the page was written.")

    def _do_typed(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            text = str(payload["text"])
        except (ValueError, KeyError, TypeError):
            self._json(400, {"error": "expected a JSON body with a 'text' field"})
            return
        self.service.typed(text)
        self._json(200, {"heard": text})

    def _do_microphone(self) -> None:
        """The page saying whether its own microphone is open."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            on = bool(payload["on"])
        except (ValueError, KeyError, TypeError):
            self._json(400, {"error": "expected a JSON body with an 'on' field"})
            return
        self._json(200, {"on": self.service.page_microphone(on)})

    def _do_headphones(self) -> None:
        """Listen while speaking, or stop. An empty body toggles it.

        Beside /pause rather than behind the web app switch, because it is a
        thing this service does whether or not anything is serving a page.
        """
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
            wanted = payload.get("on")
        except (ValueError, AttributeError):
            self._json(400, {"error": "expected a JSON body with an 'on' field"})
            return
        on = not self.service.headphones if wanted is None else bool(wanted)
        self._json(200, {"headphones": self.service.wear_headphones(on)})

    def _live(self, query: dict) -> None:
        """Long poll the live line, so the page says what the terminal says."""
        settings = self.service.config.service
        try:
            since = int(query.get("since", ["0"])[0])
            wait = float(query.get("wait", ["0"])[0])
        except ValueError:
            self._json(400, {"error": "since and wait must be numbers"})
            return

        wait = max(0.0, min(wait, settings.max_wait_seconds))
        version, text = self.service.doing.read()
        if version == since and wait:
            version, text = self.service.doing.wait(since, timeout=wait)
        self._json(200, {"version": version, "doing": text})

    def _voice(self, path: str) -> None:
        """One rendered reply, for the page to play instead of the speakers."""
        self.service.page_here()
        try:
            wanted = int(path.rsplit("/", 1)[-1].removesuffix(".wav"))
        except ValueError:
            self._json(400, {"error": "expected /voice/<id>.wav"})
            return
        wav = self.service.clip(wanted, wait=CLIP_WAIT)
        if wav is None:
            # Ordinary rather than exceptional: the page asks for every line it
            # sees, and a line spoken at the desk has no clip to fetch.
            self._json(404, {"error": "nothing rendered for that one"})
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(wav)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(wav)
        except GONE_AWAY:
            logger.debug("Client hung up before the clip was written.")

    def _do_audio(self) -> None:
        """A chunk of 16 kHz mono 16-bit PCM, straight off the page."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json(400, {"error": "Content-Length must be a number"})
            return
        self.service.page_here()
        pcm = self.rfile.read(length) if length else b""
        # Half a sample is not a sample. Trimming beats handing the splitter a
        # buffer that is one byte out of phase for the rest of the stream.
        self.service.feed(pcm[: len(pcm) - len(pcm) % 2])
        self._json(200, {"samples": len(pcm) // 2})

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

    def _stream(self, transcript, key: str, query: dict) -> None:
        """Long poll one append-only record: what was heard, or what was said."""
        settings = self.service.config.service
        try:
            since = int(query.get("since", ["0"])[0])
            wait = float(query.get("wait", ["0"])[0])
        except ValueError:
            self._json(400, {"error": "since and wait must be numbers"})
            return

        wait = max(0.0, min(wait, settings.max_wait_seconds))
        delivered = transcript.since(since)
        if not delivered and wait:
            delivered = transcript.wait_for(since, timeout=wait)

        # Only advance past what actually went out - reporting the
        # transcript's own cursor swallows anything that arrived during the wait.
        self._json(
            200,
            {
                key: [item.as_dict() for item in delivered],
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
