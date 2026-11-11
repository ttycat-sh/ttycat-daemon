"""What Codex's own records say a thread has started: its subagents, and in the app, its commands.

Codex writes which thread spawned which into its thread database, and the Codex app keeps a list of
the commands its threads have running. Neither is a documented interface, so both are read loosely and
read-only: anything unfamiliar means no count, never a wrong one. No hook is needed for any of it.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

CODEX_HOME = Path.home() / ".codex"
DB_NAME = re.compile(r"^state_(\d+)\.sqlite$")
SAFE_ID = re.compile(r"^[0-9A-Fa-f-]{16,64}$")
LOOK_EVERY_S = 2.0
STALE_S = 15 * 60        # a subagent silent this long mid-turn may have died with its Codex: it isn't counted
TAIL_BYTES = 400_000     # where a turn starting or ending is looked for
TURN = re.compile(rb'"type":\s*"(task_started|task_complete|turn_aborted)"')


def newest_db(home: Path) -> Path | None:
    best = None
    try:
        for path in home.iterdir():
            hit = DB_NAME.match(path.name)
            if hit and (best is None or int(hit.group(1)) > best[0]):
                best = (int(hit.group(1)), path)
    except OSError:
        return None
    return best[1] if best else None


def open_spawns(home: Path, since: float) -> list[tuple[str, str]] | None:
    """(the thread that spawned it, its log) for each subagent not closed and heard from lately. None: can't tell."""
    db = newest_db(home)
    if not db:
        return None
    try:
        connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.2)
        try:
            rows = connection.execute(
                "select e.parent_thread_id, t.rollout_path from thread_spawn_edges e join threads t on t.id = e.child_thread_id "
                "where e.status = 'open' and t.updated_at >= ? limit 200", (int(since),)).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return None  # busy, or a Codex that keeps this differently
    root = str(home / "sessions")
    return [(parent, path) for parent, path in rows
            if isinstance(parent, str) and isinstance(path, str) and path.startswith(root)]


def mid_turn(path: str) -> bool:
    """Whether the last thing its log says about a turn is that one started."""
    try:
        size = os.stat(path).st_size
        with open(path, "rb") as log:
            log.seek(max(0, size - TAIL_BYTES))
            marks = TURN.findall(log.read())
    except OSError:
        return False
    return bool(marks) and marks[-1] == b"task_started"


def alive(pid) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def app_commands(home: Path, is_alive=alive) -> dict[str, int]:
    """Thread -> the commands the Codex app has running for it. It lists ones long gone too."""
    try:
        listed = json.loads((home / "process_manager" / "chat_processes.json").read_text())
    except (OSError, ValueError):
        return {}
    out: dict[str, int] = {}
    for entry in listed if isinstance(listed, list) else ():
        if isinstance(entry, dict) and isinstance(entry.get("conversationId"), str) and is_alive(entry.get("osPid")):
            out[entry["conversationId"]] = out.get(entry["conversationId"], 0) + 1
    return out


@dataclass
class Seen:
    subagents: dict[str, int] = field(default_factory=dict)
    commands: dict[str, int] = field(default_factory=dict)


class Record:
    def __init__(self, home: Path = CODEX_HOME, clock=time.monotonic, now=time.time, spawns=open_spawns,
                 running=mid_turn, commands=app_commands):
        self.home, self.clock, self.now = home, clock, now
        self.spawns, self.running, self.commands = spawns, running, commands
        self.looked_at: float | None = None
        self.seen: Seen | None = None

    def look(self) -> Seen | None:
        """None: Codex's record can't be read."""
        if self.looked_at is None or self.clock() - self.looked_at >= LOOK_EVERY_S:
            self.looked_at = self.clock()
            spawned = self.spawns(self.home, self.now() - STALE_S)
            if spawned is None:
                self.seen = None
            else:
                seen = Seen(commands=self.commands(self.home))
                for parent, log in spawned:
                    if self.running(log):
                        seen.subagents[parent] = seen.subagents.get(parent, 0) + 1
                self.seen = seen
        return self.seen
