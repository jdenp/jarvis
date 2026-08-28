"""The terminal both front ends draw into.

The live line is the only interesting part: it has to be erased before anything
permanent is written, or a status left half on screen ends up spliced into the
middle of the conversation.
"""

from __future__ import annotations

import io
import logging

from jarvis.ui import COLOUR, LogToUi, Silent, Ui, paint


def screen(colour=False):
    written = io.StringIO()
    return Ui(written, colour=colour), written


# ------------------------------------------------------------------ permanent


def test_the_conversation_reads_as_a_conversation():
    ui, written = screen()
    ui.heard("play some music")
    ui.tool("press_keys", "(keys='playpause')")
    ui.result("Pressed playpause, at whatever had focus.")
    ui.spoke("Playing, sir.")
    assert written.getvalue().splitlines() == [
        "you > play some music",
        "  > press_keys(keys='playpause')",
        "    Pressed playpause, at whatever had focus.",
        "jarvis > Playing, sir.",
    ]


def test_the_tool_name_is_coloured_and_the_arguments_are_not():
    """Dim on its own was invisible on a dark terminal, which is the whole
    reason a tool call has a colour of its own."""
    ui, written = screen(colour=True)
    ui.tool("look_at_screen", "(window='Taskbar')")
    body = written.getvalue()
    assert COLOUR["tool"] + "  > look_at_screen" in body
    assert COLOUR["dim"] + "(window='Taskbar')" in body


def test_a_tool_result_is_one_line_of_what_came_back():
    """Without it a call is only a claim - you see that it looked, not that it
    found nothing."""
    ui, written = screen()
    ui.result("Taskbar - 0 targets from 39 elements\nnothing here to click\nmore lines")
    assert written.getvalue().strip() == "Taskbar - 0 targets from 39 elements"


def test_a_tool_that_gave_back_nothing_still_draws_a_line():
    ui, written = screen()
    ui.result("")
    assert written.getvalue() == "    \n"


def test_the_banner_says_what_is_running():
    ui, written = screen()
    ui.banner("0.8.0", ["chat mode - 8 tools"])
    assert "JARVIS 0.8.0" in written.getvalue()
    assert "chat mode - 8 tools" in written.getvalue()


def test_nothing_is_painted_without_a_terminal():
    """Otherwise a piped or redirected session is a file full of escape codes."""
    ui, written = screen(colour=False)
    ui.spoke("Half past two, sir.")
    assert "\033[" not in written.getvalue()


def test_colour_is_used_when_there_is_a_terminal():
    ui, written = screen(colour=True)
    ui.spoke("Half past two, sir.")
    assert COLOUR["jarvis"] in written.getvalue()


def test_painting_an_unknown_name_is_left_alone():
    assert paint("nonsense", "text") == "text"
    assert paint("dim", "text", colour=False) == "text"


# ----------------------------------------------------------------- the live line


def test_the_live_line_is_erased_before_anything_permanent():
    """Otherwise the status is spliced into the middle of the conversation."""
    ui, written = screen(colour=True)
    ui.status("thinking")
    ui.spoke("Done, sir.")
    body = written.getvalue()
    assert "thinking" in body
    assert "\r" in body, "erased by carriage return and spaces"
    assert body.rstrip().endswith(COLOUR["reset"]) or "Done, sir." in body


def test_the_live_line_is_redrawn_under_what_was_written():
    ui, written = screen(colour=True)
    ui.status("listening")
    ui.heard("hello")
    assert written.getvalue().count("listening") == 2, "once before, once after"


def test_resting_takes_the_line_away_without_printing():
    ui, written = screen(colour=True)
    ui.status("thinking")
    before = len(written.getvalue())
    ui.resting()
    after = written.getvalue()[before:]
    assert "thinking" not in after
    assert "\n" not in after, "nothing permanent was written"


def test_there_is_no_animation_without_a_terminal():
    """A status line in a log file is noise, and \\r in a pipe is worse."""
    ui, written = screen(colour=False)
    ui.status("thinking")
    assert written.getvalue() == ""


class Console:
    """A stream that admits to a code page. StringIO will not have one set."""

    def __init__(self, encoding):
        self.encoding = encoding
        self.text = ""

    def write(self, text):
        self.text += text

    def flush(self):
        pass


def test_a_console_that_cannot_encode_braille_gets_a_plain_spinner():
    """A row of boxes on the status line is worse than a rotating bar."""
    assert Ui(Console("cp1252"), colour=True).frames == "|/-\\"


def test_a_utf8_console_gets_the_smoother_one():
    assert Ui(Console("utf-8"), colour=True).frames.startswith("⠋")


# --------------------------------------------------------------------- silence


def test_the_silent_terminal_does_nothing_at_all(capsys):
    """The default. The MCP server has stdout reserved for JSON-RPC, and the
    tests have no terminal to draw on."""
    quiet = Silent()
    for call in (quiet.heard, quiet.spoke, quiet.tool, quiet.note, quiet.warn, quiet.status):
        call("something")
    quiet.resting()
    quiet.close()
    assert capsys.readouterr().out == ""


def test_both_terminals_offer_the_same_thing():
    """Silent stands in for Ui everywhere, so a method added to one and not the
    other is a crash the first time somebody runs without a terminal."""
    for name in ("heard", "spoke", "tool", "note", "warn", "status", "resting", "ask", "close"):
        assert hasattr(Silent, name), name
        assert hasattr(Ui, name), name


# --------------------------------------------------------------------- logging


def test_a_warning_arrives_as_part_of_the_conversation():
    ui, written = screen()
    logger = logging.getLogger("jarvis.test-ui")
    logger.propagate = False
    logger.addHandler(LogToUi(ui))
    logger.warning("Could not speak - %s", "no service")
    logger.info("this one belongs in the file")

    body = written.getvalue()
    assert "! test-ui: Could not speak - no service" in body
    assert "belongs in the file" not in body, "INFO stays out of the terminal"


# -------------------------------------------------------------------- thinking


def test_the_thinking_line_shows_where_the_model_has_got_to():
    """Reasoning arrives as paragraphs and only the end of it is current."""
    from jarvis.ui import tail

    assert tail("first thought\nsecond thought", 200) == "first thought second thought"
    assert tail("a much longer chain of reasoning than fits", 12).startswith("...")
    assert len(tail("a much longer chain of reasoning than fits", 12)) == 12


def test_thinking_does_not_write_to_the_terminal_itself():
    """Tokens arrive far faster than anyone can read, so the animation thread
    picks it up at its own pace - eight writes a second, not hundreds."""
    ui, written = screen(colour=True)
    ui.status("thinking")
    before = len(written.getvalue())
    for n in range(50):
        ui.thinking(f"reasoning step {n}")
    assert len(written.getvalue()) == before


def test_the_thinking_is_gone_once_there_is_an_answer():
    """Shown while it happens, kept by nothing - which is what collapsing is."""
    ui, written = screen(colour=True)
    ui.thinking("weighing up whether to look at the screen")
    ui.status("running look_at_screen")
    ui.spoke("Spotify is open, sir.")
    ui.resting()
    assert "weighing up" not in written.getvalue().splitlines()[-1]


# ----------------------------------------------------------------- the meter


def test_the_meter_sits_at_the_right_hand_end():
    ui, written = screen(colour=True)
    ui.meter("ctx 12.3k/98k  out 192")
    ui.status("listening")
    line = written.getvalue()
    assert "listening" in line
    assert line.rstrip().endswith("ctx 12.3k/98k  out 192" + COLOUR["reset"])


def test_a_narrow_window_drops_the_meter_rather_than_wrapping():
    """A status line that wraps is two lines, and only one of them gets erased."""
    ui, written = screen(colour=True)
    ui.meter("ctx 12.3k/98k  out 192")
    ui.width = lambda: 20
    ui.status("running look_at_screen")
    assert "ctx" not in written.getvalue()


def test_tokens_read_at_a_glance():
    from jarvis.brain import count

    assert count(192) == "192"
    assert count(1234) == "1.2k"
    assert count(12345) == "12k"
    assert count(98304) == "98k"


def test_jarvis_speaks_in_orange_and_they_speak_in_blue():
    ui, written = screen(colour=True)
    ui.heard("what time is it")
    ui.spoke("Half past two, sir.")
    body = written.getvalue()
    assert COLOUR["user"] + "you >" in body
    assert COLOUR["jarvis"] + "jarvis >" in body
    assert COLOUR["jarvis"].endswith(COLOUR["art"][2:]), "the same orange as the name at startup"
    assert "38;5;208" in COLOUR["art"], "which the basic sixteen do not have at all"


def test_the_name_is_painted_before_anything_else_is_written():
    """usable() turns VT processing on as a side effect, and the banner is the
    first thing printed - so the colour has to be asked for through it."""
    from jarvis.cli import _banner

    art = _banner()
    assert "Just A Rather Very Intelligent System" in art
    assert art.startswith(COLOUR["art"]) or "\033[" not in art, "orange, or plain with no codes"
