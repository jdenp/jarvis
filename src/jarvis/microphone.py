"""Microphone capture.

Capture runs on a background thread and drops into a queue, so speech is not
lost while the brain is thinking or the speaker is talking. A single Recognizer
and a single Microphone are held open for the whole session - reopening the
device per utterance costs a few hundred milliseconds and loses the calibrated
energy threshold.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from types import TracebackType

import speech_recognition as sr

from .config import AudioConfig

logger = logging.getLogger("jarvis.microphone")


class MicrophoneError(RuntimeError):
    """Raised when the input device cannot be opened."""


def phrase_duration(audio: sr.AudioData) -> float:
    """Length of a captured phrase in seconds."""
    bytes_per_second = audio.sample_rate * audio.sample_width
    return len(audio.frame_data) / bytes_per_second if bytes_per_second else 0.0


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
        self._stop_listening = None
        self._queue: queue.Queue[sr.AudioData] = queue.Queue(maxsize=16)
        self._muted = threading.Event()
        self._deaf_until = 0.0
        self._lock = threading.Lock()

    @property
    def energy_threshold(self) -> float:
        return self._recognizer.energy_threshold

    def start(self) -> None:
        """Open the device, calibrate, and begin background capture."""
        if self._stop_listening is not None:
            return
        try:
            self._source = sr.Microphone(device_index=self.config.device_index)
        except OSError as exc:  # pragma: no cover - hardware dependent
            raise MicrophoneError(f"Could not open input device: {exc}") from exc

        self._calibrate()
        self._stop_listening = self._recognizer.listen_in_background(
            self._source, self._on_audio, phrase_time_limit=self.config.phrase_time_limit
        )
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

    def _on_audio(self, _recognizer: sr.Recognizer, audio: sr.AudioData) -> None:
        """Background thread callback. Drops audio while muted or backed up."""
        if self._muted.is_set():
            return
        # A phrase is only delivered once it ends, so a flag checked here says
        # nothing about when the audio was recorded. Work back to the start of
        # the phrase and drop anything that overlapped JARVIS speaking -
        # otherwise its own voice arrives just after unmute and is transcribed.
        started_at = time.monotonic() - phrase_duration(audio)
        if started_at < self._deaf_until:
            logger.debug("Dropped a phrase that overlapped JARVIS speaking.")
            return
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
        """Stop queueing audio. Used while JARVIS is speaking, so it does not
        transcribe its own voice."""
        self._muted.set()

    def unmute(self) -> None:
        """Resume queueing, discarding anything captured while JARVIS spoke.

        The guard runs a little past the moment speech stopped, to cover the
        output buffer draining and the room's echo tail.
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
        with self._lock:
            stopper, self._stop_listening = self._stop_listening, None
        if stopper is not None:
            stopper(wait_for_stop=True)
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

        speech_recognition lists outputs alongside inputs, so filter on the
        input channel count - picking a speaker here fails at open time.
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
