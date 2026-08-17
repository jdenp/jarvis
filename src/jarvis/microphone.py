"""Microphone capture.

Capture runs on a background thread into a queue, so nothing is lost while the
agent is thinking. One Recognizer and one Microphone stay open for the session -
reopening per utterance is slow and throws away the calibration.

The stream is cut into phrases here rather than by speech_recognition, so that
background noise cannot hold a phrase open. See PhraseEnd.
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

logger = logging.getLogger("jarvis.microphone")


class MicrophoneError(RuntimeError):
    """Raised when the input device cannot be opened."""


def buffer_energy(buffer: bytes) -> float:
    """RMS of one buffer of 16-bit mono audio, which is what pyaudio gives us."""
    import numpy as np

    samples = np.frombuffer(buffer, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(np.square(samples)))) if samples.size else 0.0


class PhraseEnd:
    """Decides when a phrase has finished, from a trailing window of energies.

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

    def feed(self, energy: float, threshold: float) -> bool:
        """Add one buffer. True once the window holds a full pause of quiet."""
        self._quiet.append(energy <= threshold)
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
        self._recognizer = sr.Recognizer()
        self._recognizer.dynamic_energy_threshold = self.config.dynamic_energy_threshold
        self._recognizer.pause_threshold = self.config.pause_threshold
        if self.config.energy_threshold is not None:
            self._recognizer.energy_threshold = self.config.energy_threshold
            self._recognizer.dynamic_energy_threshold = False

        self._source: sr.Microphone | None = None
        self._queue: queue.Queue[sr.AudioData] = queue.Queue(maxsize=16)
        self._muted = threading.Event()
        self._deaf_until = 0.0
        self._running = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def energy_threshold(self) -> float:
        return self._recognizer.energy_threshold

    def start(self) -> None:
        """Open the device, calibrate, and begin background capture."""
        if self._thread is not None:
            return
        try:
            self._source = sr.Microphone(device_index=self.config.device_index)
        except OSError as exc:  # pragma: no cover - hardware dependent
            raise MicrophoneError(f"Could not open input device: {exc}") from exc

        self._calibrate()
        self._running.set()
        self._thread = threading.Thread(target=self._capture, name="jarvis-capture", daemon=True)
        self._thread.start()
        logger.info("Listening (energy threshold %.0f).", self.energy_threshold)

    def _calibrate(self) -> None:
        """Measure ambient noise on the recognizer that will do the listening."""
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

        self._apply_threshold_floor()

    def _apply_threshold_floor(self) -> None:
        """Keep calibration from leaving the mic sensitive enough to hear the speakers."""
        floor = self.config.min_energy_threshold
        measured = self._recognizer.energy_threshold
        if measured < floor:
            logger.info(
                "Calibrated to %.0f, low enough to pick up the speakers. Raising to %.0f.",
                measured,
                floor,
            )
            self._recognizer.energy_threshold = floor

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
        keep = max(1, math.ceil(self._recognizer.non_speaking_duration / seconds_per_buffer))
        shortest = max(1, math.ceil(self._recognizer.phrase_threshold / seconds_per_buffer))
        longest = max(1, math.ceil(self.config.phrase_time_limit / seconds_per_buffer))

        frames: deque[bytes] = deque()
        detector: PhraseEnd | None = None
        recorded = loud = 0

        while self._running.is_set():
            buffer = source.stream.read(source.CHUNK)
            if not buffer:
                break
            if not self._accepting():
                frames.clear()
                detector, recorded, loud = None, 0, 0
                continue

            energy = buffer_energy(buffer)
            frames.append(buffer)

            if detector is None:
                if len(frames) > keep:
                    frames.popleft()
                if energy > self._recognizer.energy_threshold:
                    detector = PhraseEnd(
                        seconds_per_buffer,
                        self.config.pause_threshold,
                        self.config.pause_quiet_fraction,
                    )
                else:
                    self._adjust(energy, seconds_per_buffer)
                continue

            recorded += 1
            loud += energy > self._recognizer.energy_threshold
            finished = detector.feed(energy, self._recognizer.energy_threshold)
            self._adjust(energy, seconds_per_buffer)
            if not finished and recorded < longest:
                continue

            if finished:
                # Trim the silence it ended on, but only what is actually silent -
                # a word spoken inside the window belongs to this phrase, and the
                # next one starts from scratch, so popping it loses it outright.
                for _ in range(max(0, detector.trailing_quiet - keep)):
                    if not frames:
                        break
                    frames.pop()
            else:
                logger.debug("Phrase hit the %.0fs limit.", self.config.phrase_time_limit)
            # Speaking buffers, not elapsed ones, or a click plus a pause counts.
            if loud >= shortest:
                self._deliver(frames, source)
            frames.clear()
            detector, recorded, loud = None, 0, 0

    def _accepting(self) -> bool:
        """Whether audio arriving right now is wanted.

        Checked per buffer, so JARVIS's own voice is dropped as it is recorded.
        """
        return not self._muted.is_set() and time.monotonic() >= self._deaf_until

    def _adjust(self, energy: float, seconds_per_buffer: float) -> None:
        """speech_recognition's asymmetric weighted average, plus the floor."""
        recognizer = self._recognizer
        if not recognizer.dynamic_energy_threshold:
            return
        damping = recognizer.dynamic_energy_adjustment_damping**seconds_per_buffer
        target = energy * recognizer.dynamic_energy_ratio
        recognizer.energy_threshold = max(
            self.config.min_energy_threshold,
            recognizer.energy_threshold * damping + target * (1 - damping),
        )

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
