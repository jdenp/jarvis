"""Wake word matching.

The wake word is always stripped when present; whether it is *required* is a
separate question the caller answers.
"""

from __future__ import annotations

import re


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
