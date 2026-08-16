"""Cleaning up MCP servers that outlived their client.

An MCP server exits by itself when the client closes the pipe, which is the
normal case. It does not when the client is *killed*: the launch chain is
cmd -> uv -> jarvis -> python, and killing the top leaves the rest holding the
pipe open, so the bottom never sees EOF. Do that a few times and a dozen idle
copies are sitting there holding the virtualenv open.

Two defences. Each server watches whether its own parent is still alive, and
the service sweeps up anything already stranded when it starts.
"""

from __future__ import annotations

import logging
import os
import threading

logger = logging.getLogger("jarvis.reap")

WATCH_INTERVAL = 5.0
MCP_MARKERS = ("jarvis", "mcp")


def _processes():
    """psutil is optional at runtime - without it, reaping is a no-op."""
    try:
        import psutil
    except ImportError:  # pragma: no cover - dependency is declared
        logger.debug("psutil is not installed, skipping cleanup.")
        return None
    return psutil


def looks_like_mcp_server(command_line: list[str] | None) -> bool:
    """Whether a command line is one of our MCP servers.

    Both markers, in order, so `jarvis serve` and unrelated MCP servers are
    left alone.
    """
    if not command_line:
        return False
    joined = " ".join(command_line).lower().replace("\\", "/")
    index = joined.find(MCP_MARKERS[0])
    return index != -1 and joined.find(MCP_MARKERS[1], index + 1) != -1


def orphaned_mcp_servers(psutil, exclude_pid: int | None = None) -> list:
    """Our MCP servers whose parent has gone."""
    stranded = []
    for process in psutil.process_iter(["pid", "cmdline", "ppid"]):
        try:
            if process.info["pid"] == exclude_pid:
                continue
            if not looks_like_mcp_server(process.info["cmdline"]):
                continue
            if not psutil.pid_exists(process.info["ppid"]):
                stranded.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return stranded


def reap_orphans(exclude_pid: int | None = None) -> int:
    """Terminate stranded MCP servers. Returns how many were cleared."""
    psutil = _processes()
    if psutil is None:
        return 0

    cleared = 0
    for process in orphaned_mcp_servers(psutil, exclude_pid):
        try:
            process.terminate()
            cleared += 1
            logger.info("Cleared a stranded MCP server (pid %s).", process.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            logger.debug("Could not clear pid %s - %s", process.pid, exc)
    return cleared


def exit_when_orphaned(interval: float = WATCH_INTERVAL) -> None:
    """Exit this process once whoever launched it has gone.

    Started by `jarvis mcp`. os._exit rather than sys.exit because this runs on
    a daemon thread while the server blocks on stdio, where an exception would
    go nowhere.
    """
    psutil = _processes()
    if psutil is None:
        return
    try:
        parent = psutil.Process().parent()
    except Exception:  # pragma: no cover - platform dependent
        return
    if parent is None:
        return

    def watch() -> None:
        while True:
            threading.Event().wait(interval)
            try:
                alive = parent.is_running() and parent.status() != psutil.STATUS_ZOMBIE
            except Exception:
                alive = False
            if not alive:
                logger.info("Nothing is listening to us any more, exiting.")
                os._exit(0)

    threading.Thread(target=watch, name="jarvis-orphan-watch", daemon=True).start()
