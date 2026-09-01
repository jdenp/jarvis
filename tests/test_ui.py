"""The terminal both front ends draw into.

The live line is the only interesting part: it has to be erased before anything
permanent is written, or a status left half on screen ends up spliced into the
middle of the conversation.
"""

from __future__ import annotations

import io
import logging

from jarvis.ui import COLOUR, GAVE_LIMIT, LogToUi, Silent, Ui, paint


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


def test_a_finished_call_is_reported_to_whoever_else_is_drawing_it():
    """The web app draws the same tool calls the terminal does, so they are
    reported rather than reinvented - and as a pair, because a row that fills
    itself in later needs an id to come back to."""
    ui, _ = screen()
    seen = []
    ui.watch_tools(lambda called, gave: seen.append((called, gave)))

    ui.tool("look_at_screen", "(window='Teams')")
    assert seen == [], "the call is not finished until something came back"

    ui.result("Teams - 14 targets from 39 elements\nand more lines")
    assert seen == [("look_at_screen(window='Teams')", "Teams - 14 targets from 39 elements")]


def test_the_page_is_given_the_whole_first_line_and_the_terminal_its_own_width():
    """The terminal cuts to fit; a phone clips in CSS and can be tapped to see
    the rest, so cutting it here would throw away the half worth reading."""
    ui, written = screen()
    seen = []
    ui.watch_tools(lambda called, gave: seen.append(gave))

    ui.tool("run_command", "(command='dir')")
    ui.result("x" * 200)
    assert seen == ["x" * 200]
    drawn = written.getvalue().splitlines()[-1]
    assert len(drawn) < len(seen[0]), "the terminal cut it to fit"


def test_a_result_nobody_would_read_is_cut_before_it_is_sent():
    """A tool that answers in one very long line should not send all of it to
    a phone to be clipped to one row."""
    ui, _ = screen()
    seen = []
    ui.watch_tools(lambda called, gave: seen.append(gave))

    ui.tool("run_command", "(command='dir')")
    ui.result("y" * 5000)
    assert seen == ["y" * GAVE_LIMIT]


def test_a_result_with_no_call_in_front_of_it_is_not_reported():
    """Nothing does this, but a listener taking a result with no name attached
    would draw a row with nothing in it."""
    ui, _ = screen()
    seen = []
    ui.watch_tools(lambda called, gave: seen.append(gave))
    ui.result("out of nowhere")
    assert seen == []


def test_a_watcher_that_throws_does_not_take_the_terminal_with_it():
    ui, written = screen()
    ui.watch_tools(lambda called, gave: 1 / 0)
    ui.tool("press_keys", "(keys='win')")
    ui.result("Pressed win.")
    assert "Pressed win." in written.getvalue()


def test_the_banner_says_what_is_running():
    ui, written = screen()
    ui.banner("0.8.0", ["code mode - 8 tools"])
    assert "JARVIS 0.8.0" in written.getvalue()
    assert "code mode - 8 tools" in written.getvalue()


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
    """The default. A command whose output is read by something else needs
    stdout to itself, and the tests have no terminal to draw on."""
    quiet = Silent()
    for call in (quiet.heard, quiet.spoke, quiet.tool, quiet.note, quiet.warn, quiet.status):
        call("something")
    quiet.resting()
    quiet.close()
    assert capsys.readouterr().out == ""


def test_both_terminals_offer_the_same_thing():
    """Silent stands in for Ui everywhere, so a method added to one and not the
    other is a crash the first time somebody runs without a terminal - which is
    how `meter` got missed, Brain drawing one only when the endpoint reports
    tokens and the tests never having any to report."""
    public = {name for name in vars(Ui) if not name.startswith("_")}
    missing = public - set(vars(Silent))
    assert not missing, f"Silent is missing {sorted(missing)}"


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


def test_an_error_is_louder_than_a_warning():
    """A warning is a thing to know about later. An error at startup is a thing
    that is not going to work, and it has to survive a wall of orange."""
    ui, written = screen(colour=True)
    logger = logging.getLogger("jarvis.test-ui-loud")
    logger.propagate = False
    logger.addHandler(LogToUi(ui))
    logger.warning("something to know about later")
    logger.error("Couldn't load memories.md, exceeded character limit.")

    warned, failed = written.getvalue().splitlines()
    assert COLOUR["warn"] in warned and COLOUR["bad"] not in warned
    assert COLOUR["bad"] in failed


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


def test_the_whole_live_line_sits_in_the_bottom_right():
    """What is happening and what it has cost read as one thing in the corner,
    rather than two things at opposite ends of an empty row."""
    ui, written = screen(colour=True)
    ui.meter("ctx 12.3k/98k - in 40k - out 1.9k")
    ui.status("listening")
    line = written.getvalue()
    assert "listening - ctx 12.3k/98k - in 40k - out 1.9k" in line
    assert line.rstrip().endswith("out 1.9k" + COLOUR["reset"])


def test_a_narrow_window_cuts_the_status_and_keeps_the_numbers():
    """A status line that wraps is two rows and only one of them gets erased, so
    something has to go - and the numbers are the part worth keeping."""
    ui, written = screen(colour=True)
    ui.meter("ctx 12.3k/98k - in 40k - out 1.9k")
    ui.width = lambda: 30
    ui.status("running look_at_screen")
    line = written.getvalue().strip()
    assert line.endswith("out 1.9k" + COLOUR["reset"])
    assert "running look_at_screen" not in line
    assert ui._width == 29, "one short of the window, so it cannot wrap"


def test_the_thinking_leaves_the_numbers_where_they_are():
    """They are worth watching precisely because they hold still."""
    ui, written = screen(colour=True)
    ui.meter("ctx 12.3k/98k - in 40k - out 1.9k")
    ui.width = lambda: 80
    ui.status("thinking")
    ui.thinking("a chain of reasoning long enough to run past the end of the window " * 3)
    ui._draw()
    assert written.getvalue().rstrip().endswith("out 1.9k" + COLOUR["reset"])


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


# ------------------------------------------------- the live line while typing


def test_the_live_line_keeps_going_on_the_row_above_an_open_prompt():
    """It used to freeze the moment a prompt opened. Escape reopens the prompt
    by itself and nobody is necessarily about to type, so the line sat there
    stopped and read as a dead terminal until JARVIS next said something."""
    from jarvis.ui import RESTORE, SAVE, UP

    ui, written = screen(colour=True)
    ui.meter("ctx 2.6k/98k")
    ui.status("listening")
    ui.hold()
    before = len(written.getvalue())

    ui._tick += 1
    ui._draw()

    after = written.getvalue()[before:]
    assert "listening" in after, "still drawing"
    assert after.startswith(SAVE + UP), "a row up from where they are typing"
    assert after.endswith(RESTORE), "and their caret goes back where it was"


def test_it_will_not_draw_on_a_row_it_does_not_own():
    """With nothing on the live line, hold() pushes no row - so the row above
    belongs to the conversation and writing there costs a line of it."""
    ui, written = screen(colour=True)
    ui.hold()
    assert ui._stepped is False
    ui.status("thinking")
    assert "thinking" not in written.getvalue()


def test_what_arrives_mid_sentence_gets_a_row_and_the_live_line_gets_a_new_one():
    """Otherwise the next tick draws the status straight over what was said."""
    ui, written = screen(colour=True)
    ui.meter("ctx 2.6k/98k")
    ui.status("listening")
    ui.hold()
    before = len(written.getvalue())
    ui.spoke("Half past two, sir.")

    after = written.getvalue()[before:]
    assert after.count("\n") == 2, "one row for the reply, one pushed for the live line"
    ui._draw()
    assert "listening" in written.getvalue()[before + len(after) :], "and it draws again"


def test_resting_while_held_clears_the_row_above_not_the_one_being_typed_on():
    from jarvis.ui import RESTORE, SAVE

    ui, written = screen(colour=True)
    ui.meter("ctx 2.6k/98k")
    ui.status("listening")
    ui.hold()
    before = len(written.getvalue())
    ui.resting()

    after = written.getvalue()[before:]
    assert SAVE in after and RESTORE in after
    assert ui._width == 0
