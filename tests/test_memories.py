"""The list JARVIS keeps for itself.

Everything here is about the list staying useful rather than just growing: no
duplicates, headings that group rather than multiply, a cap that stops reading
the top of the file, and a file a human can edit without anything breaking.
"""

from __future__ import annotations

from dataclasses import replace

from jarvis.config import Config
from jarvis.memories import as_prompt, bullets, load, remember, rewrite, sections


def known(root, limit=2000):
    """Everything under a directory, with memories.md as the written one."""
    return load(root, [root / "memories.md"], limit)


def flat(root, limit=2000):
    """The same with the grouping flattened away, for the tests about lines."""
    return [lesson for _, lessons in known(root, limit) for lesson in lessons]


def test_a_lesson_is_written_down_and_read_back(tmp_path):
    path = tmp_path / "memories.md"
    lesson = "Spotify's tree is empty until the window has been focused once."
    assert "Remembered" in remember(path, "Applications", lesson)
    assert known(tmp_path) == [("Applications", [lesson])]


def test_the_file_explains_itself_to_whoever_opens_it(tmp_path):
    path = tmp_path / "memories.md"
    remember(path, "Windows", "The taskbar scans as a window called Taskbar.")
    body = path.read_text(encoding="utf-8")
    assert body.startswith("# What JARVIS has learned")
    assert "edit or delete" in body


def test_the_same_lesson_is_not_written_twice(tmp_path):
    """A model that has just read its own memory in its prompt will offer to
    write down what it read there."""
    path = tmp_path / "memories.md"
    remember(path, "Applications", "Outlook in a browser has 177 targets.")
    assert "Already remembered" in remember(
        path, "Windows", "outlook in a BROWSER has 177 targets."
    )
    assert len(flat(tmp_path)) == 1


def test_an_empty_lesson_is_refused(tmp_path):
    path = tmp_path / "memories.md"
    assert "Nothing to remember" in remember(path, "Windows", "   ")
    assert not path.exists()


def test_a_lesson_is_flattened_to_one_line(tmp_path):
    """One bullet per lesson, so a newline in the middle would break the file."""
    path = tmp_path / "memories.md"
    remember(path, "Windows", "First half\nsecond half")
    assert flat(tmp_path) == ["First half second half"]


def test_prose_around_the_bullets_is_not_read(tmp_path):
    """The header is for a human. Sending it to the model is tokens spent saying
    nothing, and it invites the model to answer it."""
    path = tmp_path / "memories.md"
    path.write_text(
        "# Notes\n\nSome explanation someone typed.\n\n## Windows\n\n- The real lesson.\n",
        encoding="utf-8",
    )
    assert known(tmp_path) == [("Windows", ["The real lesson."])]


def test_a_hand_edited_file_is_picked_up(tmp_path):
    path = tmp_path / "memories.md"
    path.write_text("## Windows\n- Typed by hand\n", encoding="utf-8")
    assert flat(tmp_path) == ["Typed by hand"]


def test_a_missing_file_is_simply_no_memories(tmp_path):
    assert known(tmp_path / "not-there") == []


def test_a_file_over_the_cap_is_not_read_at_all(tmp_path):
    """Reading as much as fits sounds kinder and is not. The top is where the
    oldest and best worn lessons are, so losing that end quietly is how a stale
    duplicate further down ends up believed instead."""
    path = tmp_path / "memories.md"
    for n in range(10):
        remember(path, "Windows", f"Lesson number {n} about this desktop")

    assert flat(tmp_path, 120) == [], "all of it or none of it"
    assert len(flat(tmp_path, 0)) == 10, "nothing is deleted, and 0 is no cap"
    assert len(flat(tmp_path, 100_000)) == 10


def test_the_reference_files_beside_it_are_read_anyway(tmp_path):
    """They are written by hand and bounded by hand. One runaway file JARVIS
    wrote is no reason to forget how Windows works."""
    (tmp_path / "navigation").mkdir()
    (tmp_path / "navigation" / "os.md").write_text(
        "## Windows\n- Typed by hand\n", encoding="utf-8"
    )
    path = tmp_path / "memories.md"
    for n in range(10):
        remember(path, "Windows", f"Lesson number {n} about this desktop")

    assert flat(tmp_path, 120) == ["Typed by hand"]


def test_being_full_is_said_out_loud(tmp_path):
    path = tmp_path / "memories.md"
    for n in range(6):
        remember(path, "Windows", f"Lesson number {n} about this desktop", limit=100)
    said = remember(path, "Windows", "One more thing", limit=100)
    assert "none of it is read back" in said
    assert "memories.md" in said


def test_one_section_can_be_rewritten_without_touching_the_others(tmp_path):
    """The other half of `remember`, which only ever appends - right for a lesson
    learned in a turn, and no use at all for merging six of them into one."""
    path = tmp_path / "memories.md"
    remember(path, "Windows", "Alt tab does a thing")
    remember(path, "Windows", "Alt tab does the same thing")
    remember(path, "Personal", "They are left handed")

    assert rewrite(path, "Windows", ["Alt tab does one thing"]) is True
    assert dict(sections(path)) == {
        "Windows": ["Alt tab does one thing"],
        "Personal": ["They are left handed"],
    }


def test_a_rewrite_of_something_that_is_not_there_changes_nothing(tmp_path):
    path = tmp_path / "memories.md"
    remember(path, "Windows", "Alt tab does a thing")

    assert rewrite(path, "Applications", ["Teams is an MSIX package"]) is False
    assert rewrite(path, "Windows", []) is False
    assert rewrite(path, "Windows", ["   "]) is False
    assert dict(sections(path)) == {"Windows": ["Alt tab does a thing"]}


def test_a_rewritten_file_is_still_one_somebody_can_read(tmp_path):
    """It is markdown on purpose - the list is meant to be edited by hand, and a
    section run into the one below it is not."""
    path = tmp_path / "memories.md"
    remember(path, "Windows", "One")
    remember(path, "Personal", "Two")
    rewrite(path, "Windows", ["One, tidied"])

    assert "- One, tidied\n\n## Personal" in path.read_text(encoding="utf-8")


def test_nothing_learned_yet_adds_nothing_to_the_prompt():
    assert as_prompt([]) == ""


def test_the_lessons_are_labelled_in_the_prompt():
    block = as_prompt([("Windows", ["One thing.", "Another."])])
    assert block.startswith("WHAT YOU HAVE LEARNED SO FAR:")
    assert "## Windows\n- One thing.\n- Another." in block


def test_the_file_lives_under_context_by_default():
    from jarvis.tools import memory_file

    assert memory_file(Config()).parts[-3:] == ("context", "memories", "memories.md")


def test_an_absolute_path_is_taken_as_it_is(tmp_path):
    from jarvis.tools import memory_file

    where = tmp_path / "elsewhere.md"
    config = replace(Config(), brain=replace(Config().brain, memories_file=str(where)))
    assert memory_file(config) == where


# ----------------------------------------------------------------- the headings


def test_a_lesson_goes_under_its_own_heading(tmp_path):
    """One file holds everything it works out, so without the grouping it is a
    hundred unsorted sentences about windows, shortcuts and somebody's job."""
    path = tmp_path / "memories.md"
    remember(path, "Applications", "Teams is an MSIX package here.")
    remember(path, "Personal", "They work in medical technology.")
    remember(path, "Applications", "Explorer runs straight from run_command.")

    assert known(tmp_path) == [
        (
            "Applications",
            ["Teams is an MSIX package here.", "Explorer runs straight from run_command."],
        ),
        ("Personal", ["They work in medical technology."]),
    ]


def test_a_second_line_joins_the_heading_it_belongs_to(tmp_path):
    """Appended to the end of its own section rather than the end of the file,
    which is the whole point of the headings."""
    path = tmp_path / "memories.md"
    remember(path, "Applications", "First application line.")
    remember(path, "Personal", "A personal line.")
    remember(path, "Applications", "Second application line.")

    body = path.read_text(encoding="utf-8")
    assert body.count("## Applications") == 1
    assert body.index("Second application line.") < body.index("## Personal")


def test_the_same_heading_in_a_different_case_is_the_same_heading(tmp_path):
    path = tmp_path / "memories.md"
    remember(path, "Applications", "One line.")
    remember(path, "applications", "Another line.")
    assert path.read_text(encoding="utf-8").count("## ") == 1
    assert len(known(tmp_path)) == 1


def test_a_heading_is_tidied_before_it_is_written(tmp_path):
    """The model writes "## Navigation" as often as "Navigation"."""
    path = tmp_path / "memories.md"
    remember(path, "## Navigation ", "One line.")
    assert known(tmp_path) == [("Navigation", ["One line."])]


def test_a_bullet_with_no_heading_over_it_is_still_kept(tmp_path):
    """A line worth keeping is worth keeping badly filed."""
    path = tmp_path / "memories.md"
    path.write_text("- No heading anywhere.\n", encoding="utf-8")
    assert known(tmp_path) == [("Other", ["No heading anywhere."])]


def test_the_same_heading_in_two_files_is_one_heading_in_the_prompt(tmp_path):
    """So a `## Windows` JARVIS wrote lands under the shipped one of that name
    rather than beside it."""
    (tmp_path / "navigation.md").write_text("## Windows\n- Shipped line.\n", encoding="utf-8")
    remember(tmp_path / "memories.md", "Windows", "Learned line.")
    assert known(tmp_path) == [("Windows", ["Shipped line.", "Learned line."])]


# ------------------------------------------------- reference beside the memories


def test_every_markdown_file_in_the_directory_is_read(tmp_path):
    """os-navigation.md is how Windows behaves, which is reference rather than
    memory - written by hand, and read into the same block."""
    (tmp_path / "navigation.md").write_text("## Applications\n- Press the Windows key.\n")
    remember(tmp_path / "memories.md", "Windows", "Teams is an MSIX package here.")

    assert known(tmp_path) == [
        ("Applications", ["Press the Windows key."]),
        ("Windows", ["Teams is an MSIX package here."]),
    ]


def test_reference_comes_first_and_is_never_trimmed(tmp_path):
    """Capping the curated half to make room for the accumulated half would be
    the wrong way round."""
    (tmp_path / "navigation.md").write_text("## Reference\n- " + "reference " * 30 + "\n")
    for n in range(20):
        remember(tmp_path / "memories.md", "Windows", f"Lesson number {n} about this desktop")

    kept = flat(tmp_path, 200)
    assert kept[0].startswith("reference"), "kept whole, and first"
    assert len(kept) < 21, "the written half was trimmed instead"


def test_a_bullet_that_wraps_is_put_back_together(tmp_path):
    """A hand written file wraps at eighty columns, and reading only the first
    line silently halves the sentence."""
    (tmp_path / "navigation.md").write_text(
        "- To open an application, press the Windows key,\n  type its name and press enter.\n"
    )
    assert flat(tmp_path) == [
        "To open an application, press the Windows key, type its name and press enter."
    ]


def test_prose_between_bullets_does_not_join_them(tmp_path):
    (tmp_path / "navigation.md").write_text(
        "Some explanation.\n\n- First lesson.\n\nMore prose.\n\n- Second lesson.\n"
    )
    assert flat(tmp_path) == ["First lesson.", "Second lesson."]


def test_writing_only_ever_touches_its_own_file(tmp_path):
    reference = tmp_path / "navigation.md"
    reference.write_text("- Reference line.\n")
    remember(tmp_path / "memories.md", "Windows", "Something learned.")
    assert reference.read_text() == "- Reference line.\n"


def test_the_shipped_navigation_file_is_read_into_the_prompt():
    """It is reference that ships with the repository, so it should never be
    absent or unreadable."""
    from jarvis.config import project_root

    shipped = project_root() / "context" / "memories" / "navigation" / "os-navigation.md"
    groups = sections(shipped)
    assert len(groups) > 3, "and it is grouped, like everything else that is read"
    assert any("Windows key" in lesson for _, lessons in groups for lesson in lessons)


def test_one_file_grows_on_its_own_and_it_is_the_one_that_is_capped():
    """Everything else under context/memories is reference: written by hand,
    bounded by hand, and read whole."""
    from jarvis.tools import memory_file

    assert memory_file(Config()).name == "memories.md"


# ------------------------------------------------------------ inside the loop


def test_a_lesson_written_now_is_in_the_prompt_next_turn(tmp_path):
    """This is the whole point. Written at half past two, in play at half past
    three, without a restart."""
    from test_brain import brain, said

    config = replace(Config(), brain=replace(Config().brain, memories_file=str(tmp_path / "m.md")))
    it = brain(said("Noted, sir."), said("Yes sir."), config=config)
    assert "The Start menu has nothing clickable in it." not in it.messages[0]["content"]

    remember(tmp_path / "m.md", "Windows", "The Start menu has nothing clickable in it.")
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
    remember(tmp_path / "m.md", "Windows", "Something learned earlier.")
    it = brain(config=config)
    assert it.remembered() == ""

    from jarvis.tools import build_toolbox

    assert "remember" not in build_toolbox(config).names


def test_the_bullets_are_readable_without_the_grouping(tmp_path):
    """`bullets` is what the duplicate check and the tests count with."""
    path = tmp_path / "memories.md"
    remember(path, "Windows", "One line.")
    remember(path, "Personal", "Another line.")
    assert bullets(path) == ["One line.", "Another line."]
