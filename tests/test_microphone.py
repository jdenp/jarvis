"""Phrase splitting, queueing and mute behaviour, without touching hardware.

FakeSource drives the real capture loop off a written-out pattern of loud and
quiet buffers, so `_run` is covered without a microphone. These use the energy
detector, since a square wave is loud but is not speech - see test_vad.py.
"""

from __future__ import annotations

import numpy as np

from jarvis.config import AudioConfig
from jarvis.microphone import Microphone, PhraseEnd
from jarvis.vad import SAMPLE_RATE, SAMPLES, SECONDS_PER_BUFFER

PER_BUFFER = SECONDS_PER_BUFFER  # 0.032s

LOUD = 3000
QUIET = 5


def make_buffer(amplitude: int) -> bytes:
    return np.full(SAMPLES, amplitude, dtype=np.int16).tobytes()


class FakeSource:
    """A source whose stream plays back a pattern: '#' loud, '.' quiet."""

    CHUNK = SAMPLES
    SAMPLE_RATE = SAMPLE_RATE
    SAMPLE_WIDTH = 2

    def __init__(self, pattern: str) -> None:
        self.stream = self
        self._buffers = [make_buffer(LOUD if char == "#" else QUIET) for char in pattern]
        self._index = 0

    def read(self, _size: int) -> bytes:
        if self._index >= len(self._buffers):
            return b""  # end of stream, which stops the loop
        buffer = self._buffers[self._index]
        self._index += 1
        return buffer


def make_mic(**overrides) -> Microphone:
    settings = {"vad": "energy", "energy_threshold": 1000.0, "pause_threshold": 1.5}
    settings.update(overrides)
    return Microphone(AudioConfig(**settings))


def run(mic: Microphone, pattern: str) -> list[float]:
    """Play a pattern through the capture loop, returning phrase durations."""
    source = FakeSource(pattern)
    mic._running.set()
    mic._run(source)
    durations = []
    while (audio := mic.listen(timeout=0)) is not None:
        durations.append(len(audio.frame_data) / (SAMPLE_RATE * 2))
    return durations


SILENCE = "." * 60  # comfortably longer than a 1.5s pause at 32ms a buffer


# --------------------------------------------------------------- PhraseEnd


def test_the_fraction_does_not_shorten_the_pause_itself():
    """The trap: requiring a fraction of a fixed window silently cuts
    pause_threshold, so 1.5s of patience became 1.28s. The window widens
    instead, and a clean pause still has to run the full length."""
    strict = PhraseEnd(PER_BUFFER, pause_threshold=1.5, quiet_fraction=1.0)
    lenient = PhraseEnd(PER_BUFFER, pause_threshold=1.5, quiet_fraction=0.85)
    assert strict.needed == lenient.needed == 47, "1.5s at 32ms a buffer"
    assert (strict.window, lenient.window) == (47, 56)

    for _ in range(46):
        assert lenient.feed(True) is False
    assert lenient.feed(True) is True


def test_intermittent_noise_no_longer_holds_the_phrase_open():
    """The bug this guards: speech_recognition resets its pause count on any
    single buffer above the threshold, so one click per second means the phrase
    only ever ends at phrase_time_limit."""
    end = PhraseEnd(PER_BUFFER, pause_threshold=1.5, quiet_fraction=0.85)
    finished = False
    for i in range(end.window * 2):
        finished = end.feed(i % 8 != 0)
    assert finished, "one loud buffer in eight should still count as a pause"


def test_talking_through_the_window_does_not_end_the_phrase():
    end = PhraseEnd(PER_BUFFER, pause_threshold=1.5, quiet_fraction=0.85)
    for i in range(end.window * 2):
        assert end.feed(bool(i % 2)) is False


def test_the_trailing_trim_never_reaches_back_into_speech():
    """Trimmed frames are lost outright - the next phrase starts from scratch -
    so only genuinely silent buffers may be dropped."""
    end = PhraseEnd(PER_BUFFER, pause_threshold=1.5, quiet_fraction=0.85)
    for i in range(end.window):
        end.feed(i != end.window - 4)
    assert end.trailing_quiet == 3, "a word 3 buffers from the end stays put"


# ------------------------------------------------------------- capture loop


def test_speech_followed_by_silence_becomes_one_phrase():
    assert len(run(make_mic(), "#" * 20 + SILENCE)) == 1


def test_speech_followed_by_intermittent_noise_still_ends():
    """The user-visible bug: say something, then background noise, and the
    sentence does not reach the agent until the phrase time limit expires."""
    noisy = "".join("#" if i % 8 == 0 else "." for i in range(90))
    durations = run(make_mic(), "#" * 20 + noisy)
    assert len(durations) == 1
    assert durations[0] < 3.5, "ended during the noise, not at the end of it"


def test_continuous_noise_is_bounded_by_the_phrase_time_limit():
    durations = run(make_mic(phrase_time_limit=1.0), "#" * 80)
    assert durations, "the limit has to end the phrase, nothing else will"
    assert durations[0] <= 1.2


def test_a_click_is_too_short_to_be_a_phrase():
    assert run(make_mic(), "." * 5 + "#" + SILENCE) == []


def test_two_sentences_come_back_separately():
    assert len(run(make_mic(), "#" * 20 + SILENCE + "#" * 20 + SILENCE)) == 2


def test_audio_recorded_while_muted_is_dropped():
    mic = make_mic()
    mic.mute()
    assert run(mic, "#" * 20 + SILENCE) == []


def test_the_echo_guard_drops_audio_recorded_just_after_speaking():
    mic = make_mic(echo_guard_seconds=30.0)
    mic.mute()
    mic.unmute()  # speech finished, but the guard is still running
    assert run(mic, "#" * 20 + SILENCE) == []


def test_capture_resumes_once_the_guard_has_passed():
    mic = make_mic(echo_guard_seconds=0.0)
    mic.mute()
    mic.unmute()
    assert len(run(mic, "#" * 20 + SILENCE)) == 1


def test_queue_overflow_drops_rather_than_blocking():
    mic = make_mic()
    durations = run(mic, ("#" * 20 + SILENCE) * 30)
    assert len(durations) == mic._queue.maxsize


# ------------------------------------------------------------------ wiring


def test_silero_is_the_default_and_needs_no_calibration():
    mic = Microphone(AudioConfig())
    assert mic.detector.name == "silero"
    assert mic.detector.calibrates is False


def test_energy_mode_still_calibrates():
    mic = make_mic(energy_threshold=None, min_energy_threshold=80)
    assert mic.detector.calibrates is True
    mic.detector.calibrate(18)  # what a very quiet room measures
    assert mic.energy_threshold == 80


def test_a_device_at_the_wrong_rate_is_called_out(caplog):
    """Silero assumes 16 kHz. speech_recognition opens at the device default,
    which was 44100 here, so frames were not the length it thought."""

    class Wrong(FakeSource):
        SAMPLE_RATE = 44_100

    mic = Microphone(AudioConfig())  # silero
    mic._running.set()
    mic._run(Wrong("." * 10))
    assert "44100 Hz" in caplog.text


def test_stop_is_safe_before_start():
    make_mic().stop()
