"""Text to speech.

Backends are built on the worker thread that uses them - both SAPI and pygame
hold thread affine resources. ``auto`` picks ``sapi``, the offline Windows
voice; ``edge`` sounds better but sends every reply to Microsoft.
"""

from __future__ import annotations

import logging
import math
import queue
import re
import threading
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Protocol

from .config import TtsConfig

logger = logging.getLogger("jarvis.tts")

_SENTENCE_END = re.compile(r"(?<=[.!?])([\"')\]]*)\s+|\n+")


class Speaker(Protocol):
    """A synthesiser that can be interrupted mid utterance."""

    def speak(self, text: str) -> None: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...

    @property
    def is_local(self) -> bool: ...


class NullSpeaker:
    """Logs instead of speaking. Used for text mode and as the last fallback."""

    is_local = True

    def speak(self, text: str) -> None:
        logger.info("[tts] %s", text)

    def stop(self) -> None:
        pass

    def close(self) -> None:
        pass


class SapiSpeaker:
    """Offline Windows speech, driving SAPI directly.

    Not pyttsx3: it waits on COM events through a message pump, and those stop
    arriving once the microphone initialises COM. Speak then returns silently.

    Spoken asynchronously and polled, so the purge that cancels it runs on this
    thread. SAPI lives in the apartment that created it, so a purge sent from
    another thread cannot be delivered until the Speak it was meant to cancel has
    already finished - and it blocks that other thread until then.
    """

    is_local = True

    ASYNC = 1  # SVSFlagsAsync
    PURGE_ASYNC = 3  # SVSFlagsAsync | SVSFPurgeBeforeSpeak
    POLL_MS = 50

    def __init__(self, config: TtsConfig) -> None:
        import comtypes.client

        self._cancelled = threading.Event()
        self._voice = comtypes.client.CreateObject("SAPI.SpVoice")
        self._voice.Rate = _sapi_rate(config.rate)
        self._voice.Volume = int(max(0.0, min(1.0, config.volume)) * 100)
        if token := self._find_voice(config.sapi_voice):
            self._voice.Voice = token

    def _find_voice(self, wanted: str):
        """First installed voice from a comma separated preference list.

        A list, because falling through to the system default lands on the
        wrong accent entirely.
        """
        preferences = [part.strip().lower() for part in wanted.split(",") if part.strip()]
        if not preferences:
            return None

        voices = self._voice.GetVoices()
        installed = [(i, voices.Item(i).GetDescription()) for i in range(voices.Count)]
        for preference in preferences:
            for index, description in installed:
                if preference in description.lower():
                    logger.info("Speaking as %s.", description)
                    return voices.Item(index)

        logger.warning(
            "None of %s are installed, using the system default. Available: %s",
            ", ".join(preferences),
            ", ".join(name for _, name in installed) or "none",
        )
        return None

    def speak(self, text: str) -> None:
        self._cancelled.clear()
        self._voice.Speak(text, self.ASYNC)
        while not self._voice.WaitUntilDone(self.POLL_MS):
            if self._cancelled.is_set():
                self._voice.Speak("", self.PURGE_ASYNC)
                return

    def stop(self) -> None:
        """Safe from any thread: no COM call, just a flag speak() is watching."""
        self._cancelled.set()

    def close(self) -> None:
        self.stop()


def _sapi_rate(words_per_minute: int) -> int:
    """Map words per minute onto SAPI's -10..10 scale, where 0 is about 175wpm."""
    ratio = max(1, words_per_minute) / 175.0
    return max(-10, min(10, round(math.log(ratio, 1.3))))


class EdgeSpeaker:
    """Neural voice via Microsoft Edge TTS, played back through pygame.

    Not local: every reply is sent to Microsoft to be synthesised.
    """

    is_local = False

    def __init__(self, config: TtsConfig, cache_dir: Path | None = None) -> None:
        import os

        os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

        import edge_tts
        import pygame

        self._edge_tts = edge_tts
        self._pygame = pygame
        self.config = config
        self._cache_dir = cache_dir or Path.home() / ".cache" / "jarvis" / "tts"
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._cancelled = threading.Event()

        pygame.mixer.init()
        pygame.mixer.music.set_volume(max(0.0, min(1.0, config.volume)))

    def _rate_percent(self) -> str:
        """Map words-per-minute onto the +N% that Edge expects, 180wpm being 0%."""
        percent = round((self.config.rate - 180) / 180 * 100)
        return f"{percent:+d}%"

    def _synthesise(self, text: str) -> Path:
        import asyncio

        path = self._cache_dir / "utterance.mp3"
        communicate = self._edge_tts.Communicate(
            text, voice=self.config.voice, rate=self._rate_percent()
        )
        asyncio.run(communicate.save(str(path)))
        return path

    def speak(self, text: str) -> None:
        self._cancelled.clear()
        path = self._synthesise(text)
        music = self._pygame.mixer.music
        music.load(str(path))
        music.play()
        while music.get_busy():
            if self._cancelled.is_set():
                music.stop()
                break
            self._pygame.time.wait(50)
        music.unload()

    def stop(self) -> None:
        self._cancelled.set()

    def close(self) -> None:
        try:
            self._pygame.mixer.quit()
        except Exception:  # pragma: no cover - driver teardown is best effort
            logger.debug("pygame mixer did not shut down cleanly.", exc_info=True)


def build_speaker(config: TtsConfig | None = None) -> Speaker:
    """Construct the speaker named by ``config.engine``, falling back if asked.

    Call this on the thread that will do the speaking.
    """
    config = config or TtsConfig()
    engine = config.engine.strip().lower()

    if engine == "none":
        return NullSpeaker()
    if engine == "auto":
        # edge is never reached by auto - it ships every reply to Microsoft.
        try:
            speaker = SapiSpeaker(config)
        except Exception as exc:
            logger.warning("Offline speech is unavailable (%s), responses will be text only.", exc)
            return NullSpeaker()
        logger.info("Using sapi text to speech.")
        return speaker
    if engine == "sapi":
        return SapiSpeaker(config)
    if engine == "edge":
        logger.warning(
            "Text to speech is set to 'edge' - every reply is sent to Microsoft to be "
            "synthesised. Use 'sapi' to keep it on this machine."
        )
        return EdgeSpeaker(config)
    raise ValueError(f"Unknown TTS engine {config.engine!r}. Choose auto, edge, sapi or none.")


class SpeechEngine:
    """Serialises utterances onto one worker thread and allows barge-in.

    The backend is built inside the worker, where its resources are used.
    """

    _STOP = object()

    def __init__(self, factory: Callable[[], Speaker] | None = None) -> None:
        self._factory = factory or build_speaker
        self._queue: queue.Queue[object] = queue.Queue()
        self._speaker: Speaker | None = None
        self._ready = threading.Event()
        # A count rather than an idle flag: a flag races with say() between
        # incrementing and enqueueing, and wait() returns too early.
        self._drained = threading.Condition()
        self._pending = 0
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="jarvis-tts", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            self._speaker = self._factory()
        except Exception:
            logger.exception("Could not start any TTS backend, falling back to text.")
            self._speaker = NullSpeaker()
        finally:
            self._ready.set()

        while True:
            item = self._queue.get()
            if item is self._STOP:
                break
            try:
                self._speaker.speak(str(item))
            except Exception:
                logger.exception("Speech synthesis failed for %.60s.", item)
            finally:
                self._finish_one()

        self._speaker.close()

    def _finish_one(self) -> None:
        with self._drained:
            self._pending = max(0, self._pending - 1)
            if self._pending == 0:
                self._drained.notify_all()

    @property
    def speaking(self) -> bool:
        with self._drained:
            return self._pending > 0

    @property
    def is_local(self) -> bool:
        """Whether the backend the worker actually settled on stays on this machine."""
        self._ready.wait(timeout=10)
        return getattr(self._speaker, "is_local", False)

    def say(self, text: str) -> None:
        """Queue an utterance. Returns immediately."""
        text = text.strip()
        if not text or self._closed:
            return
        with self._drained:
            self._pending += 1
        self._queue.put(text)

    def wait(self, timeout: float | None = None) -> bool:
        """Block until everything queued has been spoken."""
        with self._drained:
            return self._drained.wait_for(lambda: self._pending == 0, timeout)

    def interrupt(self) -> None:
        """Drop anything queued and cut off the current utterance."""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
            self._finish_one()
        self._ready.wait(timeout=10)
        if self._speaker is not None:
            try:
                self._speaker.stop()
            except Exception:  # cross thread stop is best effort
                logger.debug("Could not interrupt the current utterance.", exc_info=True)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.interrupt()
        self._queue.put(self._STOP)
        self._thread.join(timeout=10)


def for_speaking(text: str) -> str:
    """Prose tidied into something a synthesiser can read.

    Text written for a screen arrives with its emphasis and emoji intact, and
    SAPI reads `**947 tokens**` as "asterisk asterisk nine four seven". Stripped
    rather than rejected, because the sentence underneath is usually fine.
    Newlines go too - a spoken line is one line.
    """
    cleaned = text
    for marker in ("**", "__", "`", "*", "#"):
        cleaned = cleaned.replace(marker, "")
    # Anything outside the basic plane is an emoji or a symbol, and has no
    # pronunciation worth hearing.
    cleaned = "".join(character for character in cleaned if ord(character) < 0x2500)
    return " ".join(cleaned.split())


def iter_sentences(chunks: Iterable[str], min_chars: int = 12) -> Iterator[str]:
    """Regroup streamed tokens into speakable sentences."""
    buffer = ""
    for chunk in chunks:
        buffer += chunk
        while True:
            sentence, buffer = _split_sentence(buffer, min_chars)
            if sentence is None:
                break
            yield sentence
    if tail := buffer.strip():
        yield tail


def _split_sentence(buffer: str, min_chars: int) -> tuple[str | None, str]:
    """First sentence boundary that leaves something worth speaking behind it.

    Short fragments are merged into the sentence that follows.
    """
    for match in _SENTENCE_END.finditer(buffer):
        # Keep any closing quote or bracket with the sentence it belongs to.
        end = match.start() + len(match.group(1) or "")
        sentence = buffer[:end].strip()
        if len(sentence) >= min_chars:
            return sentence, buffer[match.end() :]
    return None, buffer
