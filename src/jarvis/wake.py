"""Wake word matching.

The wake word is always stripped when present; whether it is *required* is a
separate question the caller answers.

Matching is deliberately loose. Speech recognition mangles proper nouns, and it
mangles them differently depending on the accent - "jarvis" comes back as Jovis,
Jervis, Darvus, Java's, Jobs. An exact match means the assistant ignores you and
you have no idea why, so an approximate one is checked before giving up.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

# How close a word has to sound, and how different in length it may be, before
# it counts as an attempt at the wake word.
DEFAULT_THRESHOLD = 0.78
# One character, not two: it is the difference between catching "jovis" and
# treating "jars in the cupboard" as an instruction.
MAX_LENGTH_DIFFERENCE = 1
_WORD = re.compile(r"[a-z0-9']+")


def wake_pattern(words: tuple[str, ...]) -> re.Pattern[str]:
    """One case insensitive pattern matching any configured wake word.

    Longest first, so "hey jarvis" wins over "jarvis".
    """
    alternatives = sorted((re.escape(word.strip()) for word in words if word.strip()), key=len)
    if not alternatives:
        return re.compile(r"(?!)")  # matches nothing
    return re.compile(rf"\b(?:{'|'.join(reversed(alternatives))})\b", re.IGNORECASE)


def split_wake_word(pattern: re.Pattern[str], heard: str) -> tuple[bool, str]:
    """Return whether JARVIS was named, and the utterance without its name.

    The remainder is "" when the wake word was the whole thing.
    """
    match = pattern.search(heard)
    if match is None:
        return False, heard
    remainder = heard[: match.start()] + " " + heard[match.end() :]
    return True, remainder.strip(" ,.!?")


def sounds_like_wake_word(token: str, words: tuple[str, ...], threshold: float) -> bool:
    """Whether one word is a plausible mis-hearing of a wake word.

    The length guard is what keeps this from firing on short common words: a
    three letter word is never a mangled "jarvis", however the ratio comes out.
    """
    token = token.strip().lower()
    if not token:
        return False
    for word in words:
        word = word.strip().lower()
        if not word or " " in word:
            continue  # multi word wake words are handled by the exact pattern
        if abs(len(token) - len(word)) > MAX_LENGTH_DIFFERENCE:
            continue
        if SequenceMatcher(None, token, word).ratio() >= threshold:
            return True
    return False


class WakeMatcher:
    """Exact match first, then an approximate one."""

    def __init__(
        self,
        words: tuple[str, ...],
        fuzzy: bool = True,
        threshold: float = DEFAULT_THRESHOLD,
    ) -> None:
        self.words = words
        self.fuzzy = fuzzy
        self.threshold = threshold
        self._pattern = wake_pattern(words)

    def split(self, heard: str) -> tuple[bool, str]:
        """Whether JARVIS was addressed, and the instruction without its name."""
        addressed, remainder = split_wake_word(self._pattern, heard)
        if addressed or not self.fuzzy:
            return addressed, remainder
        return self._split_fuzzily(heard)

    def _split_fuzzily(self, heard: str) -> tuple[bool, str]:
        for match in _WORD.finditer(heard.lower()):
            if not sounds_like_wake_word(match.group(), self.words, self.threshold):
                continue
            remainder = heard[: match.start()] + " " + heard[match.end() :]
            return True, remainder.strip(" ,.!?")
        return False, heard
