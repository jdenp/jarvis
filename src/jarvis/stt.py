"""Speech to text.

``whisper`` is the default and runs faster-whisper on this machine - nothing
leaves it. ``google`` is the free web endpoint that ships with
speech_recognition, which needs no model download but uploads your raw
microphone audio to Google. It is opt in, and says so loudly when selected.
"""

from __future__ import annotations

import logging
from typing import Protocol

import speech_recognition as sr

from .config import SttConfig

logger = logging.getLogger("jarvis.stt")

WHISPER_SAMPLE_RATE = 16_000


class Transcriber(Protocol):
    """Anything that turns captured audio into text."""

    def transcribe(self, audio: sr.AudioData) -> str | None: ...

    @property
    def is_local(self) -> bool: ...


class WhisperSTT:
    """Local transcription with faster-whisper.

    The model is loaded once and reused. speech_recognition's own
    ``recognize_faster_whisper`` builds a fresh ``WhisperModel`` on every call,
    which means reloading weights from disk for each utterance.
    """

    is_local = True

    def __init__(self, config: SttConfig | None = None) -> None:
        self.config = config or SttConfig()
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise ImportError("The whisper backend needs faster-whisper. Run `uv sync`.") from exc

        self._model = self._load(WhisperModel)

    def _candidates(self) -> list[tuple[str, str]]:
        """(device, compute_type) pairs to try, best first."""
        device = self.config.whisper_device.strip().lower()
        compute = self.config.whisper_compute_type
        if device == "auto":
            return [("cuda", compute), ("cpu", "int8")]
        return [(device, compute)]

    def _load(self, whisper_model_cls):
        """Load the model, proving each device works before accepting it.

        Constructing a WhisperModel on a broken CUDA install succeeds - the
        failure only appears on the first inference, because transcribe()
        returns a generator and does no work until it is consumed. So warm up
        for real here rather than discovering it one utterance at a time.
        """
        failures = []
        for device, compute in self._candidates():
            try:
                model = whisper_model_cls(
                    self.config.whisper_model, device=device, compute_type=compute
                )
                self._warm_up(model)
            except Exception as exc:
                failures.append(f"{device}: {exc}")
                logger.warning("Whisper is not usable on %s (%s).", device, exc)
                continue
            logger.info(
                "Whisper model %s ready on %s (%s).", self.config.whisper_model, device, compute
            )
            return model
        raise RuntimeError("Whisper could not start on any device - " + "; ".join(failures))

    @staticmethod
    def _warm_up(model) -> None:
        """Run one real inference so a broken backend fails here, not later."""
        import numpy as np

        rng = np.random.default_rng(0)
        noise = (rng.standard_normal(WHISPER_SAMPLE_RATE) * 0.01).astype(np.float32)
        segments, _info = model.transcribe(noise, language="en", vad_filter=False, beam_size=1)
        list(segments)  # the generator is where the work, and the failure, happens

    def transcribe(self, audio: sr.AudioData) -> str | None:
        try:
            samples = _to_float32(audio)
        except Exception as exc:
            logger.error("Could not convert captured audio - %s", exc)
            return None

        try:
            segments, _info = self._model.transcribe(
                samples,
                language=self.config.language[:2].lower() or None,
                beam_size=self.config.whisper_beam_size,
                vad_filter=self.config.whisper_vad,
                condition_on_previous_text=False,
            )
            text = " ".join(segment.text.strip() for segment in segments).strip()
        except Exception as exc:
            logger.error("Local transcription failed - %s", exc)
            return None
        return text or None


class GoogleSTT:
    """Google's free Speech Recognition web API. Uploads your audio."""

    is_local = False

    def __init__(self, config: SttConfig | None = None) -> None:
        self.config = config or SttConfig()
        self._recognizer = sr.Recognizer()
        logger.warning(
            "Speech to text is set to 'google' - every captured phrase is uploaded to "
            'Google. Set stt.backend = "whisper" to keep audio on this machine.'
        )

    def transcribe(self, audio: sr.AudioData) -> str | None:
        try:
            text = self._recognizer.recognize_google(audio, language=self.config.language)
        except sr.UnknownValueError:
            logger.debug("Audio was not intelligible.")
            return None
        except sr.RequestError as exc:
            logger.error("Speech service unavailable - %s", exc)
            return None
        text = text.strip()
        return text or None


def _to_float32(audio: sr.AudioData):
    """Whisper wants 16 kHz mono float32 in [-1, 1]."""
    import numpy as np

    raw = audio.get_raw_data(convert_rate=WHISPER_SAMPLE_RATE, convert_width=2)
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def build_transcriber(config: SttConfig | None = None) -> Transcriber:
    """Construct the transcriber named by ``config.backend``."""
    config = config or SttConfig()
    backends = {"whisper": WhisperSTT, "google": GoogleSTT}
    try:
        factory = backends[config.backend]
    except KeyError:
        raise ValueError(
            f"Unknown STT backend {config.backend!r}. Choose from {sorted(backends)}."
        ) from None
    return factory(config)
