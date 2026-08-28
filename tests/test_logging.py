"""The log file and what it is allowed to grow to.

Every tool call, every result and every thought goes in here, so the question is
not whether it gets big but what happens when it does.
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from jarvis.logging_setup import FILES, configure


def handler(logger: logging.Logger) -> RotatingFileHandler:
    return next(h for h in logger.handlers if isinstance(h, RotatingFileHandler))


def fresh(name: str = "jarvis"):
    """configure() returns early if the logger already has handlers."""
    logger = logging.getLogger(name)
    for existing in list(logger.handlers):
        logger.removeHandler(existing)
    return logger


def test_the_budget_is_split_across_the_files_rather_than_being_one_of_them():
    """100MB means 100MB on disk, not 100MB four times over. Splitting it is
    what makes reaching the limit drop the oldest quarter of the history
    instead of the lot."""
    fresh()
    try:
        rotating = handler(configure(_dir(), console=False, max_mb=100))
        assert rotating.maxBytes == 100 * 1024 * 1024 // FILES
        assert rotating.backupCount == FILES - 1
        assert rotating.maxBytes * (rotating.backupCount + 1) == 100 * 1024 * 1024
    finally:
        fresh()


def test_a_bigger_budget_is_taken_at_face_value():
    """Somebody with a large disk and no interest in this should get what they
    asked for rather than a cap somebody else chose."""
    fresh()
    try:
        rotating = handler(configure(_dir(), console=False, max_mb=20480))
        assert rotating.maxBytes == 20480 * 1024 * 1024 // FILES
    finally:
        fresh()


def test_zero_lets_it_grow_forever():
    """maxBytes of 0 is how RotatingFileHandler spells "never roll over", and a
    backupCount alongside it would be a file that is never written."""
    fresh()
    try:
        rotating = handler(configure(_dir(), console=False, max_mb=0))
        assert rotating.maxBytes == 0
        assert rotating.backupCount == 0
    finally:
        fresh()


def _dir():
    import tempfile
    from pathlib import Path

    return Path(tempfile.mkdtemp(prefix="jarvis-log-test-"))
