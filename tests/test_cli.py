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
            "--log-level", "DEBUG",
        ),
    )  # fmt: skip
    assert config.audio.device_index == 1
    assert config.tts.engine == "sapi"
    assert config.stt.backend == "google"
    assert config.service.port == 9999
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
    assert parse("status").command == "status"


def test_defaults_are_fully_local():
    config = Config()
    assert config.stt.backend == "whisper"
    assert config.tts.engine == "auto"  # auto never reaches edge
    assert config.service.host == "127.0.0.1"


def test_there_is_no_wake_word_machinery_left():
    """Removed deliberately: with no wake word it only ever produced phantom
    detections, and the Whisper hotword bias manufactured "JARVIS" from noise."""
    assert not hasattr(Config(), "wake")
    assert not hasattr(Config().stt, "hotwords")


def test_privacy_report_says_so_when_nothing_leaves():
    """With the web off, which is the only stage that is remote by nature."""
    local = replace(Config(), brain=replace(Config().brain, web=False))
    line = privacy_report(local, stt_local=True, tts_local=True)
    assert "Nothing leaves this machine." in line
    assert "REMOTE" not in line


def test_searching_the_web_is_named_as_leaving_the_machine():
    """It is the one thing in the default install that does, so the startup line
    is what makes it honest rather than quiet."""
    line = privacy_report(Config(), stt_local=True, tts_local=True)
    assert "web: html.duckduckgo.com (REMOTE)" in line
    assert "what you look up" in line


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


def test_nothing_starts_without_a_model(tmp_path, monkeypatch, caplog):
    """A JARVIS that listens, transcribes and answers nobody looks entirely
    well and is not. It used to carry on as ears and hands."""
    import logging

    from jarvis import brain, cli

    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    stopped = []

    class Service:
        ui = None
        transcriber = None
        headphones = False

        def __init__(self, config, **kwargs):
            self.speech = type("Speech", (), {"is_local": True})()

        def start(self):
            pass

        def stop(self):
            stopped.append(True)

    def refuse(*args, **kwargs):
        raise brain.ModelUnavailable("no model at http://127.0.0.1:8081/v1 - refused")

    monkeypatch.setattr("jarvis.service.VoiceService", Service)
    monkeypatch.setattr(brain, "start", refuse)

    logger = logging.getLogger("jarvis")
    with caplog.at_level(logging.ERROR, logger="jarvis"):
        code = cli.run_serve(Config(), parse("serve"), logger)

    assert code == 2, "it stops rather than coming up half working"
    assert stopped == [True], "and lets go of the microphone on the way out"
    assert "cannot start" in caplog.text
    assert "only runs with a model" in caplog.text


def test_the_brain_is_always_a_stage_of_the_privacy_line():
    """There is no switch to turn it off, so it is never absent from the line
    that says where the words go."""
    assert "brain: 127.0.0.1:8081 (local)" in privacy_report(
        Config(), stt_local=True, tts_local=True
    )
    assert not hasattr(Config().brain, "enabled")
