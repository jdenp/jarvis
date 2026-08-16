"""Runtime configuration.

Precedence, lowest to highest: dataclass defaults, the config file, ``JARVIS_*``
environment variables, command line flags. The defaults here are the source of
truth; ``config/defaults.json`` is generated from them and a test catches drift.
"""

from __future__ import annotations

import json
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
    # Hard cap on one phrase, only to stop a stuck stream recording forever.
    # It adds no delay - a phrase still ends on silence - and hitting it cuts you off.
    phrase_time_limit: float = 60.0
    calibration_seconds: float = 1.5
    dynamic_energy_threshold: bool = True
    energy_threshold: float | None = None
    # Floor under calibration - a silent room calibrates low enough to hear its
    # own speakers. Raise it if JARVIS hears itself, lower it if you must shout.
    min_energy_threshold: float = 55.0
    # Silence that ends a phrase. Generous on purpose, and the main cost in the
    # delay before an agent sees you spoke. Raise it if you keep getting cut off.
    pause_threshold: float = 1.7
    # How long after JARVIS stops talking to keep ignoring the microphone.
    echo_guard_seconds: float = 0.5
    # Half duplex by default. On, it allows barging in, but without echo
    # cancellation only echo.py stops JARVIS answering itself. Headphones only.
    listen_while_speaking: bool = False


@dataclass(frozen=True)
class SttConfig:
    """Speech to text. Defaults to local transcription."""

    backend: str = "whisper"  # whisper (local) | google (uploads your audio)
    language: str = "en-GB"
    whisper_model: str = "base.en"
    # CUDA is far quicker on long utterances but costs ~340MB of VRAM. Set
    # "auto" if the GPU is free, and install it: uv sync --extra cuda
    whisper_device: str = "cpu"  # cpu | cuda | auto
    whisper_compute_type: str = "default"
    whisper_beam_size: int = 1
    whisper_vad: bool = True


@dataclass(frozen=True)
class TtsConfig:
    """Text to speech. Defaults to the offline Windows voice."""

    engine: str = "auto"  # auto (local first) | sapi | edge | none
    voice: str = "en-GB-RyanNeural"  # edge only
    # Preference order, first installed wins. George needs registering first -
    # see scripts/expose-onecore-voices.ps1.
    sapi_voice: str = "George, Hazel"
    rate: int = 210
    volume: float = 1.0


@dataclass(frozen=True)
class ServiceConfig:
    """The voice service an agent connects to."""

    host: str = "127.0.0.1"
    port: int = 8770
    # Longest a wait_for_speech call may block. Keep it under the agent's own
    # tool timeout so the agent re-calls rather than erroring.
    max_wait_seconds: float = 55.0
    transcript_file: str = "heard.jsonl"
    # Held after a phrase arrives, in case another follows. Small, because
    # audio.pause_threshold already absorbs hesitation inside a phrase.
    settle_seconds: float = 0.8
    # If the agent has not answered within this long, speak a holding line so
    # the wait does not sound like a crash. 0 disables it.
    acknowledge_after: float = 4.0
    # Past this, an utterance is flagged as backlog rather than a live
    # request. 0 disables the flag.
    stale_after_seconds: float = 120.0
    # Some carry the "sir" and some do not, so rotating lands the inflection
    # as a habit rather than a tic. Shuffled per process, see Acknowledger.
    acknowledgements: tuple[str, ...] = (
        "Let me have a look.",
        "One moment, sir.",
        "Looking into that now.",
        "Give me a second.",
        "Checking that for you, sir.",
        "Right, on it.",
        "Just a moment.",
        "Working on it, sir.",
        "Let me check.",
        "Bear with me.",
        "On it now.",
        "Give me a moment, sir.",
    )


@dataclass(frozen=True)
class Config:
    """Top level configuration."""

    audio: AudioConfig = field(default_factory=AudioConfig)
    stt: SttConfig = field(default_factory=SttConfig)
    tts: TtsConfig = field(default_factory=TtsConfig)
    service: ServiceConfig = field(default_factory=ServiceConfig)
    log_level: str = "INFO"

    @property
    def log_dir(self) -> Path:
        return project_root() / "logs"

    @property
    def config_dir(self) -> Path:
        return project_root() / "config"

    def as_dict(self) -> dict[str, Any]:
        """Plain JSON-friendly mapping. Tuples become lists, paths are not included."""
        return _unwrap(self)

    @classmethod
    def load(cls, path: Path | None = None, environ: dict[str, str] | None = None) -> Config:
        """Build a Config from a config file and the environment.

        With no path, the first of ``CONFIG_FILES`` that exists is used.
        """
        environ = os.environ if environ is None else environ
        found = path if path is not None else find_config_file()

        data: dict[str, Any] = {}
        if found is not None and found.is_file():
            data = read_config_file(found)

        config = _apply(cls(), data)
        return _apply(config, _env_overrides(environ))


# Searched in order; the root files are what earlier versions used.
CONFIG_FILES = (
    "config/jarvis.json",
    "config/jarvis.toml",
    "jarvis.json",
    "jarvis.toml",
)

_SECTIONS = frozenset({"audio", "stt", "tts", "service"})


def find_config_file(root: Path | None = None) -> Path | None:
    """First config file that exists, or None. JARVIS_CONFIG overrides the search."""
    if explicit := os.environ.get("JARVIS_CONFIG"):
        candidate = Path(explicit).expanduser()
        return candidate if candidate.is_file() else None

    root = root or project_root()
    for relative in CONFIG_FILES:
        candidate = root / relative
        if candidate.is_file():
            return candidate
    return None


def read_config_file(path: Path) -> dict[str, Any]:
    """Parse a config file by extension."""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text) if text.strip() else {}
    return tomllib.loads(text)


def _unwrap(value: Any) -> Any:
    """Dataclasses to dicts, tuples to lists, everything else as it is."""
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _unwrap(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, tuple | list):
        return [_unwrap(item) for item in value]
    return value


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
        # JSON has no comments, so an underscore-prefixed key is a note.
        if key.startswith("_"):
            continue
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
