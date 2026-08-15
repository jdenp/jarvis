"""Backend selection and device fallback, with a stub in place of the real model."""

from __future__ import annotations

from typing import ClassVar

import pytest

from jarvis.config import SttConfig
from jarvis.stt import GoogleSTT, WhisperSTT, build_transcriber


class StubModel:
    """Stands in for faster_whisper.WhisperModel."""

    instances: ClassVar[list[StubModel]] = []

    def __init__(self, name, device="cpu", compute_type="default", broken_devices=()):
        self.name = name
        self.device = device
        self.compute_type = compute_type
        self._broken = broken_devices
        self.transcribe_calls = 0
        StubModel.instances.append(self)

    def transcribe(self, samples, **kwargs):
        self.transcribe_calls += 1

        def segments():
            # Mirrors faster-whisper: nothing runs until the generator is consumed,
            # which is exactly why a broken device survives construction.
            if self.device in self._broken:
                raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
            yield type("Segment", (), {"text": " hello there "})()

        return segments(), None


def stub_factory(broken_devices=()):
    def factory(name, device="cpu", compute_type="default"):
        return StubModel(name, device, compute_type, broken_devices)

    return factory


@pytest.fixture(autouse=True)
def clear_instances():
    StubModel.instances.clear()


def build(config: SttConfig, broken=()) -> WhisperSTT:
    stt = WhisperSTT.__new__(WhisperSTT)
    stt.config = config
    stt._model = stt._load(stub_factory(broken))
    return stt


def test_auto_prefers_cuda_when_it_works():
    stt = build(SttConfig(whisper_device="auto"))
    assert stt._model.device == "cuda"


def test_auto_falls_back_when_cuda_only_fails_on_inference():
    stt = build(SttConfig(whisper_device="auto"), broken=("cuda",))
    assert stt._model.device == "cpu"
    assert stt._model.compute_type == "int8"
    assert [m.device for m in StubModel.instances] == ["cuda", "cpu"]


def test_explicit_device_is_not_silently_replaced():
    with pytest.raises(RuntimeError, match="could not start on any device"):
        build(SttConfig(whisper_device="cuda"), broken=("cuda",))


def test_model_is_loaded_once_and_reused():
    stt = build(SttConfig(whisper_device="cpu"))
    warm_up_calls = stt._model.transcribe_calls
    for _ in range(3):
        stt.transcribe(_FakeAudio())
    assert len(StubModel.instances) == 1
    assert stt._model.transcribe_calls == warm_up_calls + 3


def test_transcription_strips_and_joins_segments():
    stt = build(SttConfig(whisper_device="cpu"))
    assert stt.transcribe(_FakeAudio()) == "hello there"


def test_inference_failure_returns_none_rather_than_raising():
    stt = build(SttConfig(whisper_device="cpu"))
    stt._model._broken = ("cpu",)
    assert stt.transcribe(_FakeAudio()) is None


class _FakeAudio:
    def get_raw_data(self, convert_rate=None, convert_width=None):
        return b"\x00\x01" * 100


def test_backends_declare_whether_they_are_local():
    assert WhisperSTT.is_local is True
    assert GoogleSTT.is_local is False


def test_unknown_backend_is_rejected():
    with pytest.raises(ValueError, match="Unknown STT backend"):
        build_transcriber(SttConfig(backend="carrier-pigeon"))
