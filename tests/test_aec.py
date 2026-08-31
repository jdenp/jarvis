"""Taking this machine's own noise back out of its own microphone.

The cancelling itself is WebRTC's and is not retested here. What is tested is
everything around it: that a buffer comes back the size it went in, that the
capture loop is never brought down by any of this being missing or broken, and
that a reference which has drifted is trimmed rather than believed.
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from jarvis.aec import DEFAULT_BACKLOG, Canceller, Reference, available, canceller
from jarvis.config import AudioConfig

RATE = 16000


class Silence:
    """A reference for a machine that is playing nothing."""

    def __init__(self, rate: int = RATE) -> None:
        self.rate = rate
        self.asked = 0

    def take(self, count: int) -> np.ndarray:
        self.asked += count
        return np.zeros(count, np.float32)

    def stop(self) -> None:
        self.stopped = True


def pcm(samples: np.ndarray) -> bytes:
    return (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()


def unpcm(buffer: bytes) -> np.ndarray:
    return np.frombuffer(buffer, "<i2").astype(np.float32) / 32768.0


needs_webrtc = pytest.mark.skipif(bool(available()), reason=available() or "installed")


# ------------------------------------------------------- deciding versus keeping


class Always:
    """A canceller whose output is fixed, whatever it is handed."""

    def __init__(self, amplitude: int) -> None:
        from test_microphone import make_buffer

        self.reply = make_buffer(amplitude)
        self.given: list[bytes] = []

    def clean(self, buffer: bytes) -> bytes:
        self.given.append(buffer)
        return self.reply

    def stop(self) -> None:
        self.stopped = True


def capture(monkeypatch, cancelling, pattern="." * 3 + "#" * 20 + "." * 60):
    """Run the real capture loop with a canceller of our choosing."""
    import jarvis.aec
    from test_microphone import make_mic, run

    monkeypatch.setattr(jarvis.aec, "canceller", lambda config, rate, width: cancelling)
    return run(make_mic(echo_cancellation=True), pattern)


def test_the_speakers_never_open_a_phrase(monkeypatch):
    """The whole point. A video playing is loud in the microphone and silent
    once it has been cancelled, so nothing is recorded and nothing is sent."""
    it = Always(5)  # cancelled down to nothing
    assert capture(monkeypatch, it) == [], "loud buffers, but nothing survived cancelling"
    assert it.given, "and the canceller was actually consulted"


def test_what_is_kept_is_the_cancelled_audio(monkeypatch):
    """It was the raw buffer for an afternoon, because cancelling costs about
    twenty points of word error on somebody talking over the speakers. But the
    raw buffer still has the speakers in it, and Whisper wrote down part of a
    video from one. Muddled is recoverable; out of nowhere is not.

    The canceller here disagrees with the microphone on purpose - it calls the
    quiet buffers loud - so the audio that comes out says which one was kept.
    """
    import jarvis.aec
    from test_microphone import LOUD, QUIET, FakeSource, make_buffer, make_mic

    # A third amplitude, so the canceller's output can never be mistaken for
    # the microphone's: quiet enough to end a phrase, and not QUIET itself.
    HUSHED = 900

    class Invert:
        def clean(self, buffer: bytes) -> bytes:
            return make_buffer(LOUD if buffer == make_buffer(QUIET) else HUSHED)

        def stop(self) -> None:
            pass

    monkeypatch.setattr(jarvis.aec, "canceller", lambda config, rate, width: Invert())
    mic = make_mic(echo_cancellation=True)
    mic._running.set()
    mic._run(FakeSource("#" * 3 + "." * 25 + "#" * 60))

    audio = mic.listen(timeout=0)
    assert audio is not None, "the gate opened on what the canceller called speech"
    assert make_buffer(QUIET) not in audio.frame_data, "no raw buffer survived"
    assert make_buffer(LOUD) in audio.frame_data, "what the canceller returned did"


def test_the_canceller_is_closed_when_capture_stops(monkeypatch):
    it = Always(5)
    capture(monkeypatch, it)
    assert getattr(it, "stopped", False), "the loopback stream does not outlive the microphone"


def test_a_phone_is_not_cancelled_against_these_speakers(monkeypatch):
    """The web app's microphone is in whatever room the phone is in, and these
    speakers are not its echo."""
    import jarvis.aec
    from jarvis.microphone import Microphone
    from test_microphone import FakeSource

    asked = []
    monkeypatch.setattr(
        jarvis.aec, "canceller", lambda config, rate, width: asked.append(rate) or None
    )
    phone = Microphone(replace(AudioConfig(), echo_cancellation=True), source=FakeSource("." * 5))
    phone._running.set()
    phone._run(phone._source)
    assert asked == [], "it was never even asked for one"


# ------------------------------------------------------------------ switched off


def test_it_is_off_unless_asked_for():
    assert canceller(AudioConfig(), RATE, 2) is None


def test_a_device_that_is_not_16_bit_is_left_alone():
    """Everything here reads and writes int16. A device that does not is a
    device this cannot help, and saying so beats mangling its audio."""
    config = replace(AudioConfig(), echo_cancellation=True)
    assert canceller(config, RATE, 4) is None


def test_nothing_here_can_stop_the_microphone_opening(monkeypatch):
    """Every way this fails leaves a JARVIS that hears its own speakers, which
    is where it started. One that will not open the microphone is worse."""
    import jarvis.aec

    def explode(rate):
        raise RuntimeError("no loopback device on this machine")

    monkeypatch.setattr(jarvis.aec, "Reference", explode)
    config = replace(AudioConfig(), echo_cancellation=True)
    assert canceller(config, RATE, 2) is None


# --------------------------------------------------------------- buffers in, out


@needs_webrtc
def test_a_buffer_comes_back_the_size_it_went_in():
    """Nothing downstream is told this happened, so the phrase splitter has to
    keep counting buffers the way it always did."""
    it = Canceller(Silence(), RATE)
    for size in (512, 512, 160, 1024, 480, 512):
        given = pcm(np.random.default_rng(size).normal(0, 0.05, size).astype(np.float32))
        assert len(it.clean(given)) == len(given)


@needs_webrtc
def test_a_machine_playing_nothing_does_not_silence_the_microphone():
    """The ordinary case by a wide margin, and not a passthrough: there is a
    noise suppressor in here as well as a canceller. On real speech in a quiet
    room it costs about 1.5% word error, which is a word in a long paragraph.
    What it must never do is decide a person is noise and hand back nothing.
    """
    rng = np.random.default_rng(7)
    sound = np.concatenate(
        [np.sin(2 * np.pi * f * np.arange(RATE // 4) / RATE) * 0.2 for f in (180, 320, 240, 400)]
    ).astype(np.float32) + rng.normal(0, 0.01, RATE).astype(np.float32)

    it = Canceller(Silence(), RATE)
    out = np.concatenate(
        [unpcm(it.clean(pcm(sound[at : at + 512]))) for at in range(0, len(sound) - 512, 512)]
    )
    kept = float(np.sum(out**2)) / float(np.sum(sound[: len(out)] ** 2))
    assert kept > 0.1, f"most of it should still be there, kept {kept:.2f}"


@needs_webrtc
def test_the_reference_is_asked_for_exactly_what_it_is_given():
    it = Canceller(reference := Silence(), RATE)
    for _ in range(10):
        it.clean(pcm(np.zeros(512, np.float32)))
    assert reference.asked == 10 * 512 // it.frame * it.frame


# ------------------------------------------------------------------------- gate


class Playing:
    """A reference for a machine with something on the speakers."""

    def __init__(self, level: float = 0.05) -> None:
        self.rate = RATE
        self.level = level

    def take(self, count: int) -> np.ndarray:
        rng = np.random.default_rng(count)
        return (rng.normal(0, self.level, count)).astype(np.float32)

    def stop(self) -> None:
        pass


@needs_webrtc
def test_the_gate_does_nothing_when_the_speakers_are_off():
    """Most of the time nothing is playing, and then there is no echo for a
    voice to be confused with. The gate has to be invisible there."""
    quiet = Canceller(Silence(), RATE, margin=6.0)
    loud = Canceller(Silence(), RATE, margin=0.0)
    speech = (np.random.default_rng(3).normal(0, 0.05, RATE * 2)).astype(np.float32)
    for at in range(0, len(speech) - 512, 512):
        given = pcm(speech[at : at + 512])
        assert quiet.clean(given) == loud.clean(given), "the gate changed nothing"


@needs_webrtc
def test_what_sits_at_the_echo_floor_is_treated_as_echo():
    """Cancelling is a subtraction and cannot touch what the speakers add
    themselves, so what survives is judged as well. Anything not clearly above
    the quietest recent residual is more echo."""
    it = Canceller(Playing(), RATE, margin=3.0)
    residual = np.zeros(0, np.float32)
    for _ in range(300):  # three seconds, enough to learn a floor
        out = it.clean(pcm(np.random.default_rng(1).normal(0, 0.002, 512).astype(np.float32)))
        residual = np.concatenate([residual, unpcm(out)])
    assert np.sqrt((residual[-RATE:] ** 2).mean()) == 0.0, "steady residual is gated away"


@needs_webrtc
def test_a_voice_over_the_top_is_loud_enough_to_get_through():
    it = Canceller(Playing(), RATE, margin=3.0)
    rng = np.random.default_rng(5)
    kept = np.zeros(0, np.float32)
    for n in range(300):
        # Quiet for a second, so a floor is learned, then somebody speaks.
        level = 0.002 if n < 100 else 0.08
        out = it.clean(pcm(rng.normal(0, level, 512).astype(np.float32)))
        if n > 150:
            kept = np.concatenate([kept, unpcm(out)])
    assert np.sqrt((kept**2).mean()) > 0.005, "the loud half was not gated away"


def test_the_gate_can_be_turned_off():
    config = replace(AudioConfig(), echo_cancellation=True, echo_gate_margin=0.0)
    assert config.echo_gate_margin == 0.0


# ----------------------------------------------------------------- the reference


def held(*blocks: np.ndarray) -> Reference:
    """A Reference with audio already in it and no device behind it."""
    import threading

    it = Reference.__new__(Reference)
    it.rate = RATE
    it.delay = DEFAULT_BACKLOG
    it._lock = threading.Lock()
    it._arriving = threading.Condition(it._lock)
    it._last = 0.0
    it._spare = np.zeros(0, np.float32)
    from collections import deque

    it._held = deque(blocks)
    return it


def test_the_reference_hands_back_what_it_was_given_in_order():
    it = held(np.arange(100, dtype=np.float32), np.arange(100, 200, dtype=np.float32))
    assert list(it.take(150)) == list(np.arange(150, dtype=np.float32))
    assert list(it.take(50)) == list(np.arange(150, 200, dtype=np.float32))


def test_a_reference_that_has_run_out_is_silence_rather_than_a_short_read():
    """The microphone cannot wait: a frame has to be cancelled against
    something, and silence cancels nothing rather than cancelling wrongly."""
    it = held(np.ones(10, np.float32))
    out = it.take(100)
    assert len(out) == 100
    assert list(out[10:]) == [0.0] * 90


def test_a_reference_that_has_drifted_ahead_is_trimmed():
    """Two clocks, hours apart. Without this the canceller ends up subtracting
    what the speakers played half a second ago, which is worse than nothing."""
    plenty = int(DEFAULT_BACKLOG * RATE) * 3
    it = held(*[np.full(1000, n, np.float32) for n in range(plenty // 1000)])
    out = it.take(100)
    assert len(out) == 100
    assert out[0] > 0, "it kept the newest audio, not the oldest"
    with it._lock:
        left = sum(len(block) for block in it._held) + len(it._spare)
    assert left <= DEFAULT_BACKLOG * RATE
