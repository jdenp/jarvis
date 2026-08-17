"""Speech detection.

The point of Silero is that loudness and speech are different questions: a
footstep is as loud as a word. These assert the discrimination directly, with
signals built here rather than recorded.
"""

from __future__ import annotations

import numpy as np
import pytest

from jarvis.config import AudioConfig
from jarvis.vad import (
    SAMPLES,
    EnergyDetector,
    SileroDetector,
    buffer_energy,
    build_detector,
)

SECONDS = 1.5
TOTAL = int(16_000 * SECONDS)


def to_buffers(samples: np.ndarray) -> list[bytes]:
    """Whole frames of int16, as the microphone would deliver them."""
    clipped = np.clip(samples, -1.0, 1.0)
    raw = (clipped * 32767).astype(np.int16)
    return [raw[i : i + SAMPLES].tobytes() for i in range(0, len(raw) - SAMPLES + 1, SAMPLES)]


def speech_fraction(detector: SileroDetector, samples: np.ndarray) -> float:
    detector.reset()
    buffers = to_buffers(samples)
    return sum(detector.is_speech(b) for b in buffers) / len(buffers)


def thumps() -> np.ndarray:
    """Low frequency bursts with gaps - a footstep, near enough."""
    step = np.sin(2 * np.pi * 60 * np.arange(1280) / 16_000) * 0.3
    gap = np.zeros(3200)
    return np.concatenate([np.concatenate([step, gap]) for _ in range(6)]).astype(np.float32)


@pytest.fixture(scope="module")
def silero() -> SileroDetector:
    return SileroDetector()


def test_energy_cannot_tell_a_thump_from_speech():
    """Why Silero is here at all: the two are indistinguishable by loudness."""
    loud = np.full(TOTAL, 0.1, dtype=np.float32)
    assert buffer_energy(to_buffers(thumps())[0]) > 1000
    assert buffer_energy(to_buffers(loud)[0]) > 1000


def test_a_loud_thump_is_not_speech(silero):
    assert speech_fraction(silero, thumps()) == 0.0


def test_loud_white_noise_is_not_speech(silero):
    rng = np.random.default_rng(0)
    noise = (rng.standard_normal(TOTAL) * 0.1).astype(np.float32)
    assert speech_fraction(silero, noise) == 0.0


def test_silence_is_not_speech(silero):
    assert speech_fraction(silero, np.zeros(TOTAL, dtype=np.float32)) == 0.0


def test_probability_is_a_probability(silero):
    silero.reset()
    value = silero.probability(to_buffers(thumps())[0])
    assert 0.0 <= value <= 1.0


def test_a_wrong_sized_buffer_is_not_speech(silero):
    """Silero only accepts 512 samples, and a short read must not crash it."""
    assert silero.probability(b"\x00\x00" * 100) == 0.0
    assert silero.is_speech(b"") is False


def test_hysteresis_holds_speech_through_a_dip(monkeypatch):
    """Without it, a quiet consonant mid word reads as a pause."""
    detector = SileroDetector(threshold=0.35, hysteresis=0.15)
    scores = iter([0.9, 0.25, 0.9, 0.1])
    monkeypatch.setattr(detector, "probability", lambda _buffer: next(scores))
    frame = bytes(SAMPLES * 2)

    assert detector.is_speech(frame) is True
    assert detector.is_speech(frame) is True, "0.25 is above the 0.20 holding bar"
    assert detector.is_speech(frame) is True
    assert detector.is_speech(frame) is False, "0.10 is below it, so the pause starts"


def test_the_bar_to_start_speaking_is_the_higher_one(monkeypatch):
    detector = SileroDetector(threshold=0.35, hysteresis=0.15)
    monkeypatch.setattr(detector, "probability", lambda _buffer: 0.25)
    assert detector.is_speech(bytes(SAMPLES * 2)) is False


def test_reset_clears_the_state(silero):
    silero.reset()
    before = silero._h.copy()
    silero.probability(to_buffers(thumps())[0])
    silero.reset()
    assert np.array_equal(silero._h, before)


# ------------------------------------------------------------------ factory


def test_auto_picks_silero():
    assert build_detector(AudioConfig(vad="auto")).name == "silero"


def test_energy_can_be_asked_for():
    assert build_detector(AudioConfig(vad="energy")).name == "energy"


def test_an_unknown_detector_is_refused():
    with pytest.raises(ValueError, match="Unknown vad"):
        build_detector(AudioConfig(vad="webrtc"))


def test_auto_falls_back_when_silero_will_not_load(monkeypatch):
    def explode(*_args, **_kwargs):
        raise RuntimeError("no onnxruntime")

    monkeypatch.setattr("jarvis.vad.SileroDetector.__init__", explode)
    assert build_detector(AudioConfig(vad="auto")).name == "energy"
    with pytest.raises(RuntimeError):
        build_detector(AudioConfig(vad="silero"))


# ------------------------------------------------------------------- energy


def test_the_energy_threshold_never_drifts_below_the_floor():
    detector = EnergyDetector(AudioConfig(min_energy_threshold=80))
    quiet = np.full(SAMPLES, 1, dtype=np.int16).tobytes()
    for _ in range(200):
        detector.is_speech(quiet)
    assert detector.threshold == 80


def test_the_energy_threshold_chases_the_noise():
    detector = EnergyDetector(AudioConfig(min_energy_threshold=80))
    noisy = np.full(SAMPLES, 400, dtype=np.int16).tobytes()
    for _ in range(400):
        detector.is_speech(noisy)
    assert 500 < detector.threshold < 700, "roughly 1.5x the ambient level"


def test_a_fixed_threshold_stops_it_adapting():
    detector = EnergyDetector(AudioConfig(energy_threshold=250))
    quiet = np.full(SAMPLES, 1, dtype=np.int16).tobytes()
    for _ in range(200):
        detector.is_speech(quiet)
    assert detector.threshold == 250
