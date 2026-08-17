from __future__ import annotations

import pytest

from jarvis.config import Config


def test_defaults_are_sane():
    config = Config.load(path=None, environ={})
    assert config.service.host == "127.0.0.1"
    assert config.stt.backend == "whisper"


def test_toml_overrides_defaults(tmp_path):
    path = tmp_path / "jarvis.toml"
    path.write_text(
        """
        log_level = "DEBUG"

        [stt]
        backend = "google"
        whisper_beam_size = 3

        """,
        encoding="utf-8",
    )
    config = Config.load(path=path, environ={})
    assert config.log_level == "DEBUG"
    assert config.stt.backend == "google"
    assert config.stt.whisper_beam_size == 3
    assert config.stt.whisper_model == "base.en"  # untouched default survives


def test_environment_beats_toml(tmp_path):
    path = tmp_path / "jarvis.toml"
    path.write_text('[stt]\nbackend = "from-file"\n', encoding="utf-8")
    config = Config.load(path=path, environ={"JARVIS_STT_BACKEND": "from-env"})
    assert config.stt.backend == "from-env"


def test_environment_coerces_types():
    config = Config.load(
        path=None,
        environ={
            "JARVIS_STT_WHISPER_VAD": "false",
            "JARVIS_SERVICE_PORT": "9001",
            "JARVIS_AUDIO_DEVICE_INDEX": "3",
            "JARVIS_LOG_LEVEL": "WARNING",
        },
    )
    assert config.stt.whisper_vad is False
    assert config.service.port == 9001
    assert config.audio.device_index == 3
    assert config.log_level == "WARNING"


def test_unknown_option_is_rejected(tmp_path):
    path = tmp_path / "jarvis.toml"
    path.write_text("[stt]\nnonsense = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="nonsense"):
        Config.load(path=path, environ={})


def test_unknown_section_is_rejected(tmp_path):
    path = tmp_path / "jarvis.toml"
    path.write_text('[brain]\nmodel = "gone"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="brain"):
        Config.load(path=path, environ={})


def test_optional_field_accepts_empty_string():
    config = Config.load(path=None, environ={"JARVIS_AUDIO_ENERGY_THRESHOLD": ""})
    assert config.audio.energy_threshold is None


def test_the_config_path_is_not_read_as_a_setting():
    """JARVIS_CONFIG says where to look, not what to set. Treating it as an
    option made the documented override fail on startup."""
    config = Config.load(environ={"JARVIS_CONFIG": "somewhere/jarvis.json"})
    assert config.stt.backend == "whisper"


def test_the_home_path_is_not_read_as_a_setting():
    assert Config.load(environ={"JARVIS_HOME": "C:/elsewhere"}).log_level == "INFO"
