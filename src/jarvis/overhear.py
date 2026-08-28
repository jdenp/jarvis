"""Reading a connected agent's own prose off disk, and speaking what it never said.

Every other attempt to make an agent speak its answer failed the same way: the
schema shapes a call that happens and cannot cause one, so anything JARVIS puts
in a tool result is advice. Four versions of that advice were built and removed.

This one does not ask. The client writes its whole conversation to disk as it
goes - assistant messages, thinking, tool calls - so the answer the user never
heard is sitting in a file. From one real session, the two lines that were
written and thrown away:

    "Spotify is open. Let me press play to start music."
    "Done - Spotify's open and Katy Perry's Harleys In Hawaii is playing now."

A lead-in and a closing report, both perfectly sayable. This reads them and says
them. It needs no cooperation from the agent, no protocol feature, and no
capability the client has to advertise - which is the whole point, because
sampling is deprecated and no client here has ever offered it.

Jank, unarguably: it depends on another program's on-disk format. Hence
`service.overhear`, and hence everything here failing quietly rather than loudly.

**You point it at the directory.** `service.agent_sessions` is a directory of
session directories, each holding `<id>.messages.json` - which is one common
layout and not a standard, so there is no default and an empty setting simply
switches this off. Nothing is guessed at: a file that is not a list of messages
is left alone and logged once, because reading somebody's half-understood format
out loud is a worse failure than staying quiet.

The message shape expected inside is the ordinary Anthropic API one - parts typed
`text`, `thinking`, `tool_use` - so any client storing raw API messages works as
is, and `transcripts` is the seam for one that lays its files out differently.
What nothing can be done for is a client that never writes the conversation down.
There has to be a transcript to overhear.

Only the MCP path needs any of this. With the brain answering, the reply is the
speech and there is nothing to overhear.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

# Tidying prose for a synthesiser is tts.py's job, not this module's - the brain
# needs the same thing done to its own replies. Re-exported because the tests
# and the reasoning for it live here.
from .tts import for_speaking

logger = logging.getLogger("jarvis.overhear")

# Past this, prose was written to be read rather than heard - a table, a diff, a
# summary with headings - and reading it aloud is worse than silence.
MAX_SPOKEN_CHARS = 400

# Anywhere in the text, these mean it was written for the eye.
NOT_FOR_SPEAKING = ("```", "<function")

# At the start of any line, the same. Checked per line rather than as a substring
# so that a heading on the very first line is caught as well as a later one.
NOT_AT_A_LINE_START = ("#", "- ", "* ", "|", ">")


def looks_like_a_transcript(blob: dict) -> bool:
    """Whether this is something this module knows how to read.

    A list of messages, each with a role. Checked so that a file which merely
    happens to sit in the configured directory is left alone - reading a format
    nobody has verified out loud is a worse failure than staying quiet.
    """
    if not isinstance(blob, dict):
        return False
    messages = blob.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    return all(isinstance(message, dict) and "role" in message for message in messages)


def transcripts(root: Path) -> list[Path]:
    """Every session transcript under a client's data directory, newest first."""
    try:
        found = [
            session / f"{session.name}.messages.json"
            for session in root.iterdir()
            if session.is_dir()
        ]
    except OSError:
        return []
    live = [path for path in found if path.is_file()]
    return sorted(live, key=lambda path: path.stat().st_mtime, reverse=True)


def newest_transcript(root: Path) -> Path | None:
    """The transcript written to most recently, which is this conversation.

    A guess, and the only one available: the MCP server is told nothing about
    which session spawned it. Wrong only if two conversations are running at
    once, and then it speaks the other one's answer.
    """
    found = transcripts(root)
    return found[0] if found else None


def assistant_prose(blob: dict) -> list[str]:
    """Everything the agent wrote as text, in order.

    Not its thinking, which is verbose, internal and frequently about the user
    rather than to them. Not its tool calls. Just the prose it addressed to
    whoever it believed was reading.
    """
    said: list[str] = []
    for message in blob.get("messages") or []:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str):
            said.append(content)
            continue
        for part in content or []:
            if isinstance(part, dict) and part.get("type") == "text":
                said.append(str(part.get("text") or ""))
    return [text.strip() for text in said if text and text.strip()]


def worth_speaking(text: str, limit: int = MAX_SPOKEN_CHARS) -> bool:
    """Whether a line of prose can be read aloud without embarrassment.

    The agent wrote it for a reader, so some of it is a table or a code block.
    Better to stay quiet than to read markdown out.
    """
    body = text.strip()
    if not body or len(text) > limit:
        return False
    if any(marker in body for marker in NOT_FOR_SPEAKING):
        return False
    for line in body.splitlines():
        start = line.lstrip()
        if start.startswith(NOT_AT_A_LINE_START):
            return False
        # "1. first" and the rest of a numbered list.
        head, _, rest = start.partition(". ")
        if head.isdigit() and rest:
            return False
    return True


class Overheard:
    """Remembers how much of a transcript has already been read.

    Position rather than content, because the agent repeats itself and two
    identical lines twenty minutes apart are two things worth saying.
    """

    def __init__(self, root: Path, limit: int = MAX_SPOKEN_CHARS) -> None:
        self.root = root
        self.limit = limit
        self._seen: dict[Path, int] = {}
        self._stamps: dict[Path, tuple[float, int]] = {}
        self._complained = False

    def catch_up(self) -> None:
        """Mark everything already written as read, without speaking any of it.

        Called when a reply first falls due. Whatever is in the file already
        belongs to earlier turns, and saying it now would be answering a
        finished conversation.
        """
        path = newest_transcript(self.root)
        if path is None:
            return
        blob = self._read(path)
        if blob is not None and looks_like_a_transcript(blob):
            self._seen[path] = len(assistant_prose(blob))

    def anything_new(self) -> list[str]:
        """Prose written since the last look, filtered to what can be spoken."""
        path = newest_transcript(self.root)
        if path is None:
            return []
        try:
            stat = path.stat()
        except OSError:
            return []
        # Size as well as time. Windows timestamps are coarse enough that two
        # rewrites inside the same tick share an mtime, and the client rewrites
        # the whole file every message - so mtime alone loses one.
        stamp = (stat.st_mtime, stat.st_size)
        if self._stamps.get(path) == stamp:
            return []
        self._stamps[path] = stamp

        blob = self._read(path)
        if blob is None:
            return []
        if not looks_like_a_transcript(blob):
            if not self._complained:
                self._complained = True
                logger.warning(
                    "%s is not a list of messages, so it is not being read out. See "
                    "overhear.py if you want to teach it another transcript format.",
                    path,
                )
            return []
        prose = assistant_prose(blob)
        already = self._seen.get(path, 0)
        self._seen[path] = len(prose)
        fresh = (line for line in prose[already:] if worth_speaking(line, self.limit))
        return [tidy for tidy in (for_speaking(line) for line in fresh) if tidy]

    def _read(self, path: Path) -> dict | None:
        """Parse a transcript, tolerating a half-written one.

        The client rewrites the whole file, so a read can land mid-write and see
        truncated JSON. Nothing to do but wait for the next poll.
        """
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


def catching_up(root: Path) -> bool:
    """Whether there is anything at that path worth watching.

    No default location: one client's layout is not a standard, so the path is
    configured or the feature is off.
    """
    return bool(transcripts(root))
