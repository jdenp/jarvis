"""What JARVIS has learned, in a file it writes itself.

The desk is full of things that are only discoverable by getting them wrong.
Spotify's tree is empty until the window has been focused once; the Start menu
has nothing clickable in it; a particular window's Close button is the fourth of
four with the same label. None of that is in the model's weights and none of it
is worth putting in the system prompt by hand, because the next machine's list is
different.

So the model writes its own. `remember()` appends a line, the whole list is read
back into the system prompt at the start of every turn, and a lesson learned at
half past two is in play at half past three. Plain markdown bullets on purpose -
editing or deleting one is a text edit, nothing parses it beyond reading the
lines, and a memory that has gone wrong is fixed by opening the file.

Capped, because it is prompt. Past `brain.max_memory_chars` the oldest go, which
is the right end to lose: the desk changes, and a lesson about an application
that has since been updated is worse than no lesson.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

logger = logging.getLogger("jarvis.memories")

HEADER = """\
# What JARVIS has learned

Written by JARVIS itself, and read back into its system prompt at the start of
every turn. Plain bullets - edit or delete any of them freely, and add your own.
Anything about how this particular desktop behaves belongs here; anything about
one conversation does not.
"""


def load(root: Path, written: Sequence[Path] = (), limit: int = 2000) -> list[str]:
    """Everything known about this machine, ready for the prompt.

    Every markdown file under `root`, at any depth. The ones in `written` are
    the ones JARVIS adds to itself, and they are the only ones capped: they grow
    on their own, where the rest are written by hand and bounded by hand.
    Trimming the curated half to make room for the accumulated half would be the
    wrong way round, so reference comes first and comes whole.

    Only the bullets, from all of them. The prose around them is for whoever
    opens the file, and would be tokens spent saying nothing to a model.
    """
    if not root.is_dir():
        return []

    grown = {path.resolve() for path in written}
    reference: list[str] = []
    learned: list[str] = []
    for found in sorted(root.rglob("*.md")):
        if found.resolve() in grown:
            learned += newest(bullets(found), limit)
        else:
            reference += bullets(found)
    return reference + learned


def bullets(path: Path) -> list[str]:
    """The list items in one markdown file, in order, unwrapped."""
    try:
        return bullets_in(path.read_text(encoding="utf-8"))
    except OSError:
        return []


def bullets_in(text: str) -> list[str]:
    """The list items in some markdown, in order, unwrapped.

    A hand written file wraps at eighty columns, and reading only the first line
    of each bullet silently halves it - "press the Windows key, type its name"
    with the rest of the sentence left in the file.
    """
    found: list[str] = []
    for line in text.splitlines():
        if line.startswith("- "):
            found.append(line[2:].strip())
        elif found and line.startswith((" ", "\t")) and line.strip():
            found[-1] += " " + line.strip()
    return [lesson for lesson in found if lesson]


def newest(found: list[str], limit: int) -> list[str]:
    """As many as fit, counting back from the most recently written."""
    if limit <= 0:
        return found

    kept: list[str] = []
    spent = 0
    for lesson in reversed(found):
        spent += len(lesson) + 3
        if spent > limit:
            break
        kept.append(lesson)
    return list(reversed(kept))


def remember(path: Path, lesson: str, limit: int = 2000) -> str:
    """Write one lesson down, and say what happened.

    Refuses a repeat rather than appending it. A model that has just been told
    its memory is in the prompt will offer to write down what it read there.
    """
    lesson = " ".join(lesson.split())
    if not lesson:
        return "Nothing to remember - the lesson was empty."

    existing = bullets(path)
    if any(lesson.lower() == already.lower() for already in existing):
        return "Already remembered, so nothing was added."

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(HEADER + "\n", encoding="utf-8")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"- {lesson}\n")
    except OSError as exc:
        logger.warning("Could not write %s - %s", path, exc)
        return f"Could not write it down - {exc}"

    logger.info("Remembered: %s", lesson)
    dropped = len(existing) + 1 - len(newest(bullets(path), limit))
    if dropped > 0:
        return (
            f"Remembered. The list is full, so the {dropped} oldest are no longer read "
            "back - edit the file if one of those mattered."
        )
    return "Remembered. It will be in your prompt from the next thing they say."


def as_prompt(lessons: list[str]) -> str:
    """The lessons as a block for the system prompt, or nothing at all."""
    if not lessons:
        return ""
    return "WHAT IS KNOWN ABOUT THIS MACHINE:\n" + "\n".join(f"- {x}" for x in lessons)
