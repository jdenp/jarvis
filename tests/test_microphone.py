"""Phrase splitting, queueing and mute behaviour, without touching hardware.

FakeSource drives the real capture loop off a written-out pattern of loud and
quiet buffers, so `_run` is covered without a microphone.
"""

from __future__ import annotations

import numpy as np

from jarvis.config import AudioConfig
from jarvis.microphone import Microphone, PhraseEnd, buffer_energy

CHUNK = 1024
RATE = 16_000
PER_BUFFER = CHUNK / RATE  # 0.064s

LOUD = 3000
QUIET = 5


def make_buffer(amplitude: int) -> bytes:
    return np.full(CHUNK, amplitude, dtype=np.int16).tobytes()


class FakeSource:
    """A source whose stream plays back a pattern: '#' loud, '.' quiet."""

    CHUNK = CHUNK
    SAMPLE_RATE = RATE
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
    # A fixed threshold so the pattern means what it says, and dynamic
    # adjustment is exercised on its own below.
    settings = {"energy_threshold": 1000.0, "pause_threshold": 1.5}
    settings.update(overrides)
    return Microphone(AudioConfig(**settings))


def run(mic: Microphone, pattern: str) -> list[float]:
    """Play a pattern through the capture loop, returning phrase durations."""
    source = FakeSource(pattern)
    mic._running.set()
    mic._run(source)
    durations = []
    while (audio := mic.listen(timeout=0)) is not None:
        durations.append(len(audio.frame_data) / (RATE * 2))
    return durations


def test_buffer_energy_reads_the_amplitude():
    assert buffer_energy(make_buffer(3000)) == 3000
    assert buffer_energy(b"") == 0.0


# --------------------------------------------------------------- PhraseEnd


def test_the_fraction_does_not_shorten_the_pause_itself():
    """The trap: requiring a fraction of a fixed window silently cuts
    pause_threshold, so 1.5s of patience became 1.28s. The window widens
    instead, and a clean pause still has to run the full length."""
    strict = PhraseEnd(PER_BUFFER, pause_threshold=1.5, quiet_fraction=1.0)
    lenient = PhraseEnd(PER_BUFFER, pause_threshold=1.5, quiet_fraction=0.85)
    assert strict.needed == lenient.needed == 24, "1.5s at 64ms a buffer"
    assert (strict.window, lenient.window) == (24, 29)

    for _ in range(23):
        assert lenient.feed(QUIET, 1000) is False
    assert lenient.feed(QUIET, 1000) is True


def test_intermittent_noise_no_longer_holds_the_phrase_open():
    """The bug this guards: speech_recognition resets its pause count on any
    single buffer above the threshold, so one click per second means the phrase
    only ever ends at phrase_time_limit."""
    end = PhraseEnd(PER_BUFFER, pause_threshold=1.5, quiet_fraction=0.85)
    finished = False
    for i in range(end.window * 2):
        finished = end.feed(LOUD if i % 8 == 0 else QUIET, 1000)
    assert finished, "one loud buffer in eight should still count as a pause"


def test_the_trailing_trim_never_reaches_back_into_speech():
    """Trimmed frames are lost outright - the next phrase starts from scratch -
    so only genuinely silent buffers may be dropped."""
    end = PhraseEnd(PER_BUFFER, pause_threshold=1.5, quiet_fraction=0.85)
    for i in range(end.window):
        end.feed(LOUD if i == end.window - 4 else QUIET, 1000)
    assert end.trailing_quiet == 3, "a word 3 buffers from the end stays put"


def test_talking_through_the_window_does_not_end_the_phrase():
    end = PhraseEnd(PER_BUFFER, pause_threshold=1.5, quiet_fraction=0.85)
    for i in range(end.window * 2):
        assert end.feed(LOUD if i % 2 else QUIET, 1000) is False


# ------------------------------------------------------------- capture loop


def test_speech_followed_by_silence_becomes_one_phrase():
    assert len(run(make_mic(), "#" * 10 + "." * 30)) == 1


def test_speech_followed_by_intermittent_noise_still_ends():
    """The user-visible bug: say something, then background noise, and the
    sentence does not reach the agent until the phrase time limit expires."""
    noisy = "".join("#" if i % 8 == 0 else "." for i in range(60))
    durations = run(make_mic(), "#" * 10 + noisy)
    assert len(durations) == 1
    assert durations[0] < 3.5, "ended during the noise, not at the end of it"


def test_continuous_noise_is_bounded_by_the_phrase_time_limit():
    mic = make_mic(phrase_time_limit=1.0)
    durations = run(mic, "#" * 40)
    assert durations, "the limit has to end the phrase, nothing else will"
    assert durations[0] <= 1.2


def test_a_click_is_too_short_to_be_a_phrase():
    assert run(make_mic(), "." * 5 + "#" + "." * 30) == []


def test_two_sentences_come_back_separately():
    quiet = "." * 30
    assert len(run(make_mic(), "#" * 10 + quiet + "#" * 10 + quiet)) == 2


def test_audio_recorded_while_muted_is_dropped():
    mic = make_mic()
    mic.mute()
    assert run(mic, "#" * 10 + "." * 30) == []


def test_the_echo_guard_drops_audio_recorded_just_after_speaking():
    mic = make_mic(echo_guard_seconds=30.0)
    mic.mute()
    mic.unmute()  # speech finished, but the guard is still running
    assert run(mic, "#" * 10 + "." * 30) == []


def test_capture_resumes_once_the_guard_has_passed():
    mic = make_mic(echo_guard_seconds=0.0)
    mic.mute()
    mic.unmute()
    assert len(run(mic, "#" * 10 + "." * 30)) == 1


def test_queue_overflow_drops_rather_than_blocking():
    mic = make_mic()
    one = "#" * 10 + "." * 30
    durations = run(mic, one * 50)
    assert len(durations) == mic._queue.maxsize


# ------------------------------------------------------- threshold handling


def test_a_silent_room_does_not_leave_the_mic_hypersensitive():
    mic = Microphone(AudioConfig(min_energy_threshold=80))
    mic._recognizer.energy_threshold = 18  # what a very quiet room calibrates to
    mic._apply_threshold_floor()
    assert mic.energy_threshold == 80


def test_a_normal_room_keeps_its_measured_threshold():
    mic = Microphone(AudioConfig(min_energy_threshold=80))
    mic._recognizer.energy_threshold = 340
    mic._apply_threshold_floor()
    assert mic.energy_threshold == 340


def test_dynamic_adjustment_chases_the_noise_but_respects_the_floor():
    mic = Microphone(AudioConfig(min_energy_threshold=80))
    mic._recognizer.energy_threshold = 300
    for _ in range(200):
        mic._adjust(1.0, PER_BUFFER)
    assert mic.energy_threshold == 80, "a silent room must not drift below the floor"

    for _ in range(200):
        mic._adjust(400.0, PER_BUFFER)
    assert 500 < mic.energy_threshold < 700, "roughly 1.5x the ambient level"


def test_fixed_threshold_disables_dynamic_energy():
    mic = Microphone(AudioConfig(energy_threshold=250))
    assert mic.energy_threshold == 250
    assert mic._recognizer.dynamic_energy_threshold is False


def test_stop_is_safe_before_start():
    Microphone(AudioConfig()).stop()
