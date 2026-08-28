"""The record of what JARVIS has heard.

Append only, with monotonic ids so a client holding a cursor never misses or
repeats an utterance across a reconnect. ``wait_for`` blocks until one arrives.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass(frozen=True)
class Utterance:
    """One thing the user said, verbatim.

    No wake word and no filtering - the agent decides what was meant for it.
    """

    id: int
    text: str
    at: str

    @classmethod
    def new(cls, id: int, text: str) -> Utterance:
        return cls(id=id, text=text, at=datetime.now(UTC).isoformat(timespec="seconds"))

    def as_dict(self) -> dict:
        return asdict(self)


class Transcript:
    """In memory ring of recent utterances, mirrored to a JSONL file."""

    def __init__(self, path: Path | None = None, keep: int = 200) -> None:
        self.path = path
        self._items: deque[Utterance] = deque(maxlen=keep)
        self._next_id = 1
        self._condition = threading.Condition()
        self._paused = False
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._resume(path)

    def _resume(self, path: Path) -> None:
        """Continue the id sequence across restarts rather than replaying."""
        if not path.is_file():
            return
        last = 0
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    last = max(last, int(json.loads(line)["id"]))
                except (ValueError, KeyError, TypeError):
                    continue
        self._next_id = last + 1

    @property
    def cursor(self) -> int:
        """Id of the most recent utterance. Ask for everything after it."""
        with self._condition:
            return self._next_id - 1

    def pause(self) -> bool:
        """Stop recording new utterances. Returns True if was not already paused."""
        with self._condition:
            if self._paused:
                return False
            self._paused = True
            return True

    def resume(self) -> bool:
        """Resume recording new utterances. Returns True if was not already resumed."""
        with self._condition:
            if not self._paused:
                return False
            self._paused = False
            return True

    @property
    def paused(self) -> bool:
        """Whether transcription is currently paused."""
        with self._condition:
            return self._paused

    def add(self, text: str, always: bool = False) -> Utterance:
        """Record an utterance and wake anything waiting.

        If paused, the utterance still gets an id but does not enter the ring
        or notify waiters, so clients holding a cursor never see it. `always`
        is for the things that did not come from the microphone: pausing closes
        the ears, and somebody typing has plainly chosen to say something.
        """
        with self._condition:
            utterance = Utterance.new(self._next_id, text)
            self._next_id += 1
            if always or not self._paused:
                self._items.append(utterance)
                self._condition.notify_all()
        self._append_to_file(utterance)
        return utterance

    def _append_to_file(self, utterance: Utterance) -> None:
        if self.path is None:
            return
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(utterance.as_dict()) + "\n")

    def since(self, cursor: int) -> list[Utterance]:
        """Everything recorded after ``cursor``."""
        with self._condition:
            return [item for item in self._items if item.id > cursor]

    def wait_for(self, cursor: int, timeout: float) -> list[Utterance]:
        """Block until there is something after ``cursor``, or time out.

        Returns an empty list on timeout, which the caller should treat as
        "nothing said yet", not as an error.
        """

        def has_something() -> bool:
            return any(item.id > cursor for item in self._items)

        with self._condition:
            if not self._condition.wait_for(has_something, timeout=timeout):
                return []
            return [item for item in self._items if item.id > cursor]
