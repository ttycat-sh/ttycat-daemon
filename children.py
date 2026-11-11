"""The processes an agent has started, from the process table: the one thing every agent in a terminal has.

It says what's running under an agent, never what the agent meant by it, so anything counted this way is
an estimate. Read-only, and looked at every few seconds, not with every poll.
"""
from __future__ import annotations

import subprocess
import time

LOOK_EVERY_S = 3.0


def scan_children() -> list[tuple[int, int, str]]:
    """(pid, its parent, command) for every process."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,ppid=,command="], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
    return rows


class Processes:
    """What's running under a process, once it has stayed: there on two looks in a row. A command
    that runs and returns is the agent working, not something it has left open."""

    def __init__(self, scan=scan_children, clock=time.monotonic, every: float = LOOK_EVERY_S):
        self.scan, self.clock, self.every = scan, clock, every
        self.looked_at: float | None = None
        self.seen: set[int] = set()
        self.under: dict[int, list[tuple[int, str]]] = {}

    def _look(self):
        now = self.clock()
        if self.looked_at is not None and now - self.looked_at < self.every:
            return
        self.looked_at = now
        rows = self.scan()
        under: dict[int, list[tuple[int, str]]] = {}
        for pid, parent, command in rows:
            if pid in self.seen:
                under.setdefault(parent, []).append((pid, command))
        self.seen, self.under = {pid for pid, _, _ in rows}, under

    def children(self, pid: int) -> list[tuple[int, str]]:
        """(pid, command) of what it started itself and is still running."""
        self._look()
        return list(self.under.get(pid, ()))


_shared: Processes | None = None


def shared() -> Processes:
    """One look at the process table serves everything that asks."""
    global _shared
    if _shared is None:
        _shared = Processes()
    return _shared
