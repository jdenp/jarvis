"""The record of what JARVIS has heard.

Append only, with monotonic ids so a client can say "everything after 7" and
never miss or repeat an utterance across reconnects. ``wait_for`` blocks until
something arrives, which is what makes an agent integration an interrupt rather
than a polling loop.
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
    """One thing the user said."""

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

    def add(self, text: str) -> Utterance:
        """Record an utterance and wake anything waiting."""
        with self._condition:
            utterance = Utterance.new(self._next_id, text)
            self._next_id += 1
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
