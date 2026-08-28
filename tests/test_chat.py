"""Chat mode - the same loop with the microphone taken out.

The thing worth testing is that it really is the same loop: `ConsoleVoice` has to
satisfy exactly what `Brain.run_forever` asks of `ServiceVoice`, or this is a
second implementation pretending to be a front end.
"""

from __future__ import annotations

import builtins
import io

import pytest

from jarvis.chat import ConsoleVoice, Quit
from jarvis.ui import Ui


def typing(monkeypatch, *lines):
    """Stand in for a person at a keyboard."""
    queue = list(lines)

    def read(prompt=""):
        if not queue:
            raise EOFError
        return queue.pop(0)

    monkeypatch.setattr(builtins, "input", read)


def voice(written=None):
    """A chat front end drawing into something readable back."""
    return ConsoleVoice(Ui(written or io.StringIO(), colour=False))


def test_a_typed_line_is_heard(monkeypatch):
    typing(monkeypatch, "play some music")
    assert voice().hear(1.0) == ["play some music"]


def test_a_blank_line_just_asks_again(monkeypatch):
    typing(monkeypatch, "", "   ", "hello")
    assert voice().hear(1.0) == ["hello"]


def test_the_mid_task_check_finds_nothing_rather_than_blocking(monkeypatch):
    """One line is read at a time here, so the barge-in the voice path gets is
    absent rather than faked - and `hear(0.0)` must not sit on stdin."""
    typing(monkeypatch, "should not be read")
    assert voice().hear(0.0) == []


def test_running_out_of_input_ends_the_session(monkeypatch):
    typing(monkeypatch)
    with pytest.raises(EOFError):
        voice().hear(1.0)


def test_quitting_is_not_something_the_model_ever_sees(monkeypatch):
    typing(monkeypatch, "/quit")
    with pytest.raises(Quit):
        voice().hear(1.0)


def test_a_command_is_answered_and_then_it_asks_again(monkeypatch):
    written = io.StringIO()
    typing(monkeypatch, "/help", "what time is it")
    assert voice(written).hear(1.0) == ["what time is it"]
    assert "/memories" in written.getvalue()


def test_an_unknown_command_says_so(monkeypatch):
    written = io.StringIO()
    typing(monkeypatch, "/dance", "hello")
    voice(written).hear(1.0)
    assert "No such command as /dance" in written.getvalue()


def test_the_reply_is_drawn_and_kept():
    written = io.StringIO()
    talking = voice(written)
    talking.say("Half past two, sir.")
    assert "jarvis > Half past two, sir." in written.getvalue()
    assert talking.spoken == ["Half past two, sir."]


def test_the_console_voice_is_what_the_loop_expects():
    """Both front ends are duck-typed against the same two methods. If one grows
    an argument, this fails rather than only chat mode failing at runtime."""
    import inspect

    from jarvis.brain import ServiceVoice

    for name in ("hear", "say"):
        theirs = inspect.signature(getattr(ServiceVoice, name))
        ours = inspect.signature(getattr(ConsoleVoice, name))
        assert list(theirs.parameters) == list(ours.parameters), name


def test_the_tool_list_reads_as_a_list(monkeypatch):
    from dataclasses import replace

    from jarvis.brain import Brain
    from jarvis.config import Config

    config = replace(
        Config(),
        screen=replace(Config().screen, control=False),
        brain=replace(Config().brain, shell=False, memories=False),
    )
    written = io.StringIO()
    talking = voice(written)
    talking.brain = Brain(config, talking, model=object())
    typing(monkeypatch, "/tools", "hello")
    talking.hear(1.0)

    printed = written.getvalue()
    assert "look_at_screen" in printed
    assert "numbered" in printed, "and the first sentence of what it does"


def test_chat_mode_cannot_close_ears_it_does_not_have():
    """No microphone here, so the two transcription tools are absent and the
    prompt does not mention them."""
    from dataclasses import replace

    from jarvis.brain import Brain
    from jarvis.config import Config

    talking = voice()
    brain = Brain(replace(Config()), talking, model=object())
    assert "pause_transcription" not in brain.toolbox.names
    assert "pause_transcription" not in brain.messages[0]["content"]
