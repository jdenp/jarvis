"""Phrase splitting, queueing and mute behaviour, without touching hardware.

FakeSource drives the real capture loop off a written-out pattern of loud and
quiet buffers, so `_run` is covered without a microphone. These use the energy
detector, since a square wave is loud but is not speech - see test_vad.py.
"""

from __future__ import annotations

import queue
import threading
import time

import numpy as np

from jarvis.config import AudioConfig
from jarvis.microphone import Microphone, PhraseEnd, RemoteStream
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


def test_audio_recorded_while_paused_is_never_captured():
    """Paused used to gate only delivery, so the phrase was still recorded,
    still transcribed and still written to heard.jsonl."""
    mic = make_mic()
    mic.pause()
    assert run(mic, "#" * 20 + SILENCE) == []


def test_resuming_starts_capturing_again():
    mic = make_mic()
    mic.pause()
    mic.resume()
    assert len(run(mic, "#" * 20 + SILENCE)) == 1


def test_pausing_drops_a_phrase_already_waiting():
    """A phrase landing a second after the key is pressed is the surprise this
    is meant to remove."""
    mic = make_mic()
    assert len(run(mic, "#" * 20 + SILENCE)) == 1  # something is queued
    run(mic, "#" * 20 + SILENCE)
    mic.pause()
    assert mic.listen(timeout=0) is None


def test_unmuting_after_a_reply_does_not_lift_a_pause():
    """say() ends in unmute(). One shared flag would have a finished reply
    quietly start listening again."""
    mic = make_mic(echo_guard_seconds=0.0)
    mic.pause()
    mic.mute()
    mic.unmute()
    assert mic.paused is True
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


def test_a_short_answer_is_not_thrown_away():
    """The regression this guards: min_speech_seconds counts real speech now, so
    0.3s dropped "No." and "Stop." without trace - the worst word to lose."""
    short = "#" * 6 + SILENCE  # 0.19s of speech
    assert len(run(make_mic(min_speech_seconds=0.15), short)) == 1
    assert run(make_mic(min_speech_seconds=0.3), short) == []


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


# --------------------------------------------------------- audio off a network


def test_what_was_written_comes_back_a_buffer_at_a_time():
    """`_run` reads CHUNK frames at a time, and the network arrives in whatever
    size the page felt like sending."""
    stream = RemoteStream()
    stream.write(make_buffer(LOUD) + make_buffer(QUIET))

    assert stream.read(SAMPLES) == make_buffer(LOUD)
    assert stream.read(SAMPLES) == make_buffer(QUIET)


def test_a_gap_in_the_network_reads_as_silence_not_as_the_end():
    """A read returning nothing stops the capture loop for good, and a phone
    going quiet for a moment is not the end of anything."""
    stream = RemoteStream(idle_seconds=30)
    stream.write(make_buffer(LOUD))
    stream.read(SAMPLES)

    assert stream.read(SAMPLES) == bytes(SAMPLES * 2), "silence, so the phrase can end"
    assert stream.live, "still connected, just not talking"


def test_a_stream_that_stays_quiet_goes_back_to_sleep():
    """Otherwise Silero scores silence forever on behalf of a phone that went
    into somebody's pocket an hour ago."""
    stream = RemoteStream(idle_seconds=0.05)
    stream.write(make_buffer(LOUD))
    stream.read(SAMPLES)
    assert stream.live

    time.sleep(0.1)
    threading.Timer(0.3, stream.close).start()
    assert stream.read(SAMPLES) == b"", "asleep until closed, rather than filling silence"
    assert not stream.live


def test_closing_it_ends_the_capture_loop():
    stream = RemoteStream()
    stream.close()
    assert stream.read(SAMPLES) == b""


def test_a_remote_source_delivers_into_a_queue_it_was_handed():
    """Which is the whole trick: the service reads one queue and never learns
    that any of it came from a phone."""
    together: queue.Queue = queue.Queue(maxsize=16)
    phone = make_mic()
    desk = make_mic()
    phone._queue = together
    assert desk.sink is not together, "the desk keeps its own unless one is handed to it"

    stream = RemoteStream()
    # One blob, the way the page sends it: a quarter second at a time, which is
    # eight buffers' worth and nothing to do with where a phrase begins.
    pattern = "#" * 20 + SILENCE
    stream.write(b"".join(make_buffer(LOUD if char == "#" else QUIET) for char in pattern))
    stream.close()
    phone._running.set()
    phone._run(stream)

    assert together.qsize() == 1


def test_a_source_that_was_handed_over_is_not_a_device():
    """Nothing to open and nothing to calibrate, and it survives a stop so the
    same stream can be listened to again."""
    stream = RemoteStream()
    phone = Microphone(AudioConfig(), source=stream, sink=queue.Queue())
    phone.start()
    phone.stop()
    assert phone._source is stream


def test_a_deferred_microphone_captures_nothing():
    """Two live microphones in one room hear the same sentence twice, and the
    second copy arrives as a follow-up question nobody asked."""
    mic = make_mic()
    mic.defer(True)
    assert run(mic, "#" * 20 + SILENCE) == []

    mic.defer(False)
    assert len(run(mic, "#" * 20 + SILENCE)) == 1


def test_taking_the_floor_back_does_not_undo_a_pause_or_a_mute():
    """It is neither the echo gate nor a pause anybody asked for, so it has to
    keep out of the way of both on its way in and out."""
    mic = make_mic()
    mic.pause()
    mic.defer(True)
    mic.defer(False)
    assert mic.paused

    mic = make_mic()
    mic.mute()
    mic.defer(True)
    mic.defer(False)
    assert run(mic, "#" * 20 + SILENCE) == [], "still muted"
