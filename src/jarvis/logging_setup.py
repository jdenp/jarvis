"""Logging: bare messages on the console, full detail in a rotating file."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 3


def configure(log_dir: Path, level: str = "INFO", console: bool = True) -> logging.Logger:
    """Set up the ``jarvis`` logger. Safe to call more than once.

    ``console=False`` keeps stdout clean, which an MCP server needs - anything
    printed there is parsed as JSON-RPC by the agent on the other end.
    """
    logger = logging.getLogger("jarvis")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    if logger.handlers:
        return logger

    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / "jarvis.log", maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
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
