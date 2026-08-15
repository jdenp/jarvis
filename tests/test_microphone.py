"""Queue, mute and echo-guard behaviour, without touching real hardware."""

from __future__ import annotations

from dataclasses import dataclass

from jarvis.config import AudioConfig
from jarvis.microphone import Microphone, phrase_duration


@dataclass
class FakeAudio:
    """Enough of sr.AudioData to compute a duration."""

    seconds: float = 0.1
    label: str = ""
    sample_rate: int = 16_000
    sample_width: int = 2

    @property
    def frame_data(self) -> bytes:
        return b"\x00" * int(self.seconds * self.sample_rate * self.sample_width)


def make_mic(**overrides) -> Microphone:
    return Microphone(AudioConfig(**overrides))


def test_phrase_duration_matches_the_frame_count():
    assert phrase_duration(FakeAudio(seconds=2.5)) == 2.5
    assert phrase_duration(FakeAudio(seconds=0)) == 0.0


def test_captured_audio_is_queued_in_order():
    mic = make_mic()
    one, two = FakeAudio(label="one"), FakeAudio(label="two")
    mic._on_audio(None, one)
    mic._on_audio(None, two)
    assert mic.listen(timeout=0) is one
    assert mic.listen(timeout=0) is two
    assert mic.listen(timeout=0) is None


def test_muted_audio_is_discarded():
    mic = make_mic()
    mic.mute()
    mic._on_audio(None, FakeAudio())
    assert mic.listen(timeout=0) is None


def test_unmute_drops_anything_captured_while_muted():
    mic = make_mic()
    mic._on_audio(None, FakeAudio())
    mic.mute()
    mic.unmute()
    assert mic.listen(timeout=0) is None


def test_phrase_recorded_while_speaking_is_dropped_even_if_it_arrives_after_unmute():
    """The bug this guards: listen_in_background only hands over a phrase once
    it ends, so JARVIS's own voice arrives just after unmute clears the flag."""
    mic = make_mic(echo_guard_seconds=0.5)
    mic.mute()
    mic.unmute()  # speech finished, flag is clear again
    # A four second phrase delivered now must have started while JARVIS spoke.
    mic._on_audio(None, FakeAudio(seconds=4.0, label="jarvis hearing itself"))
    assert mic.listen(timeout=0) is None


def test_a_phrase_recorded_after_the_guard_gets_through():
    mic = make_mic(echo_guard_seconds=0.0)
    mic.mute()
    mic.unmute()
    fresh = FakeAudio(seconds=0.0, label="the user speaking")
    mic._on_audio(None, fresh)
    assert mic.listen(timeout=0) is fresh


def test_echo_guard_is_not_armed_before_jarvis_has_spoken():
    mic = make_mic()
    long_phrase = FakeAudio(seconds=9.0)
    mic._on_audio(None, long_phrase)
    assert mic.listen(timeout=0) is long_phrase


def test_queue_overflow_drops_rather_than_blocking():
    mic = make_mic()
    for i in range(50):
        mic._on_audio(None, FakeAudio(label=f"phrase {i}"))
    drained = []
    while (item := mic.listen(timeout=0)) is not None:
        drained.append(item)
    assert len(drained) == mic._queue.maxsize
    assert drained[0].label == "phrase 0"


def test_a_silent_room_does_not_leave_the_mic_hypersensitive():
    mic = make_mic(min_energy_threshold=80)
    mic._recognizer.energy_threshold = 18  # what a very quiet room calibrates to
    mic._apply_threshold_floor()
    assert mic.energy_threshold == 80


def test_a_normal_room_keeps_its_measured_threshold():
    mic = make_mic(min_energy_threshold=80)
    mic._recognizer.energy_threshold = 340
    mic._apply_threshold_floor()
    assert mic.energy_threshold == 340


def test_fixed_threshold_disables_dynamic_energy():
    mic = make_mic(energy_threshold=250)
    assert mic.energy_threshold == 250
    assert mic._recognizer.dynamic_energy_threshold is False


def test_stop_is_safe_before_start():
    make_mic().stop()
