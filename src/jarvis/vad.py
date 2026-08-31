"""Deciding whether a buffer of audio is someone talking.

Loudness cannot tell a footstep from a word, so noise holds a phrase open until
the time limit. Silero answers the actual question and ships with
faster-whisper, so it costs no new dependency.
"""

from __future__ import annotations

import logging

from .config import AudioConfig

logger = logging.getLogger("jarvis.vad")

# Silero v6 takes 512 new samples plus 64 of the frame before, at 16 kHz.
SAMPLES = 512
CONTEXT = 64
SAMPLE_RATE = 16_000
SECONDS_PER_BUFFER = SAMPLES / SAMPLE_RATE

# speech_recognition's asymmetric average, which the energy detector keeps.
DAMPING = 0.15
RATIO = 1.5


def buffer_energy(buffer: bytes) -> float:
    """RMS of one buffer of 16-bit mono audio, which is what pyaudio gives us."""
    import numpy as np

    samples = np.frombuffer(buffer, dtype=np.int16).astype(np.float32)
    return float(np.sqrt(np.mean(np.square(samples)))) if samples.size else 0.0


class EnergyDetector:
    """Loudness against an adaptive threshold. Cheap, and fooled by any noise."""

    name = "energy"
    calibrates = True

    def __init__(self, config: AudioConfig) -> None:
        self.config = config
        self.threshold = config.energy_threshold or 300.0
        self._adapt = config.dynamic_energy_threshold and config.energy_threshold is None
        self._damping = DAMPING**SECONDS_PER_BUFFER

    def calibrate(self, measured: float) -> None:
        """Take a measured ambient level, but never go below the floor."""
        floor = self.config.min_energy_threshold
        if measured < floor:
            logger.info(
                "Calibrated to %.0f, low enough to hear the speakers. Using %.0f.", measured, floor
            )
        self.threshold = max(measured, floor)

    def is_speech(self, buffer: bytes) -> bool:
        energy = buffer_energy(buffer)
        speech = energy > self.threshold
        if self._adapt:
            self.threshold = max(
                self.config.min_energy_threshold,
                self.threshold * self._damping + energy * RATIO * (1 - self._damping),
            )
        return speech

    def reset(self) -> None:
        pass


class SileroDetector:
    """Scores each frame for speech, whatever its volume.

    Measured on this machine: 60Hz thumps as loud as speech score 0.006, and the
    same sentence 24dB quieter scores the same as the original.
    """

    name = "silero"
    calibrates = False

    def __init__(self, threshold: float = 0.35, hysteresis: float = 0.15) -> None:
        import numpy as np
        from faster_whisper.vad import get_vad_model

        self._np = np
        # Shares faster-whisper's session, which is already pinned to one thread.
        self._session = get_vad_model().session
        self.threshold = threshold
        self.hysteresis = hysteresis
        self.reset()

    def reset(self) -> None:
        """Clear the LSTM state, for when the audio stream has a gap in it."""
        np = self._np
        self._h = np.zeros((1, 1, 128), dtype="float32")
        self._c = np.zeros((1, 1, 128), dtype="float32")
        self._context = np.zeros(CONTEXT, dtype="float32")
        self._active = False

    def probability(self, buffer: bytes) -> float:
        """Speech probability for one frame, 0.0 if it is the wrong size."""
        np = self._np
        frame = np.frombuffer(buffer, dtype=np.int16).astype(np.float32) / 32768.0
        if frame.size != SAMPLES:
            return 0.0
        stacked = np.concatenate([self._context, frame])[None, :]
        probs, self._h, self._c = self._session.run(
            None, {"input": stacked, "h": self._h, "c": self._c}
        )
        self._context = frame[-CONTEXT:]
        return float(np.asarray(probs).reshape(-1)[0])

    def is_speech(self, buffer: bytes) -> bool:
        """Harder to start speaking than to keep speaking, as Silero intends."""
        bar = self.threshold - self.hysteresis if self._active else self.threshold
        self._active = self.probability(buffer) >= bar
        return self._active


# Said once a run: two microphones are built at startup, on the same settings.
_announced = False


def build_detector(config: AudioConfig | None = None):
    """Construct the detector named by ``config.vad``, falling back if asked."""
    config = config or AudioConfig()
    wanted = config.vad.strip().lower()

    if wanted == "energy":
        return EnergyDetector(config)
    if wanted not in {"auto", "silero"}:
        raise ValueError(f"Unknown vad {config.vad!r}. Choose auto, silero or energy.")

    try:
        detector = SileroDetector(config.vad_threshold, config.vad_hysteresis)
    except Exception as exc:
        if wanted == "silero":
            raise
        logger.warning("Silero is unavailable (%s), falling back to loudness.", exc)
        return EnergyDetector(config)
    global _announced
    if not _announced:
        _announced = True
        logger.info(
            "Speech detection: silero, threshold %.2f, holding to %.2f.",
            config.vad_threshold,
            config.vad_threshold - config.vad_hysteresis,
        )
    return detector
