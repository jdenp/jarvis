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
CONTAINED = 0.6  # of a transcript matching one run inside a reply
LEAST_RUN = 12  # characters, so a short command cannot match by coincidence


def normalise(text: str) -> str:
    """Lowercase words only, so punctuation and casing cannot defeat a match."""
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def sounds_like(heard: str, spoken: str) -> bool:
    """Whether a transcript is plausibly a recording of something JARVIS said."""
    if not heard or not spoken:
        return False

    # autojunk off: over 200 characters it starts treating common letters as junk,
    # and a long reply is where this has to work.
    matcher = SequenceMatcher(None, heard, spoken, autojunk=False)
    if matcher.ratio() >= SIMILARITY:
        return True

    # Everything below matches a part rather than the whole, which needs a floor:
    # "no" is inside "technology", and swallowing a real answer is worse than
    # letting an echo through. An echo of a short line is caught by ratio anyway.
    if min(len(heard), len(spoken)) < LEAST_RUN:
        return False
    if heard in spoken or spoken in heard:
        return True

    # A reply cut off mid sentence comes back as a piece of the middle of a much
    # longer utterance, which the whole-string ratio scores near zero. Both tests
    # matter - one long run rules out coincidence, and the total rules out a
    # request that happens to open with a few of JARVIS's words.
    blocks = matcher.get_matching_blocks()
    longest = max((block.size for block in blocks), default=0)
    matched = sum(block.size for block in blocks)
    return longest >= LEAST_RUN and matched >= CONTAINED * len(heard)


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
