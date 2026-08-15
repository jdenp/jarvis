from __future__ import annotations

from dataclasses import replace

import pytest

from jarvis.cli import apply_args, build_parser, privacy_report
from jarvis.config import Config


def parse(*argv):
    return build_parser().parse_args(list(argv))


def test_flags_override_the_loaded_config():
    config = apply_args(
        Config(),
        parse(
            "--device", "1",
            "--tts", "sapi",
            "--stt", "google",
            "--port", "9999",
            "--no-wake-word",
            "--log-level", "DEBUG",
        ),
    )  # fmt: skip
    assert config.audio.device_index == 1
    assert config.tts.engine == "sapi"
    assert config.stt.backend == "google"
    assert config.service.port == 9999
    assert config.wake.required is False
    assert config.log_level == "DEBUG"


def test_no_flags_leaves_the_config_alone():
    assert apply_args(Config(), parse()) == Config()


def test_device_zero_is_not_treated_as_unset():
    assert apply_args(Config(), parse("--device", "0")).audio.device_index == 0


def test_unknown_tts_engine_is_rejected_at_parse_time():
    with pytest.raises(SystemExit):
        parse("--tts", "festival")


def test_no_arguments_means_serve():
    assert parse().command is None  # main() treats this as serve


def test_subcommands_parse():
    assert parse("say", "hello", "there").text == ["hello", "there"]
    assert parse("next", "--wait", "30").wait == 30
    assert parse("mcp").command == "mcp"
    assert parse("status").command == "status"


def test_defaults_are_fully_local():
    config = Config()
    assert config.stt.backend == "whisper"
    assert config.tts.engine == "auto"  # auto never reaches edge
    assert config.service.host == "127.0.0.1"


def test_privacy_report_says_so_when_nothing_leaves():
    line = privacy_report(Config(), stt_local=True, tts_local=True)
    assert "Nothing leaves this machine." in line
    assert "REMOTE" not in line


def test_privacy_report_names_each_leak():
    config = replace(
        Config(),
        stt=replace(Config().stt, backend="google"),
        tts=replace(Config().tts, engine="edge"),
    )
    line = privacy_report(config, stt_local=False, tts_local=False)
    assert "REMOTE" in line
    assert "your microphone audio" in line
    assert "every reply" in line
