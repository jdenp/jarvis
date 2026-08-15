"""Runtime configuration.

Precedence, lowest to highest: dataclass defaults, ``jarvis.toml``, ``JARVIS_*``
environment variables, command line flags.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass, replace
from pathlib import Path
from typing import Any


def project_root() -> Path:
    """Directory holding jarvis.toml and logs/, overridable with JARVIS_HOME."""
    if env_home := os.environ.get("JARVIS_HOME"):
        return Path(env_home).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class AudioConfig:
    """Microphone capture settings."""

    device_index: int | None = None
    phrase_time_limit: float = 10.0
    calibration_seconds: float = 1.5
    dynamic_energy_threshold: bool = True
    energy_threshold: float | None = None
    # A very quiet room calibrates down to single digits, which is sensitive
    # enough to hear the speakers. Refuse to go below this.
    min_energy_threshold: float = 80.0
    pause_threshold: float = 0.8
    # How long after JARVIS stops talking to keep ignoring the microphone.
    echo_guard_seconds: float = 0.5


@dataclass(frozen=True)
class WakeConfig:
    """Wake word gating."""

    words: tuple[str, ...] = ("jarvis", "hey jarvis", "jervis", "travis")
    required: bool = True


@dataclass(frozen=True)
class SttConfig:
    """Speech to text. Defaults to local transcription."""

    backend: str = "whisper"  # whisper (local) | google (uploads your audio)
    language: str = "en-GB"
    whisper_model: str = "base.en"
    whisper_device: str = "auto"  # auto | cuda | cpu
    whisper_compute_type: str = "default"
    whisper_beam_size: int = 1
    whisper_vad: bool = True


@dataclass(frozen=True)
class TtsConfig:
    """Text to speech. Defaults to the offline Windows voice."""

    engine: str = "auto"  # auto (local first) | sapi | edge | none
    voice: str = "en-GB-RyanNeural"  # edge only
    # Preference order, first installed wins. George is British male but ships
    # as a OneCore voice, which SAPI only sees once it is registered - see
    # scripts/expose-onecore-voices.ps1.
    sapi_voice: str = "George, Hazel"
    rate: int = 210
    volume: float = 1.0


@dataclass(frozen=True)
class ServiceConfig:
    """The voice service an agent connects to.

    One process owns the microphone, Whisper and the speakers; the CLI and the
    MCP server are thin clients over loopback HTTP.
    """

    host: str = "127.0.0.1"
    port: int = 8770
    # Longest a wait_for_speech call may block. Keep it under the agent's own
    # tool timeout so the agent re-calls rather than erroring.
    max_wait_seconds: float = 55.0
    transcript_file: str = "heard.jsonl"
    # After speech aimed at JARVIS arrives, hold on briefly for the rest of the
    # thought. Saying the wake word then hesitating splits one request into two
    # phrases, and answering the first half is worse than waiting a beat.
    settle_seconds: float = 1.2
    # If the agent has not answered within this long, speak a holding line so
    # the wait does not sound like a crash. 0 disables it.
    acknowledge_after: float = 2.5
    acknowledgements: tuple[str, ...] = (
        "Let me have a look.",
        "One moment.",
        "Looking into that now.",
        "Give me a second.",
        "Checking that for you.",
    )


@dataclass(frozen=True)
class Config:
    """Top level configuration."""

    audio: AudioConfig = field(default_factory=AudioConfig)
    wake: WakeConfig = field(default_factory=WakeConfig)
    stt: SttConfig = field(default_factory=SttConfig)
    tts: TtsConfig = field(default_factory=TtsConfig)
    service: ServiceConfig = field(default_factory=ServiceConfig)
    log_level: str = "INFO"

    @property
    def log_dir(self) -> Path:
        return project_root() / "logs"

    @classmethod
    def load(cls, path: Path | None = None, environ: dict[str, str] | None = None) -> Config:
        """Build a Config from the TOML file and environment."""
        environ = os.environ if environ is None else environ
        path = path or project_root() / "jarvis.toml"

        data: dict[str, Any] = {}
        if path.is_file():
            data = tomllib.loads(path.read_text(encoding="utf-8"))

        config = _apply(cls(), data)
        return _apply(config, _env_overrides(environ))


_SECTIONS = frozenset({"audio", "wake", "stt", "tts", "service"})


def _env_overrides(environ: dict[str, str]) -> dict[str, Any]:
    """Turn JARVIS_STT_BACKEND=x into {"stt": {"backend": "x"}}."""
    overrides: dict[str, Any] = {}
    for key, value in environ.items():
        if not key.startswith("JARVIS_") or key == "JARVIS_HOME":
            continue
        remainder = key[len("JARVIS_") :].lower()
        section, _, option = remainder.partition("_")
        if section in _SECTIONS and option:
            overrides.setdefault(section, {})[option] = value
        else:
            overrides[remainder] = value
    return overrides


def _apply(config: Any, data: dict[str, Any]) -> Any:
    """Recursively overlay a mapping onto a frozen dataclass, coercing types."""
    if not data:
        return config
    known = {f.name: f for f in fields(config)}
    updates: dict[str, Any] = {}
    for key, value in data.items():
        spec = known.get(key)
        if spec is None:
            raise ValueError(f"Unknown config option: {key}")
        current = getattr(config, key)
        if is_dataclass(current) and isinstance(value, dict):
            updates[key] = _apply(current, value)
        else:
            updates[key] = _coerce(value, spec.type, key)
    return replace(config, **updates)


def _coerce(value: Any, annotation: Any, key: str) -> Any:
    """Coerce a TOML or environment value to the field's declared type."""
    text = annotation if isinstance(annotation, str) else getattr(annotation, "__name__", "")

    if "tuple" in text:
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return tuple(value)
    if "None" in text and (value is None or value == ""):
        return None
    if "bool" in text:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if "int" in text and "str" not in text:
        return int(value)
    if "float" in text:
        return float(value)
    if "str" in text:
        return str(value)
    raise ValueError(f"Cannot coerce {value!r} for config option {key}")
