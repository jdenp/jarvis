"""Reading the agent's own prose off disk, and speaking what it never said.

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
sampling is deprecated and Cline never offered it anyway.

Jank, unarguably: it depends on another program's on-disk format. Hence
`service.overhear`, and hence everything here failing quietly rather than loudly.

**This reads Cline, and only Cline.** The layout is its own - one directory per
session under `~/.cline/data/sessions`, holding `<id>.messages.json` beside
`<id>.json` - and so is the envelope, which carries `origin.source`, an `agent`
name and Cline's own version string. A transcript without that envelope is left
alone rather than guessed at, because the alternative is reading somebody's
half-understood file out loud.

Adapting it to another client is a small job and a real one: the content shape
inside `messages` is the ordinary Anthropic API shape - parts typed `text`,
`thinking`, `tool_use` - so anything storing raw API messages needs only a new
reader, and `looks_like_cline` plus `transcripts` are the seam. What it cannot be
adapted to is a client that never writes the conversation down. There has to be a
transcript to overhear.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("jarvis.overhear")

# Past this, prose was written to be read rather than heard - a table, a diff, a
# summary with headings - and reading it aloud is worse than silence.
MAX_SPOKEN_CHARS = 400

# Anywhere in the text, these mean it was written for the eye.
NOT_FOR_SPEAKING = ("```", "<function")

# At the start of any line, the same. Checked per line rather than as a substring
# so that a heading on the very first line is caught as well as a later one.
NOT_AT_A_LINE_START = ("#", "- ", "* ", "|", ">")


def looks_like_cline(blob: dict) -> bool:
    """Whether this is a transcript in the format this module understands.

    Cline stamps its own envelope on: an `origin` with a source and its version,
    and a session id. Checked so that a file which merely happens to sit in the
    configured directory is left alone. Reading a format nobody has verified out
    loud is a worse failure than staying quiet.
    """
    if not isinstance(blob, dict) or not isinstance(blob.get("messages"), list):
        return False
    origin = blob.get("origin")
    return isinstance(origin, dict) and "source" in origin and bool(blob.get("sessionId"))


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


def for_speaking(text: str) -> str:
    """Prose tidied into something a synthesiser can read.

    It was typed for a screen, so emphasis and emoji come through as themselves:
    SAPI reads `**947 tokens**` as "asterisk asterisk nine four seven". Stripped
    rather than rejected, because the sentence underneath is a perfectly good
    answer. Newlines go too - a spoken line is one line.
    """
    cleaned = text
    for marker in ("**", "__", "`", "*", "#"):
        cleaned = cleaned.replace(marker, "")
    # Anything outside the basic plane is an emoji or a symbol, and has no
    # pronunciation worth hearing.
    cleaned = "".join(character for character in cleaned if ord(character) < 0x2500)
    return " ".join(cleaned.split())


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
        if blob is not None and looks_like_cline(blob):
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
        if not looks_like_cline(blob):
            if not self._complained:
                self._complained = True
                logger.warning(
                    "%s is not in the format this understands - Cline's, with an origin "
                    "and a session id. Staying quiet rather than reading it out. See "
                    "overhear.py if you want to teach it another client's transcript.",
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


def default_sessions_dir() -> Path:
    """Where Cline keeps its transcripts: one directory per session, each holding
    `<session-id>.messages.json`.

    `service.cline_sessions` overrides it, for a portable install or a version
    that moves the directory. It does not make this work with another client -
    that needs a reader for their format, not a different path.
    """
    return Path.home() / ".cline" / "data" / "sessions"
