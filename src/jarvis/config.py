"""Runtime configuration.

Precedence, lowest to highest: dataclass defaults, the config file, ``JARVIS_*``
environment variables, command line flags.

The defaults here are the single source of truth. ``config/defaults.json`` is
generated from them by ``jarvis config --defaults``, and a test fails if the two
drift - a hand-maintained example file goes stale the first time anyone changes
a default, which it repeatedly did.
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
    # Hard cap on one phrase, set high enough not to be a factor. This does not
    # add any delay - a phrase still ends on silence - it only stops a stuck
    # stream recording forever. Hitting it truncates you mid sentence, so there
    # is no reason to keep it tight.
    phrase_time_limit: float = 60.0
    calibration_seconds: float = 1.5
    dynamic_energy_threshold: bool = True
    energy_threshold: float | None = None
    # Floor under the calibrated threshold, because a silent room calibrates to
    # single digits and then hears its own speakers. Kept low: the echo guard in
    # microphone.py does the real work of not transcribing ourselves, so this
    # only has to catch the pathological case. Raise it if JARVIS starts hearing
    # itself, lower it if you have to speak up to be heard.
    min_energy_threshold: float = 55.0
    # How long a silence ends a phrase. Deliberately generous: pausing to think
    # mid sentence should not split one request into two. This is the main cost
    # in the delay before an agent sees what you said, and the main thing to
    # change if you keep getting cut off.
    pause_threshold: float = 1.7
    # How long after JARVIS stops talking to keep ignoring the microphone.
    echo_guard_seconds: float = 0.5
    # Half duplex by default: the microphone is muted while JARVIS speaks, so it
    # does not transcribe its own voice. Turning this on keeps listening through
    # a reply, which allows barging in - but with one microphone and no echo
    # cancellation the only thing standing between you and JARVIS answering
    # itself is the text comparison in echo.py. Speakers rather than headphones
    # will almost certainly need it left off.
    listen_while_speaking: bool = False


@dataclass(frozen=True)
class SttConfig:
    """Speech to text. Defaults to local transcription."""

    backend: str = "whisper"  # whisper (local) | google (uploads your audio)
    language: str = "en-GB"
    whisper_model: str = "base.en"
    # cpu by default. CUDA is about 0.2s quicker per utterance and costs ~340MB
    # of VRAM, nearly all of it the CUDA context rather than the model. On a
    # machine also running a local LLM that is a bad trade: the delay is
    # dominated by audio.pause_threshold, not by transcription. Set "auto" or
    # "cuda" if the GPU is free, and install the extra: uv sync --extra cuda
    whisper_device: str = "cpu"  # cpu | cuda | auto
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
    # Held after a phrase arrives, in case another follows it. Small, because
    # audio.pause_threshold already absorbs hesitation inside a phrase - this
    # only catches a speaker who stopped completely and then carried on.
    settle_seconds: float = 0.8
    # If the agent has not answered within this long, speak a holding line so
    # the wait does not sound like a crash. 0 disables it.
    acknowledge_after: float = 4.0
    # Some carry the "sir" and some do not, so rotating through them lands the
    # inflection as a habit rather than a tic. The order is shuffled per process
    # - a fixed list always opened with the same line, and a new MCP server is
    # started often enough that it became the only one you ever heard.
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

        With no path, the first of ``CONFIG_FILES`` that exists is used. JSON is
        the documented format; TOML still works for anyone who had one.
        """
        environ = os.environ if environ is None else environ
        found = path if path is not None else find_config_file()

        data: dict[str, Any] = {}
        if found is not None and found.is_file():
            data = read_config_file(found)

        config = _apply(cls(), data)
        return _apply(config, _env_overrides(environ))


# Searched in order. config/ first so everything to do with configuration lives
# in one place; the root files are what earlier versions used.
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
        # JSON has no comments, so anything underscore-prefixed is treated as a
        # note. Without this the only way to explain a setting is out of band,
        # and settings whose reasoning is not written down get changed back.
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
