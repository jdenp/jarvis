"""The desk as tools a model can call.

Two things matter here and neither is the happy path: a tool that is switched
off has to be absent rather than present and refusing, and a tool that fails has
to come back as a result. A tool call with no result leaves the conversation
unable to continue.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace

import pytest

from conftest import FakeDesktop, button
from jarvis.config import Config, project_root
from jarvis.screen import Screen
from jarvis.tools import Tool, Toolbox, build_toolbox, clip, parse_arguments, render_scan, shell


def desk(*elements, **kwargs) -> Screen:
    return Screen(Config().screen, backend=FakeDesktop(list(elements), **kwargs))


def box(config=None, screen=None) -> Toolbox:
    return build_toolbox(config or Config(), screen or desk(button("Reply")))


# --------------------------------------------------------------- what is offered


def test_everything_is_offered_by_default():
    """Plug and play - the defaults turn it all on, so this is the real list."""
    assert box().names == [
        "look_at_screen",
        "screenshot",
        "look_at_image",
        "focus_window",
        "click",
        "type_text",
        "scroll",
        "press_keys",
        "search_web",
        "read_page",
        "remember",
        "run_command",
    ]


def test_with_control_off_it_can_only_look():
    config = replace(Config(), screen=replace(Config().screen, control=False))
    assert box(config).names == [
        "look_at_screen",
        "screenshot",
        "look_at_image",
        "search_web",
        "read_page",
        "remember",
        "run_command",
    ]


def test_with_the_shell_off_there_is_no_shell():
    config = replace(Config(), brain=replace(Config().brain, shell=False))
    assert "run_command" not in box(config).names


def test_a_tool_that_is_off_is_absent_rather_than_refusing():
    """A model cannot be told not to reach for something it can see, so the way
    to switch a tool off is to not describe it."""
    config = replace(Config(), screen=replace(Config().screen, control=False))
    assert "Refused" not in box(config).run("click", {"target": 1, "expecting": "Reply"})
    assert "no tool called 'click'" in box(config).run("click", {"target": 1, "expecting": "x"})


def test_the_schema_is_the_shape_an_openai_endpoint_expects():
    spec = next(s for s in box().specs() if s["function"]["name"] == "click")
    assert spec["type"] == "function"
    assert spec["function"]["parameters"]["required"] == ["target", "expecting"]
    assert spec["function"]["parameters"]["properties"]["target"]["type"] == "integer"
    assert spec["function"]["parameters"]["properties"]["button"]["enum"] == ["left", "right"]


# ------------------------------------------------------------------- dispatching


def test_an_unknown_name_says_what_does_exist():
    result = box().run("play_music", {})
    assert "no tool called 'play_music'" in result
    assert "press_keys" in result


def test_wrong_arguments_come_back_as_something_to_read():
    result = box().run("press_keys", {"combination": "ctrl+s"})
    assert "called wrongly" in result
    assert "combination" in result


def test_a_refusal_is_a_result_not_an_exception():
    """`expecting` is checked before anything is pressed, so a stale number
    costs a turn rather than deleting the wrong message."""
    tools = box(screen=desk(button("Delete")))
    tools.run("look_at_screen", {})
    result = tools.run("click", {"target": 1, "expecting": "Reply"})
    assert result.startswith("Refused:")
    assert "'Delete', not 'Reply'" in result


def test_clicking_before_looking_says_to_look_first():
    result = box().run("click", {"target": 1, "expecting": "Reply"})
    assert "Look at the screen first" in result


def test_a_tool_blowing_up_is_still_a_result(monkeypatch):
    monkeypatch.setattr("jarvis.hands.press", lambda keys: (_ for _ in ()).throw(OSError("no")))
    result = box().run("press_keys", {"keys": "ctrl+s"})
    assert "press_keys failed - OSError: no" in result


# ---------------------------------------------------------------------- looking


def test_a_scan_is_numbers_and_labels_and_no_coordinates():
    scan_text = box(screen=desk(button("Reply", 10, 20), button("Reply all", 100, 20))).run(
        "look_at_screen", {}
    )
    assert '1  Button      "Reply"' in scan_text
    assert '2  Button      "Reply all"' in scan_text, "quoted, so `expecting` is unambiguous"
    for coordinate in ("10", "20", "100"):
        assert f" {coordinate} " not in scan_text, "a model never sees a pixel"


def test_a_window_with_nothing_clickable_says_to_use_the_keyboard():
    """The Start menu exposes one element covering itself. Clicking it is
    refused, so a scan that says nothing more costs three turns to find out."""
    whole = button("Start", 0, 0, 800, 600)
    result = box(screen=desk(whole)).run("look_at_screen", {})
    assert "nothing here to click" in result
    assert "type_text" in result and "press_keys" in result


def test_the_other_windows_are_named():
    screen = desk(button("Reply"), others=[(2, "Spotify Premium")])
    assert "Spotify Premium" in box(screen=screen).run("look_at_screen", {})


def test_a_truncated_list_says_it_is_a_sample():
    """It used to be the first sixty in reading order, which amputated the
    bottom of the window - on Spotify, the whole transport bar."""
    config = replace(Config(), screen=replace(Config().screen, max_targets=3))
    crowded = [button(f"Button {n}", 0, n * 30) for n in range(10)]
    screen = Screen(config.screen, backend=FakeDesktop(crowded))
    result = build_toolbox(config, screen).run("look_at_screen", {})
    assert "7 more targets did not fit" in result
    assert "even spread" in result


def test_render_respects_the_label_limit():
    """A chat row carries the whole last message as its name."""
    screen = desk(button("x" * 200))
    scan = screen.look()
    assert len(render_scan(scan, label_chars=20).splitlines()[1]) < 60


# ----------------------------------------------------------------------- acting


def test_clicking_reports_that_the_numbers_have_moved(monkeypatch):
    clicks = []
    monkeypatch.setattr("jarvis.hands.click", lambda x, y, **kw: clicks.append((x, y, kw)))
    tools = box(screen=desk(button("Reply", 10, 20)))
    tools.run("look_at_screen", {})
    result = tools.run("click", {"target": 1, "expecting": "Reply"})
    assert clicks and clicks[0][:2] == (50, 32)
    assert "left clicked 'Reply'" in result
    assert "look again" in result


def test_typing_with_no_target_says_it_could_not_be_checked(monkeypatch):
    typed = []
    monkeypatch.setattr("jarvis.hands.type_text", typed.append)
    monkeypatch.setattr("jarvis.hands.press", lambda keys: typed.append(f"<{keys}>"))
    result = box().run("type_text", {"text": "spotify", "then": "press_enter"})
    assert typed == ["spotify", "<enter>"]
    assert "nothing here can confirm where it went" in result
    assert "escape" in result, "and that whatever was opened is still open"


def test_naming_a_target_without_saying_what_it_is_is_refused():
    """Otherwise the check is quietly switched off by leaving one field out."""
    result = box().run("type_text", {"text": "hi", "then": "leave_it", "target": 1})
    assert "without `expecting`" in result


def test_an_unknown_key_name_is_refused_rather_than_half_pressed():
    """It reads as a refusal, not as a crash - "press_keys failed - ValueError"
    invites a retry of the same thing."""
    result = box().run("press_keys", {"keys": "ctrl+nope"})
    assert result == "Refused: Unknown key 'nope' in 'ctrl+nope'."


# -------------------------------------------------------------- writing it down


def test_the_generated_tool_file_matches_the_code(capsys):
    """context/tools/tools.md is generated by `jarvis tools --write`, the same
    way config/defaults.json is. Nothing reads it - the schemas go in every
    request - but it is the only readable account of what the model is told, and
    a hand-maintained one would be wrong within a week.

    Compared against what the command prints rather than rebuilt here, so the
    test cannot drift from the command either.
    """
    from jarvis.cli import TOOLS_FILE, main

    assert main(["tools"]) == 0
    printed = capsys.readouterr().out
    path = project_root() / TOOLS_FILE
    assert path.is_file(), "run: jarvis tools --write"
    assert path.read_text(encoding="utf-8") == printed, (
        "context/tools/tools.md is out of date - regenerate it with `jarvis tools --write`"
    )


def test_every_tool_is_in_it(capsys):
    from jarvis.cli import main

    main(["tools"])
    printed = capsys.readouterr().out
    for name in box().names:
        assert f"## {name}" in printed
    assert "## pause_transcription" in printed, "including the ones only the voice path has"


def test_arguments_are_described_with_their_type_and_whether_they_are_needed():
    from jarvis.tools import as_markdown

    body = as_markdown(box())
    assert "- `target` (integer, required) - a number from look_at_screen" in body
    assert "- `button` (left | right)" in body, "an enum reads as its choices"


def test_a_tool_with_no_arguments_says_so():
    from jarvis.tools import as_markdown

    tools = build_toolbox(Config(), desk(button("Reply")), ears=Ears())
    assert "No arguments." in as_markdown(tools)


# ------------------------------------------------------------------------- ears


class Ears:
    """Whatever owns the microphone. Only the voice path has one."""

    def __init__(self, listening=True):
        self.listening = listening

    def pause(self):
        if not self.listening:
            return False
        self.listening = False
        return True

    def resume(self):
        self.listening = True


def test_there_is_nothing_to_pause_without_a_microphone():
    """Chat mode has no ears to close, so the tools are absent rather than
    present and failing."""
    assert "pause_transcription" not in box().names


def test_listening_can_be_stopped_and_started():
    ears = Ears()
    tools = build_toolbox(Config(), desk(button("Reply")), ears=ears)
    assert "resume_transcription" in tools.names

    assert "Stopped listening" in tools.run("pause_transcription", {})
    assert ears.listening is False
    assert "Listening again" in tools.run("resume_transcription", {})
    assert ears.listening is True


def test_pausing_says_how_to_get_back():
    """From there it cannot hear them ask, and silence is indistinguishable from
    a crash."""
    tools = build_toolbox(Config(), desk(button("Reply")), ears=Ears())
    result = tools.run("pause_transcription", {})
    assert Config().service.hotkey in result

    spec = next(s for s in tools.specs() if s["function"]["name"] == "pause_transcription")
    assert Config().service.hotkey in spec["function"]["description"]
    assert "hyphen does that" in spec["function"]["description"], "not a way to avoid replying"


def test_pausing_twice_says_so_rather_than_pretending():
    tools = build_toolbox(Config(), desk(button("Reply")), ears=Ears(listening=False))
    assert tools.run("pause_transcription", {}) == "Already not listening."


def test_the_prompt_only_mentions_closing_its_ears_when_it_can():
    """A prompt naming a tool that does not exist invites a call that comes
    straight back as an error."""
    from jarvis.brain import Brain
    from test_brain import FakeVoice, brain

    assert "pause_transcription" not in brain().messages[0]["content"]

    config = replace(
        Config(),
        screen=replace(Config().screen, control=False),
        brain=replace(Config().brain, shell=False, memories=False),
    )
    voice = FakeVoice()
    voice.pause = lambda: True
    voice.resume = lambda: None
    listening = Brain(config, voice, model=object())
    assert "pause_transcription and then" in listening.messages[0]["content"]


# ------------------------------------------------------------------------ shell


def test_a_command_runs_and_its_output_comes_back():
    assert "hello" in shell("Write-Output hello", timeout=30)


def test_a_failing_command_reports_its_exit_code():
    result = shell("exit 3", timeout=30)
    assert "Exit code 3" in result


def test_a_command_that_waits_for_input_is_killed(monkeypatch):
    def hangs(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="powershell", timeout=60)

    monkeypatch.setattr(subprocess, "run", hangs)
    result = shell("Read-Host", timeout=60)
    assert "Timed out after 60s" in result
    assert "waiting for input" in result


def test_the_shell_tool_points_coding_work_elsewhere():
    """JARVIS is not the one to edit source files a line at a time."""
    spec = next(s for s in box().specs() if s["function"]["name"] == "run_command")
    assert "not your job" in spec["function"]["description"]


def test_a_named_coding_agent_is_the_one_it_is_told_to_run():
    """Named in config or not at all - a hardcoded command in a description is a
    claim about the machine that nothing checks."""
    config = replace(Config(), brain=replace(Config().brain, coding_agent="somecli"))
    spec = next(s for s in box(config).specs() if s["function"]["name"] == "run_command")
    assert 'run `somecli "the whole request' in spec["function"]["description"]


# --------------------------------------------------------------------- clipping


def test_output_is_cut_out_of_the_middle_not_the_end():
    """The head says what ran and the tail carries the error."""
    text = "START" + "x" * 500 + "END"
    cut = clip(text, 100)
    assert cut.startswith("START")
    assert cut.endswith("END")
    assert "dropped from the middle" in cut


def test_something_short_enough_is_left_alone():
    assert clip("fine", 100) == "fine"
    assert clip("fine", 0) == "fine", "no limit means no limit"


# ------------------------------------------------------------------- arguments


def test_arguments_arrive_as_a_json_string():
    assert parse_arguments('{"keys": "playpause"}') == ({"keys": "playpause"}, "")


def test_arguments_already_parsed_are_taken_as_they_are():
    assert parse_arguments({"keys": "mute"}) == ({"keys": "mute"}, "")


def test_no_arguments_is_not_a_failure():
    assert parse_arguments("") == ({}, "")
    assert parse_arguments(None) == ({}, "")


@pytest.mark.parametrize("raw", ['{"keys": ', '["playpause"]', "42"])
def test_anything_else_says_why_it_could_not_be_read(raw):
    arguments, complaint = parse_arguments(raw)
    assert arguments == {}
    assert complaint, "the model has to be told what was wrong with it"


# ------------------------------------------------------- refusing twice over


def test_the_same_refusal_twice_says_something_else():
    """Followed exactly, "look again and use the new numbers" gives back the
    same numbers and the same refusal. A live session clicked System in a
    terminal, looked again, clicked System again, and spent its whole budget
    going round."""
    tools = box(screen=desk(button("Delete")))
    tools.run("look_at_screen", {})

    first = tools.run("click", {"target": 1, "expecting": "Reply"})
    second = tools.run("click", {"target": 1, "expecting": "Reply"})
    assert "word for word the last refusal" not in first
    assert "word for word the last refusal" in second
    assert "Stop clicking this one" in second
    assert "press_keys" in second and "shell command" in second, "and what to do instead"


def test_a_different_refusal_is_not_an_escalation():
    tools = box(screen=desk(button("Delete"), button("Archive", 200)))
    tools.run("look_at_screen", {})
    tools.run("click", {"target": 1, "expecting": "Reply"})
    other = tools.run("click", {"target": 2, "expecting": "Reply"})
    assert "word for word" not in other


def test_something_that_works_in_between_clears_it():
    """Twice in a row is the signal, not twice ever."""
    tools = box(screen=desk(button("Delete")))
    tools.run("look_at_screen", {})
    tools.run("click", {"target": 1, "expecting": "Reply"})
    tools.run("look_at_screen", {})
    again = tools.run("click", {"target": 1, "expecting": "Reply"})
    assert "word for word" not in again


# ------------------------------------------------------------- going in circles


def clicker():
    """A tool that always works and never gets anywhere."""
    return Toolbox([Tool(name="click", description="click", run=lambda **k: "left clicked Casual")])


def test_the_third_identical_call_says_so():
    """A live session pressed "More actions for Casual" six times over two
    minutes. Every click worked, so nothing refused and nothing escalated - it
    opened the same Edit/Delete menu each time and the card the user wanted was
    never in the scan to be pressed."""
    box = clicker()
    assert "third time" not in box.run("click", {"target": 1})
    assert "third time" not in box.run("click", {"target": 1})
    assert "third time" in box.run("click", {"target": 1})


def test_a_look_in_between_does_not_reset_it():
    """Which is the shape it actually takes: click, look, click, look. Comparing
    against only the previous call would never have fired once."""
    box = Toolbox(
        [
            Tool(name="click", description="click", run=lambda **k: "clicked"),
            Tool(name="look_at_screen", description="look", run=lambda **k: "scanned"),
        ]
    )
    for _ in range(2):
        box.run("click", {"target": 1})
        box.run("look_at_screen", {})
    assert "third time" in box.run("click", {"target": 1})


def test_a_different_argument_is_a_different_call():
    box = clicker()
    for target in (1, 2, 3):
        assert "third time" not in box.run("click", {"target": target})


def test_the_result_itself_still_comes_back():
    box = clicker()
    for _ in range(3):
        result = box.run("click", {"target": 1})
    assert result.startswith("left clicked Casual")


def test_it_only_looks_back_so_far():
    """A button pressed once a minute for a good reason should not be nagged."""
    box = Toolbox(
        [
            Tool(name="click", description="click", run=lambda **k: "clicked"),
            Tool(name="look_at_screen", description="look", run=lambda **k: "scanned"),
        ]
    )
    box.run("click", {"target": 1})
    for _ in range(8):
        box.run("look_at_screen", {})
    box.run("click", {"target": 1})
    assert "third time" not in box.run("click", {"target": 1})


# ------------------------------------------------------------- closing a window


def pressing(front_stays=False):
    """A toolbox and the list of what actually reached the keyboard."""
    import jarvis.hands

    pressed: list[str] = []
    original = jarvis.hands.press
    jarvis.hands.press = pressed.append

    class Stuck(FakeDesktop):
        def activate(self, hwnd):
            self.activations.append(hwnd)
            return False

    backend = (Stuck if front_stays else FakeDesktop)([button("Reply")], front=9)
    toolbox = build_toolbox(Config(), Screen(Config().screen, backend=backend))
    toolbox._restore = original  # type: ignore[attr-defined]
    return toolbox, pressed


@pytest.fixture(autouse=True)
def _put_the_keyboard_back():
    import jarvis.hands

    original = jarvis.hands.press
    yield
    jarvis.hands.press = original


def test_a_closing_combination_sent_blind_is_refused():
    """The one that actually happened: it scanned Chrome, which focuses nothing,
    then pressed alt+f4 at whatever was in front - its own console. The log ends
    mid line."""
    box, _ = pressing()
    result = box.run("press_keys", {"keys": "alt+f4"})
    assert result.startswith("Refused")
    assert "Name the window" in result
    assert "window=" in result


def test_every_spelling_of_it_is_refused():
    box, _ = pressing()
    for keys in ("alt+f4", "ALT+F4", "alt-f4", "ctrl+w", "ctrl+shift+w", "ctrl+q"):
        assert box.run("press_keys", {"keys": keys}).startswith("Refused"), keys


def test_anything_that_does_not_close_a_window_still_goes_straight_through():
    box, pressed = pressing()
    for keys in ("ctrl+s", "escape", "playpause", "win+up", "f4"):
        assert not box.run("press_keys", {"keys": keys}).startswith("Refused"), keys
    assert pressed == ["ctrl+s", "escape", "playpause", "win+up", "f4"]


def test_naming_the_window_focuses_it_first():
    box, pressed = pressing()
    result = box.run("press_keys", {"keys": "alt+f4", "window": "Notepad"})
    assert pressed == ["alt+f4"]
    assert "Notepad" in result


def test_a_window_that_will_not_come_forward_presses_nothing():
    """Focusing fails often enough to matter - the foreground lock refuses it -
    and pressing anyway sends a close to whatever is there instead."""
    box, pressed = pressing(front_stays=True)
    result = box.run("press_keys", {"keys": "alt+f4", "window": "Notepad"})
    assert result.startswith("Refused")
    assert "would not come to the front" in result
    assert pressed == []


# ------------------------------------------------------------------- seeing


def png(path, width=40, height=20, colour=(20, 120, 200)):
    from PIL import Image

    Image.new("RGB", (width, height), colour).save(path, "PNG")
    return path


def test_a_picture_comes_back_as_a_data_url(tmp_path):
    from jarvis.tools import read_image

    found, encoded, size = read_image(str(png(tmp_path / "shot.png")))
    assert found.name == "shot.png"
    assert encoded.startswith("data:image/png;base64,")
    assert size == (40, 20)


def test_a_wide_picture_is_narrowed_before_it_is_sent(tmp_path):
    """The whole desk across two monitors is 4880px, which is worth shrinking
    before it becomes tokens."""
    from jarvis.tools import read_image

    _, _, size = read_image(str(png(tmp_path / "wide.png", width=4000, height=1000)), 1600)
    assert size == (1600, 400), "aspect kept"


def test_a_picture_already_small_enough_is_left_alone(tmp_path):
    from jarvis.tools import read_image

    _, _, size = read_image(str(png(tmp_path / "small.png", width=800, height=600)), 1600)
    assert size == (800, 600)


def test_a_file_that_is_not_there_says_so(tmp_path):
    from jarvis.screen import ScreenUnavailable
    from jarvis.tools import read_image

    with pytest.raises(ScreenUnavailable, match="no file at"):
        read_image(str(tmp_path / "nothing.png"))


def test_a_file_that_is_not_a_picture_says_what_is_readable(tmp_path):
    from jarvis.screen import ScreenUnavailable
    from jarvis.tools import read_image

    document = tmp_path / "notes.txt"
    document.write_text("not a picture")
    with pytest.raises(ScreenUnavailable, match=r"[.]png"):
        read_image(str(document))


def test_looking_queues_the_picture_for_the_brain(tmp_path):
    """A tool result is text, so this is the only way it reaches the model."""
    kit = box()
    result = kit.run("look_at_image", {"path": str(png(tmp_path / "shot.png"))})
    assert "in front of you now, 40 by 20" in result
    assert len(kit.images) == 1
    assert kit.images[0].startswith("data:image/png;base64,")


def test_a_missing_file_queues_nothing(tmp_path):
    kit = box()
    assert kit.run("look_at_image", {"path": str(tmp_path / "gone.png")}).startswith("Refused")
    assert kit.images == []


def test_taking_one_says_where_it_went_and_what_to_do_next(tmp_path, monkeypatch):
    monkeypatch.setattr("jarvis.marks.capture", lambda bounds, path, width=0: png(path))
    kit = box()
    result = kit.run("screenshot", {})
    assert "every monitor" in result
    assert "look_at_image" in result
    assert kit.images == [], "taking a picture is not looking at one"


def test_naming_a_window_takes_that_window(tmp_path, monkeypatch):
    seen = {}

    def capture(bounds, path, width=0):
        seen["bounds"] = bounds
        return png(path)

    monkeypatch.setattr("jarvis.marks.capture", capture)
    assert "Notepad" in box().run("screenshot", {"window": "Notepad"})
    assert seen["bounds"] is not None, "cropped to the window rather than the whole desk"


def test_with_images_off_neither_tool_is_there():
    """A model with no projector gets a couple of thousand tokens of payload for
    nothing, so this is a switch rather than a warning."""
    config = replace(Config(), brain=replace(Config().brain, images=False))
    assert "screenshot" not in box(config).names
    assert "look_at_image" not in box(config).names


# ------------------------------------------------------------------ elevation


def test_a_window_with_nothing_in_it_is_told_to_wait():
    """A window still drawing itself is the ordinary reason for this, and looking
    again a moment later is the right answer to it."""
    kit = box(screen=desk(button("", width=2, height=2)))
    said = kit.run("look_at_screen", {})
    assert "still building itself" in said


def test_an_elevated_window_is_told_the_truth_instead(monkeypatch):
    """Waiting is exactly wrong here. A live session waited, focused, scrolled,
    screenshotted and alt+f4'd Task Manager over four minutes, wrote four
    memories blaming its render time, and never once said the one true thing."""
    monkeypatch.setattr("jarvis.tools.runs_as_admin", lambda hwnd: True)
    kit = box(screen=desk(button("", width=2, height=2)))
    said = kit.run("look_at_screen", {})

    assert "runs as administrator" in said
    assert "Waiting will not change that" in said
    assert "taskkill" in said, "the one route that does work"
    assert "still building itself" not in said


def test_our_own_window_is_not_elevated():
    """Not a mock - the check has to be right about this machine, and a False
    that is really an error would put the message on every empty window."""
    from jarvis.screen import runs_as_admin

    assert runs_as_admin(0) is False, "no such window is not an elevated one"
