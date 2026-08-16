"""Second line of defence against JARVIS transcribing its own voice.

`Microphone` drops audio recorded while speaking; this catches what slips past.
Hearing yourself is lossy and clips the start, so matching is on containment or
similarity rather than equality.
"""

from __future__ import annotations

import re
import time
from collections import deque
from difflib import SequenceMatcher

MEMORY_SECONDS = 20.0
SIMILARITY = 0.75


def normalise(text: str) -> str:
    """Lowercase words only, so punctuation and casing cannot defeat a match."""
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def sounds_like(heard: str, spoken: str) -> bool:
    """Whether a transcript is plausibly a recording of something JARVIS said."""
    if not heard or not spoken:
        return False
    if heard in spoken or spoken in heard:
        return True
    return SequenceMatcher(None, heard, spoken).ratio() >= SIMILARITY


class EchoGuard:
    """Remembers what was spoken recently, and recognises it coming back."""

    def __init__(self, memory_seconds: float = MEMORY_SECONDS, keep: int = 8) -> None:
        self.memory_seconds = memory_seconds
        self._spoken: deque[tuple[float, str]] = deque(maxlen=keep)

    def remember(self, text: str) -> None:
        normalised = normalise(text)
        if normalised:
            self._spoken.append((time.monotonic(), normalised))

    def is_echo(self, heard: str) -> bool:
        candidate = normalise(heard)
        if not candidate:
            return False
        cutoff = time.monotonic() - self.memory_seconds
        return any(
            spoken_at >= cutoff and sounds_like(candidate, spoken)
            for spoken_at, spoken in self._spoken
        )
