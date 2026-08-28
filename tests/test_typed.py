"""Typing into a voice session.

The line goes in exactly where speech does, so almost everything about it is
already tested elsewhere. What is here is the reading: it has to leave the
terminal as it found it, whether the line was sent or abandoned, because it is
borrowing the row the live line lives on.
"""

from __future__ import annotations

import io

import pytest

from jarvis.typed import CANCEL as CANCEL_KEY
from jarvis.typed import ENTER, PREFIXES, Typing
from jarvis.ui import Ui


class Keys:
    """A person at a keyboard, scripted.

    Stops the reader once they run out of things to type, so `run()` can be
    called straight rather than driven from another thread.
    """

    def __init__(self, *presses: str) -> None:
        self.pending = list("".join(presses))
        self.exhausted = lambda: None

    def waiting(self) -> bool:
        if not self.pending:
            self.exhausted()
        return bool(self.pending)

    def read(self) -> str:
        return self.pending.pop(0)


def typing(*presses, written=None, on_cancel=None):
    said: list[str] = []
    keys = Keys(*presses)
    reader = Typing(
        Ui(written or io.StringIO(), colour=True),
        said.append,
        keyboard=keys,
        on_cancel=on_cancel,
    )
    keys.exhausted = reader.stop
    return reader, said


def test_a_line_and_enter_is_said():
    reader, _ = typing("hello sir\r")
    assert reader.read_line() == "hello sir"


def test_enter_on_its_own_says_nothing():
    reader, _ = typing("\r")
    assert reader.read_line() == ""


def test_surrounding_space_goes():
    reader, _ = typing("   open spotify   \r")
    assert reader.read_line() == "open spotify"


def test_backspace_takes_a_character_back():
    reader, _ = typing("helllo\b\bo\r")
    assert reader.read_line() == "hello"


def test_backspace_on_an_empty_line_is_harmless():
    reader, _ = typing("\b\b\bhi\r")
    assert reader.read_line() == "hi"


def test_escape_throws_the_line_away():
    """The way out for somebody who pressed a key by accident - which is the
    whole reason this waits for a keypress rather than showing a prompt."""
    reader, _ = typing("delete everything\x1b")
    assert reader.read_line() == ""


def test_an_arrow_key_is_read_and_dropped():
    """Windows sends a prefix and then the code. Both go: there is nothing here
    to navigate, and leaving the second one turns an arrow into a letter."""
    reader, _ = typing("ab\xe0Hc\r")
    assert reader.read_line() == "abc"


def test_what_was_typed_is_echoed_as_it_goes():
    written = io.StringIO()
    reader, _ = typing("hi\r", written=written)
    reader.read_line()
    assert "you > " in written.getvalue()
    assert "h" in written.getvalue() and "i" in written.getvalue()


def test_the_line_is_wiped_afterwards():
    """What was sent is redrawn a moment later as `you > ...`, and what was
    abandoned should leave nothing behind at all."""
    written = io.StringIO()
    reader, _ = typing("hello\x1b", written=written)
    reader.read_line()
    assert written.getvalue().endswith("\r")
    assert " " * 10 in written.getvalue(), "painted over"


def test_the_live_line_is_given_back():
    written = io.StringIO()
    reader, _ = typing("hi\r", written=written)
    reader.ui.status("listening")
    reader.read_line()
    assert reader.ui._held is False
    reader.ui.status("thinking")
    assert "thinking" in written.getvalue(), "and it draws again"


def test_the_listening_line_stays_put_while_you_type():
    """It takes the row beneath rather than the row itself. Erasing it meant
    that the moment you started typing the terminal stopped saying whether
    JARVIS was listening or thinking, and looked dead for as long as you took."""
    from jarvis.ui import UP

    written = io.StringIO()
    reader, _ = typing("hello\r", written=written)
    reader.ui.status("listening")
    before = written.getvalue()
    reader.read_line()

    during = written.getvalue()[len(before) :]
    assert during.startswith("\n"), "a new row, rather than wiping the old one"
    assert UP in during, "and the row is given back afterwards"


def test_nothing_is_pushed_when_there_is_nothing_to_keep():
    """With no status drawn there is no reason to put a blank row in front of
    whoever is typing."""
    written = io.StringIO()
    reader, _ = typing("hi\r", written=written)
    reader.read_line()
    assert not written.getvalue().startswith("\n")


def test_the_status_does_not_draw_over_what_is_being_typed():
    written = io.StringIO()
    reader, _ = typing("hi", written=written)
    reader.ui.hold()
    reader.ui.status("thinking")
    assert "thinking" not in written.getvalue()


def test_a_reply_arriving_mid_sentence_puts_the_line_back():
    """The cursor is on the row they borrowed rather than on the live line, so
    the ordinary erase wiped a half typed sentence and printed on top of it.
    Every escape hit this, the cancel note landing at the moment the prompt
    came back."""
    written = io.StringIO()
    reader, _ = typing("hi", written=written)
    reader.ui.status("listening")
    reader.ui.hold(reader._repaint)
    reader._typed = list("open spo")
    before = len(written.getvalue())

    reader.ui.spoke("Half past two, sir.")

    after = written.getvalue()[before:]
    assert "Half past two, sir." in after
    assert after.endswith("open spo"), "and their sentence is drawn again underneath"
    assert reader.ui._stepped is True, "one row given up, one taken - the way back holds"


def test_the_live_line_is_spent_on_what_arrived_rather_than_left_above_it():
    """Otherwise a stale `listening` is stranded in the scrollback every time
    anything is written while somebody is typing."""
    from jarvis.ui import UP

    written = io.StringIO()
    reader, _ = typing("hi", written=written)
    reader.ui.status("listening")
    reader.ui.hold(reader._repaint)
    before = len(written.getvalue())
    reader.ui.spoke("Half past two, sir.")
    assert UP in written.getvalue()[before:], "it takes the row back first"


def test_nothing_is_repainted_once_the_line_is_finished_with():
    """The callback outliving the line would draw a prompt nobody asked for."""
    written = io.StringIO()
    reader, _ = typing("hi" + ENTER[0], written=written)
    reader.read_line()
    assert reader.ui._repaint is None


# ------------------------------------------------------------------ escape


def test_escape_on_an_empty_line_means_stop_rather_than_never_mind():
    """There is nothing on the line to throw away, so the key means the other
    thing you would want from it while something is being written for you."""
    from jarvis.typed import CANCEL

    reader, _ = typing(CANCEL)
    assert reader.read_line() == ""
    assert reader.cancelled is True


def test_escape_on_a_line_with_something_on_it_still_only_drops_the_line():
    from jarvis.typed import CANCEL

    reader, _ = typing("delete everything" + CANCEL)
    assert reader.read_line() == ""
    assert reader.cancelled is False, "nothing was stopped, a sentence was abandoned"


def test_stopping_something_asks_straight_away_what_they_meant_instead():
    """Escape is nearly always followed by saying what you wanted. Waiting for
    another keypress to put the prompt back makes them press twice."""
    from jarvis.typed import CANCEL

    reader, said = typing(CANCEL, "open spotify" + ENTER[0], on_cancel=lambda: True)
    reader.run()
    assert said == ["open spotify"], "read without waiting to be woken again"


def test_escape_with_nothing_to_stop_opens_no_prompt_at_all():
    written = io.StringIO()
    reader, said = typing(CANCEL_KEY, written=written, on_cancel=lambda: False)
    reader.run()
    assert said == []
    assert "you > " not in written.getvalue()


def test_a_key_that_types_nothing_leaves_no_prompt_behind():
    """Num lock pauses transcription and the console sees the press too, so an
    empty `you >` was left sitting under the status line every time."""
    # A function or arrow key is a prefix and then its code; a toggle is a
    # control character the console has no letter for. None of them open a line.
    presses = [prefix + "H" for prefix in PREFIXES] + [chr(14), chr(3)]
    for press in presses:
        written = io.StringIO()
        reader, said = typing(press, written=written)
        reader.run()
        assert "you > " not in written.getvalue(), repr(press)
        assert said == []


def test_the_key_that_opened_the_line_is_the_first_character_on_it():
    """Read before anything is drawn, so it has to be put back afterwards."""
    reader, said = typing("open spotify" + ENTER[0])
    reader.run()
    assert said == ["open spotify"]


# ------------------------------------------------------------------ the loop


def test_a_finished_line_is_handed_over():
    reader, said = typing("open spotify\r")
    reader.run()
    assert said == ["open spotify"]


def test_one_line_at_a_time_and_all_of_them():
    reader, said = typing("open spotify\r", "and turn it up\r")
    reader.run()
    assert said == ["open spotify", "and turn it up"]


def test_an_abandoned_line_is_not_handed_over():
    reader, said = typing("open spotify\x1b")
    reader.run()
    assert said == []


def test_a_broken_keyboard_does_not_take_the_session_down():
    said: list[str] = []
    reader = None

    class Broken:
        def waiting(self):
            return True

        def read(self):
            reader.stop()
            raise OSError("the console went away")

    reader = Typing(Ui(io.StringIO(), colour=True), said.append, keyboard=Broken())
    reader.run()  # the assertion is that this returns at all
    assert said == []
    assert reader.ui._held is False, "and the live line was given back"


# --------------------------------------------------- where the line ends up


def test_a_typed_line_is_recorded_as_though_it_were_heard():
    """Same transcript, same line on screen. The brain cannot tell the
    difference, and should not."""
    from jarvis.service import VoiceService

    written = io.StringIO()
    service = VoiceService(_config(), ui=Ui(written, colour=False))
    service.typed("  open spotify  ")

    assert [item.text for item in service.transcript.since(0)] == ["open spotify"]
    assert "you > open spotify" in written.getvalue()


def test_typing_works_while_the_microphone_is_paused():
    """Pausing shuts the ears. Somebody typing has plainly chosen to speak."""
    from jarvis.service import VoiceService

    service = VoiceService(_config())
    service.transcript.pause()
    service.typed("still here")
    assert [item.text for item in service.transcript.since(0)] == ["still here"]


def test_an_empty_line_reaches_nothing():
    from jarvis.service import VoiceService

    service = VoiceService(_config())
    service.typed("   ")
    assert service.transcript.since(0) == []


@pytest.fixture(autouse=True)
def _keep_the_transcript_out_of_the_repository(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))


def _config():
    from jarvis.config import Config

    return Config()


def test_the_prompt_is_the_same_blue_as_every_other_you():
    """It is the same thing: a line from them, on its way in."""
    from jarvis.ui import COLOUR

    written = io.StringIO()
    reader, _ = typing("hi\r", written=written)
    reader.read_line()
    assert COLOUR["user"] + "you > " in written.getvalue()


def test_the_status_comes_back_once_the_line_is_gone():
    """It borrows the row rather than taking it."""
    written = io.StringIO()
    reader, _ = typing("hi\r", written=written)
    reader.ui.status("listening")
    before = written.getvalue().count("listening")
    reader.read_line()
    assert written.getvalue().count("listening") > before
