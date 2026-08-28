"""Logging: bare messages on the console, full detail in a rotating file."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# The budget is split across this many files, so that hitting the limit drops
# the oldest quarter of the history rather than all of it at once.
FILES = 4
DEFAULT_MAX_MB = 100


def configure(
    log_dir: Path, level: str = "INFO", console: bool = True, max_mb: int = DEFAULT_MAX_MB
) -> logging.Logger:
    """Set up the ``jarvis`` logger. Safe to call more than once.

    ``console=False`` keeps stdout clean, which a command whose output is read
    by something else needs.

    ``max_mb`` is the whole budget, not the size of one file - what the log and
    its backups take up between them. 0 turns rotation off and lets it grow.
    """
    logger = logging.getLogger("jarvis")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    if logger.handlers:
        return logger

    log_dir.mkdir(parents=True, exist_ok=True)
    each = max(0, int(max_mb)) * 1024 * 1024 // FILES
    file_handler = RotatingFileHandler(
        log_dir / "jarvis.log",
        maxBytes=each,
        backupCount=FILES - 1 if each else 0,
        encoding="utf-8",
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(file_handler)

    if console:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    return logger
