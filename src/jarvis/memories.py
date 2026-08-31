"""What JARVIS has learned, in a file it writes itself.

The desk is full of things that are only discoverable by getting them wrong.
Spotify's tree is empty until the window has been focused once; the Start menu
has nothing clickable in it; a particular window's Close button is the fourth of
four with the same label. None of that is in the model's weights and none of it
is worth putting in the system prompt by hand, because the next machine's list is
different. Nor is the other half of it: what they do for a living, that they want
Chrome and never Edge, which of their windows is the one they meant.

So the model writes its own. `remember()` puts a bullet under a heading, the
whole list is read back into the system prompt at the start of every turn, and a
lesson learned at half past two is in play at half past three. Plain markdown on
purpose - editing or deleting a line is a text edit, nothing parses it beyond
reading the bullets, and a memory that has gone wrong is fixed by opening the
file.

The headings are the model's own and they are what keeps this readable past
thirty lines. One file holds everything it works out, so without them it is a
hundred unsorted sentences about windows, keyboard shortcuts and somebody's job.

Capped, because it is prompt. Past `brain.max_memory_chars` the top of the file
stops being read, and remember() says so when that starts happening.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

logger = logging.getLogger("jarvis.memories")

# Bullets that arrive with no heading over them. Named rather than dropped: a
# line worth keeping is worth keeping badly filed.
OTHER = "Other"

HEADER = """\
# What JARVIS has learned

Written by JARVIS itself, and read back into its system prompt at the start of
every turn. Bullets under `##` headings - edit or delete any of them freely,
rename or merge the headings, and add your own.

Everything it works out about this desk belongs here, and so does everything it
picks up about the person at it. Anything about one conversation does not.
"""

# A file's worth of bullets, grouped: [("Navigation", ["...", "..."]), ...].
Groups = list[tuple[str, list[str]]]


def load(
    root: Path,
    written: Sequence[Path] = (),
    limit: int = 2000,
    ignore: Sequence[Path] = (),
) -> Groups:
    """Everything known, grouped by heading, ready for the prompt.

    Every markdown file under `root`, at any depth. The ones in `written` are
    the ones JARVIS adds to itself, and they are the only ones capped: they grow
    on their own, where the rest are written by hand and bounded by hand.
    Trimming the curated half to make room for the accumulated half would be the
    wrong way round, so reference comes first and comes whole.

    A written file over the cap is left out altogether - see `over_limit`.

    `ignore` is the session's own notes, which belong at the end of the prompt
    and would be in it twice if this picked them up as well. Named rather than
    kept out by where they live, because where they live is a config option.

    Only the bullets and the headings over them. The prose around them is for
    whoever opens the file, and would be tokens spent saying nothing to a model.
    """
    if not root.is_dir():
        return []

    grown = {path.resolve() for path in written}
    skip = {path.resolve() for path in ignore}
    reference: Groups = []
    learned: Groups = []
    for found in sorted(root.rglob("*.md")):
        if found.resolve() in skip:
            continue
        groups = sections(found)
        if found.resolve() in grown:
            if over_limit(groups, limit):
                continue
            learned = merged(learned, groups)
        else:
            reference = merged(reference, groups)
    return merged(reference, learned)


def sections(path: Path) -> Groups:
    """One file's bullets, grouped under the heading each sits below."""
    try:
        return sections_in(path.read_text(encoding="utf-8"))
    except OSError:
        return []


def sections_in(text: str) -> Groups:
    """The same, from markdown in hand.

    A hand written file wraps at eighty columns, and reading only the first line
    of each bullet silently halves it - "press the Windows key, type its name"
    with the rest of the sentence left in the file.
    """
    groups: Groups = []
    heading = OTHER
    found: list[str] = []

    def close() -> None:
        kept = [lesson for lesson in found if lesson]
        if kept:
            groups.append((heading, kept))

    for line in text.splitlines():
        if line.startswith("## "):
            close()
            heading, found = clean(line), []
        elif line.startswith("- "):
            found.append(line[2:].strip())
        elif found and line.startswith((" ", "\t")) and line.strip():
            found[-1] += " " + line.strip()
    close()
    return groups


def bullets(path: Path) -> list[str]:
    """One file's bullets with the grouping flattened away."""
    return [lesson for _, lessons in sections(path) for lesson in lessons]


def bullets_in(text: str) -> list[str]:
    """The same, from markdown in hand."""
    return [lesson for _, lessons in sections_in(text) for lesson in lessons]


def clean(heading: str) -> str:
    """A heading as it goes in the file: one line, no hashes, no empty."""
    return " ".join(heading.replace("#", " ").split()) or OTHER


def merged(groups: Groups, more: Groups) -> Groups:
    """Two grouped lists, with same-named headings joined rather than repeated.

    Across files as well as within one, so a `## Navigation` that JARVIS wrote
    lands under the same heading as the shipped reference of the same name.
    """
    out: Groups = [(name, list(lessons)) for name, lessons in groups]
    where = {name.lower(): at for at, (name, _) in enumerate(out)}
    for name, lessons in more:
        at = where.get(name.lower())
        if at is None:
            where[name.lower()] = len(out)
            out.append((name, list(lessons)))
        else:
            out[at][1].extend(lessons)
    return out


def spent(groups: Groups) -> int:
    """What a file of bullets costs in the prompt, in characters."""
    return sum(len(lesson) + 3 for _, lessons in groups for lesson in lessons)


def over_limit(groups: Groups, limit: int) -> int:
    """What it costs, when that is over the limit. 0 when it is not.

    All of it or none of it. Reading as much as fits sounds kinder and is not:
    the top of the file is where the oldest and best worn lessons are, so
    quietly losing that end is how a stale duplicate further down ends up
    believed instead. Better to say so once, loudly, and read nothing.
    """
    cost = spent(groups)
    return cost if 0 < limit < cost else 0


def remember(path: Path, heading: str, lesson: str, limit: int = 2000) -> str:
    """Write one line down under its heading, and say what happened.

    Refuses a repeat rather than appending it. A model that has just been told
    its memory is in the prompt will offer to write down what it read there.
    """
    heading = clean(heading)
    lesson = " ".join(lesson.split())
    if not lesson:
        return "Nothing to remember - the lesson was empty."

    existing = bullets(path)
    if any(lesson.lower() == already.lower() for already in existing):
        return "Already remembered, so nothing was added."

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = path.read_text(encoding="utf-8") if path.exists() else HEADER
        path.write_text(filed(body, heading, lesson), encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not write %s - %s", path, exc)
        return f"Could not write it down - {exc}"

    logger.info("Remembered under %s: %s", heading, lesson)
    if cost := over_limit(sections(path), limit):
        return (
            f"Remembered, but {path.name} is now {cost} characters against a limit "
            f"of {limit}, so none of it is read back until it is trimmed."
        )
    return f"Remembered, under {heading}. It is in your prompt from the next thing they say."


def filed(body: str, heading: str, lesson: str) -> str:
    """The file with one more bullet in it, under a heading made if it is new.

    Appended to the end of its own section rather than the end of the file,
    which is the whole point of the headings.
    """
    lines = body.splitlines()
    wanted = f"## {heading}".lower()
    at = next((i for i, line in enumerate(lines) if line.strip().lower() == wanted), None)
    if at is None:
        while lines and not lines[-1].strip():
            lines.pop()
        return "\n".join([*lines, "", f"## {heading}", f"- {lesson}"]) + "\n"

    end = at + 1
    while end < len(lines) and not lines[end].startswith("#"):
        end += 1
    while end > at + 1 and not lines[end - 1].strip():
        end -= 1
    lines.insert(end, f"- {lesson}")
    return "\n".join(lines) + "\n"


def rewrite(path: Path, heading: str, lessons: Sequence[str]) -> bool:
    """Swap one section's bullets for a shorter set. False if nothing changed.

    The other half of `remember`, which only ever appends - right for a lesson
    learned in a turn, and no use at all for merging six of them into one.
    """
    heading = clean(heading)
    body = [f"- {' '.join(lesson.split())}" for lesson in lessons if lesson.strip()]
    if not body:
        return False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False

    wanted = f"## {heading}".lower()
    at = next((i for i, line in enumerate(lines) if line.strip().lower() == wanted), None)
    if at is None:
        return False
    end = at + 1
    while end < len(lines) and not lines[end].startswith("#"):
        end += 1

    gap = [""] if end > at + 1 and not lines[end - 1].strip() else []
    lines[at + 1 : end] = body + gap
    try:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not write %s - %s", path, exc)
        return False
    logger.info("Compacted %s down to %d line(s).", heading, len(body))
    return True


def as_prompt(groups: Groups) -> str:
    """The lessons as a block for the system prompt, or nothing at all."""
    if not groups:
        return ""
    return "WHAT YOU HAVE LEARNED SO FAR:\n" + as_lines(groups)


def as_session(groups: Groups) -> str:
    """The same for the block at the end of the prompt rather than the front."""
    if not groups:
        return ""
    return (
        "WHAT YOU HAVE WRITTEN DOWN SINCE THIS SESSION STARTED. It is not in the "
        "list above yet - it is folded in when the room goes quiet.\n" + as_lines(groups)
    )


def assimilate(session: Path, disk: Path, limit: int = 2000) -> int:
    """Fold what one session wrote into the file that outlives it.

    A file merge and not a model call, because both halves use the same
    headings: a lesson filed under `## Windows` this afternoon goes under
    `## Windows`. `remember` refuses the repeats, and tidying what is left is
    the compaction pass on the same idle minute.
    """
    if session == disk:
        return 0
    moved = 0
    for heading, lessons in sections(session):
        for lesson in lessons:
            remember(disk, heading, lesson, limit)
            moved += 1
    if moved:
        logger.info("Folded %d session note(s) into %s.", moved, disk.name)
        try:
            session.unlink()
        except OSError as exc:
            logger.warning("Could not clear %s - %s", session, exc)
    return moved


def as_lines(groups: Groups) -> str:
    """The same without the label, in the markdown the file is written in."""
    out: list[str] = []
    for name, lessons in groups:
        out.append(f"\n## {name}")
        out += [f"- {lesson}" for lesson in lessons]
    return "\n".join(out).strip()
