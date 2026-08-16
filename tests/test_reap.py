"""Clearing MCP servers that outlived their client."""

from __future__ import annotations

import pytest

from jarvis.reap import looks_like_mcp_server, orphaned_mcp_servers


@pytest.mark.parametrize(
    "command_line",
    [
        ["uv", "run", "--directory", "C:/Repos/jarvis", "jarvis", "mcp"],
        ["jarvis", "mcp"],
        [r"C:\Repos\jarvis\.venv\Scripts\python.exe", "-m", "jarvis", "mcp"],
    ],
)
def test_recognises_our_mcp_servers(command_line):
    assert looks_like_mcp_server(command_line) is True


@pytest.mark.parametrize(
    "command_line",
    [
        None,
        [],
        ["uv", "run", "jarvis", "serve"],
        ["npx", "-y", "@someone/other-mcp-server"],
        ["python", "-m", "mcp", "something"],
        ["jarvis", "status"],
    ],
)
def test_leaves_everything_else_alone(command_line):
    """Sweeping up must not take out the service itself, or someone else's
    MCP server."""
    assert looks_like_mcp_server(command_line) is False


class FakeProcess:
    def __init__(self, pid, cmdline, ppid):
        self.pid = pid
        self.info = {"pid": pid, "cmdline": cmdline, "ppid": ppid}


class FakePsutil:
    NoSuchProcess = type("NoSuchProcess", (Exception,), {})
    AccessDenied = type("AccessDenied", (Exception,), {})

    def __init__(self, processes, live_pids):
        self._processes = processes
        self._live = set(live_pids)

    def process_iter(self, _attrs=None):
        return list(self._processes)

    def pid_exists(self, pid):
        return pid in self._live


def test_only_the_stranded_ones_are_listed():
    processes = [
        FakeProcess(100, ["uv", "run", "jarvis", "mcp"], ppid=10),  # parent alive
        FakeProcess(101, ["uv", "run", "jarvis", "mcp"], ppid=99),  # parent gone
        FakeProcess(102, ["uv", "run", "jarvis", "serve"], ppid=98),  # not an mcp server
    ]
    stranded = orphaned_mcp_servers(FakePsutil(processes, live_pids={10}))
    assert [p.pid for p in stranded] == [101]


def test_our_own_process_is_never_swept_up():
    processes = [FakeProcess(101, ["jarvis", "mcp"], ppid=99)]
    fake = FakePsutil(processes, live_pids=set())
    assert orphaned_mcp_servers(fake, exclude_pid=101) == []
    assert [p.pid for p in orphaned_mcp_servers(fake)] == [101]
