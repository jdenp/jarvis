"""Microphone capture.

Capture runs on a background thread into a queue, so nothing is lost while the
agent is thinking. One Recognizer and one Microphone stay open for the session -
reopening per utterance is slow and throws away the calibration.

The stream is cut into phrases here rather than by speech_recognition, so that
background noise cannot hold a phrase open. See PhraseEnd, and vad.py for how a
buffer is judged to be speech at all.
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from collections import deque
from types import TracebackType

import speech_recognition as sr

from .config import AudioConfig
from .vad import SAMPLE_RATE, SAMPLES, build_detector

logger = logging.getLogger("jarvis.microphone")

KEEP_SILENCE = 0.5  # silence kept either side of a phrase


class MicrophoneError(RuntimeError):
    """Raised when the input device cannot be opened."""


class PhraseEnd:
    """Decides when a phrase has finished, from a trailing window of buffers.

    speech_recognition needs ``pause_threshold`` of *consecutive* quiet buffers,
    so one keyboard click resets the count and a noisy room holds the phrase open
    until the time limit. This still waits for a whole ``pause_threshold`` of
    quiet, but allows it to be interrupted: the window is widened, and the quiet
    inside it only has to add up. So ``pause_threshold`` keeps meaning what it
    says and the fraction buys noise tolerance rather than spending patience.
    """

    def __init__(
        self, seconds_per_buffer: float, pause_threshold: float, quiet_fraction: float
    ) -> None:
        fraction = min(1.0, max(0.05, quiet_fraction))
        self.needed = max(1, math.ceil(pause_threshold / seconds_per_buffer))
        self.window = max(self.needed, math.ceil(self.needed / fraction))
        self._quiet: deque[bool] = deque(maxlen=self.window)

    def feed(self, quiet: bool) -> bool:
        """Add one buffer. True once the window holds a full pause of quiet."""
        self._quiet.append(quiet)
        return sum(self._quiet) >= self.needed

    @property
    def trailing_quiet(self) -> int:
        """Consecutive quiet buffers at the end, so trimming never cuts speech."""
        count = 0
        for quiet in reversed(self._quiet):
            if not quiet:
                break
            count += 1
        return count


class Microphone:
    """Continuous background capture from a single input device."""

    def __init__(self, config: AudioConfig | None = None) -> None:
        self.config = config or AudioConfig()
        self.detector = build_detector(self.config)
        self._recognizer = sr.Recognizer()

        self._source: sr.Microphone | None = None
        self._queue: queue.Queue[sr.AudioData] = queue.Queue(maxsize=16)
        self._muted = threading.Event()
        self._deaf_until = 0.0
        self._running = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def energy_threshold(self) -> float:
        """Only meaningful for the energy detector; Silero ignores loudness."""
        return getattr(self.detector, "threshold", 0.0)

    def start(self) -> None:
        """Open the device, calibrate if needed, and begin background capture."""
        if self._thread is not None:
            return
        try:
            # 16 kHz explicitly. Left to itself speech_recognition opens at the
            # device default - 44100 on this mic - and Silero silently scores
            # frames that are not the length it thinks they are.
            self._source = sr.Microphone(
                device_index=self.config.device_index,
                sample_rate=SAMPLE_RATE,
                chunk_size=SAMPLES,
            )
        except OSError as exc:  # pragma: no cover - hardware dependent
            raise MicrophoneError(f"Could not open input device: {exc}") from exc

        self._calibrate()
        self._running.set()
        self._thread = threading.Thread(target=self._capture, name="jarvis-capture", daemon=True)
        self._thread.start()
        logger.info("Listening (%s).", self.detector.name)

    def _calibrate(self) -> None:
        """Measure ambient noise, if the detector actually uses a threshold."""
        if not self.detector.calibrates:
            return
        if self.config.energy_threshold is not None:
            logger.info("Using fixed energy threshold %.0f.", self.config.energy_threshold)
            return
        assert self._source is not None
        logger.info("Calibrating, please stay quiet for %.1fs.", self.config.calibration_seconds)
        try:
            with self._source as source:
                self._recognizer.adjust_for_ambient_noise(
                    source, duration=self.config.calibration_seconds
                )
        except OSError as exc:  # pragma: no cover - hardware dependent
            raise MicrophoneError(f"Could not read from input device: {exc}") from exc

        self.detector.calibrate(self._recognizer.energy_threshold)

    def _capture(self) -> None:
        """Hold the device open and read it until stopped."""
        assert self._source is not None
        try:
            with self._source as source:
                self._run(source)
        except OSError:  # pragma: no cover - hardware dependent
            logger.exception("Microphone capture stopped.")

    def _run(self, source) -> None:
        """Cut the stream into phrases and queue them.

        Ours rather than speech_recognition's, for the phrase end rule in
        PhraseEnd and because gating per buffer is simpler than working out
        after the fact when a delivered phrase was recorded.
        """
        seconds_per_buffer = source.CHUNK / source.SAMPLE_RATE
        if not self.detector.calibrates and source.SAMPLE_RATE != SAMPLE_RATE:
            logger.warning(
                "Device is at %d Hz, but speech detection needs %d Hz. Set audio.vad to 'energy'.",
                source.SAMPLE_RATE,
                SAMPLE_RATE,
            )
        keep = max(1, math.ceil(KEEP_SILENCE / seconds_per_buffer))
        shortest = max(1, math.ceil(self.config.min_speech_seconds / seconds_per_buffer))
        longest = max(1, math.ceil(self.config.phrase_time_limit / seconds_per_buffer))

        frames: deque[bytes] = deque()
        pause: PhraseEnd | None = None
        recorded = spoken = 0

        while self._running.is_set():
            buffer = source.stream.read(source.CHUNK)
            if not buffer:
                break
            if not self._accepting():
                # A gap in the audio, so any streaming state is now stale.
                frames.clear()
                self.detector.reset()
                pause, recorded, spoken = None, 0, 0
                continue

            speech = self.detector.is_speech(buffer)
            frames.append(buffer)

            if pause is None:
                if len(frames) > keep:
                    frames.popleft()
                if speech:
                    pause = PhraseEnd(
                        seconds_per_buffer,
                        self.config.pause_threshold,
                        self.config.pause_quiet_fraction,
                    )
                continue

            recorded += 1
            spoken += speech
            finished = pause.feed(not speech)
            if not finished and recorded < longest:
                continue

            if finished:
                # Trim the silence it ended on, but only what is actually silent -
                # a word inside the window belongs to this phrase, and the next
                # one starts from scratch, so popping it loses it outright.
                for _ in range(max(0, pause.trailing_quiet - keep)):
                    if not frames:
                        break
                    frames.pop()
            else:
                logger.debug("Phrase hit the %.0fs limit.", self.config.phrase_time_limit)
            if spoken >= shortest:
                self._deliver(frames, source)
            frames.clear()
            pause, recorded, spoken = None, 0, 0

    def _accepting(self) -> bool:
        """Whether audio arriving right now is wanted.

        Checked per buffer, so JARVIS's own voice is dropped as it is recorded.
        """
        return not self._muted.is_set() and time.monotonic() >= self._deaf_until

    def _deliver(self, frames: deque[bytes], source) -> None:
        audio = sr.AudioData(b"".join(frames), source.SAMPLE_RATE, source.SAMPLE_WIDTH)
        try:
            self._queue.put_nowait(audio)
        except queue.Full:
            logger.warning("Audio queue full, dropping a phrase.")

    def listen(self, timeout: float | None = None) -> sr.AudioData | None:
        """Pop the next captured phrase, or None if nothing arrived in time."""
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def mute(self) -> None:
        """Stop queueing audio, so JARVIS does not transcribe its own voice."""
        self._muted.set()

    def unmute(self) -> None:
        """Resume queueing, discarding anything captured while JARVIS spoke.

        The guard runs past the moment speech stopped, for the echo tail.
        """
        self._deaf_until = time.monotonic() + self.config.echo_guard_seconds
        self.drain()
        self._muted.clear()

    def drain(self) -> None:
        """Discard any queued phrases."""
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def stop(self) -> None:
        """Stop background capture and release the device."""
        self._running.clear()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5)
            logger.debug("Microphone stopped.")
        self._source = None

    def __enter__(self) -> Microphone:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    @staticmethod
    def list_devices() -> list[tuple[int, str]]:
        """Return (index, name) for every device that can actually record.

        speech_recognition lists outputs too, and picking one fails at open.
        """
        import pyaudio

        audio = pyaudio.PyAudio()
        try:
            devices = []
            for index in range(audio.get_device_count()):
                info = audio.get_device_info_by_index(index)
                if info.get("maxInputChannels", 0) > 0:
                    devices.append((index, str(info.get("name", "?"))))
            return devices
        finally:
            audio.terminate()
