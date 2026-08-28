"""The list JARVIS keeps for itself.

Everything here is about the list staying useful rather than just growing: no
duplicates, a cap that drops the oldest, and a file a human can edit without
anything breaking.
"""

from __future__ import annotations

from dataclasses import replace

from jarvis.config import Config
from jarvis.memories import as_prompt, load, remember


def known(root, limit=2000):
    """Everything under a directory, with memories.md as the written one."""
    return load(root, [root / "memories.md"], limit)


def test_a_lesson_is_written_down_and_read_back(tmp_path):
    path = tmp_path / "memories.md"
    lesson = "Spotify's tree is empty until the window has been focused once."
    assert "Remembered" in remember(path, lesson)
    assert known(tmp_path) == [lesson]


def test_the_file_explains_itself_to_whoever_opens_it(tmp_path):
    path = tmp_path / "memories.md"
    remember(path, "The taskbar scans as a window called Taskbar.")
    body = path.read_text(encoding="utf-8")
    assert body.startswith("# What JARVIS has learned")
    assert "edit or delete" in body


def test_the_same_lesson_is_not_written_twice(tmp_path):
    """A model that has just read its own memory in its prompt will offer to
    write down what it read there."""
    path = tmp_path / "memories.md"
    remember(path, "Outlook in a browser has 177 targets.")
    assert "Already remembered" in remember(path, "outlook in a BROWSER has 177 targets.")
    assert len(known(tmp_path)) == 1


def test_an_empty_lesson_is_refused(tmp_path):
    path = tmp_path / "memories.md"
    assert "Nothing to remember" in remember(path, "   ")
    assert not path.exists()


def test_a_lesson_is_flattened_to_one_line(tmp_path):
    """One bullet per lesson, so a newline in the middle would break the file."""
    path = tmp_path / "memories.md"
    remember(path, "First half\nsecond half")
    assert known(tmp_path) == ["First half second half"]


def test_prose_around_the_bullets_is_not_read(tmp_path):
    """The header is for a human. Sending it to the model is tokens spent saying
    nothing, and it invites the model to answer it."""
    path = tmp_path / "memories.md"
    path.write_text(
        "# Notes\n\nSome explanation someone typed.\n\n- The real lesson.\n", encoding="utf-8"
    )
    assert known(tmp_path) == ["The real lesson."]


def test_a_hand_edited_file_is_picked_up(tmp_path):
    path = tmp_path / "memories.md"
    path.write_text("- Typed by hand\n", encoding="utf-8")
    assert known(tmp_path) == ["Typed by hand"]


def test_a_missing_file_is_simply_no_memories(tmp_path):
    assert known(tmp_path / "not-there") == []


def test_the_oldest_go_when_it_is_full(tmp_path):
    """The right end to lose: the desk changes, so a lesson about an application
    that has since been updated is worse than no lesson."""
    path = tmp_path / "memories.md"
    for n in range(10):
        remember(path, f"Lesson number {n} about this desktop")

    kept = known(tmp_path, 120)
    assert kept, "something has to survive"
    assert kept[-1].startswith("Lesson number 9")
    assert not any(x.startswith("Lesson number 0") for x in kept)
    assert len(known(tmp_path, 0)) == 10, "nothing is deleted, only left unread"


def test_being_full_is_said_out_loud(tmp_path):
    path = tmp_path / "memories.md"
    for n in range(6):
        remember(path, f"Lesson number {n} about this desktop", limit=100)
    assert "no longer read back" in remember(path, "One more thing", limit=100)


def test_nothing_learned_yet_adds_nothing_to_the_prompt():
    assert as_prompt([]) == ""


def test_the_lessons_are_labelled_in_the_prompt():
    block = as_prompt(["One thing.", "Another."])
    assert block.startswith("WHAT IS KNOWN ABOUT THIS MACHINE:")
    assert "- One thing.\n- Another." in block


def test_the_file_lives_under_context_by_default():
    from jarvis.tools import memory_file

    assert memory_file(Config()).parts[-3:] == ("context", "memories", "memories.md")


def test_an_absolute_path_is_taken_as_it_is(tmp_path):
    from jarvis.tools import memory_file

    where = tmp_path / "elsewhere.md"
    config = replace(Config(), brain=replace(Config().brain, memories_file=str(where)))
    assert memory_file(config) == where


# ------------------------------------------------- reference beside the memories


def test_every_markdown_file_in_the_directory_is_read(tmp_path):
    """navigation.md is how Windows behaves, which is reference rather than
    memory - written by hand, and read into the same block."""
    (tmp_path / "navigation.md").write_text("- Press the Windows key to open things.\n")
    remember(tmp_path / "memories.md", "Teams is an MSIX package here.")

    assert known(tmp_path) == [
        "Press the Windows key to open things.",
        "Teams is an MSIX package here.",
    ]


def test_reference_comes_first_and_is_never_trimmed(tmp_path):
    """Capping the curated half to make room for the accumulated half would be
    the wrong way round."""
    (tmp_path / "navigation.md").write_text("- " + "reference " * 30 + "\n")
    for n in range(20):
        remember(tmp_path / "memories.md", f"Lesson number {n} about this desktop")

    kept = known(tmp_path, 200)
    assert kept[0].startswith("reference"), "kept whole, and first"
    assert len(kept) < 21, "the written half was trimmed instead"


def test_a_bullet_that_wraps_is_put_back_together(tmp_path):
    """A hand written file wraps at eighty columns, and reading only the first
    line silently halves the sentence."""
    (tmp_path / "navigation.md").write_text(
        "- To open an application, press the Windows key,\n  type its name and press enter.\n"
    )
    assert known(tmp_path) == [
        "To open an application, press the Windows key, type its name and press enter."
    ]


def test_prose_between_bullets_does_not_join_them(tmp_path):
    (tmp_path / "navigation.md").write_text(
        "Some explanation.\n\n- First lesson.\n\nMore prose.\n\n- Second lesson.\n"
    )
    assert known(tmp_path) == ["First lesson.", "Second lesson."]


def test_writing_only_ever_touches_its_own_file(tmp_path):
    reference = tmp_path / "navigation.md"
    reference.write_text("- Reference line.\n")
    remember(tmp_path / "memories.md", "Something learned.")
    assert reference.read_text() == "- Reference line.\n"


def test_the_shipped_navigation_file_is_read_into_the_prompt():
    """It is reference that ships with the repository, so it should never be
    absent or unreadable."""
    from jarvis.config import project_root
    from jarvis.memories import bullets

    shipped = project_root() / "context" / "memories" / "navigation" / "os-navigation.md"
    lessons = bullets(shipped)
    assert len(lessons) > 5
    assert any("Windows key" in lesson for lesson in lessons)


def test_the_two_files_jarvis_writes_are_the_two_that_get_capped():
    """Everything else under context/memories is reference: written by hand,
    bounded by hand, and read whole."""
    from jarvis.tools import memory_file, navigation_file

    written = {memory_file(Config()).name, navigation_file(Config()).name}
    assert written == {"memories.md", "user-navigation.md"}
    assert navigation_file(Config()).parent.name == "navigation"


# ------------------------------------------------------------ inside the loop


def test_a_lesson_written_now_is_in_the_prompt_next_turn(tmp_path):
    """This is the whole point. Written at half past two, in play at half past
    three, without a restart."""
    from test_brain import brain, said

    config = replace(Config(), brain=replace(Config().brain, memories_file=str(tmp_path / "m.md")))
    it = brain(said("Noted, sir."), said("Yes sir."), config=config)
    assert "WHAT YOU HAVE LEARNED" not in it.messages[0]["content"]

    remember(tmp_path / "m.md", "The Start menu has nothing clickable in it.")
    it.turn(["hello"])
    assert "The Start menu has nothing clickable in it." in it.messages[0]["content"]


def test_a_refusal_suggests_writing_the_reason_down(tmp_path):
    """Only on a refusal. Nudging after every call would fill the list with
    notes about things that worked."""
    from test_brain import brain, calling, said, toolbox

    config = replace(Config(), brain=replace(Config().brain, memories_file=str(tmp_path / "m.md")))
    box = toolbox(click="Refused: Target 1 is 'Delete', not 'Reply'.", press_keys="Pressed mute")
    it = brain(
        calling("click", target=1, expecting="Reply"),
        said("It refused, sir."),
        config=config,
        box=box,
    )
    it.turn(["press reply"])
    assert "remember() it" in next(m for m in it.messages if m["role"] == "tool")["content"]

    it = brain(calling("press_keys", keys="mute"), said("Muted."), config=config, box=box)
    it.turn(["mute it"])
    assert "remember()" not in next(m for m in it.messages if m["role"] == "tool")["content"]


def test_memories_off_means_no_tool_and_no_prompt_block(tmp_path):
    from test_brain import brain

    config = replace(
        Config(),
        brain=replace(
            Config().brain, memories=False, memories_file=str(tmp_path / "m.md"), shell=False
        ),
        screen=replace(Config().screen, control=False),
    )
    remember(tmp_path / "m.md", "Something learned earlier.")
    it = brain(config=config)
    assert it.remembered() == ""

    from jarvis.tools import build_toolbox

    assert "remember" not in build_toolbox(config).names
