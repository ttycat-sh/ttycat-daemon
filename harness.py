"""What each kind of agent can tell about itself beyond its state, and how sure that is.

Agents differ: one keeps a record of its subagents, another has none at all. So each kind says what it
can report and how it gets it, and nothing is shown for what it can't: no count is not a zero.

EXACT comes from the agent's own record. ESTIMATED is worked out from outside, as from the process
table. Everything here only reads, and needs nothing added to anyone's setup.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

import codex_spawns
import opencode

EXACT, ESTIMATED = "exact", "estimated"
SHELLS = {"sh", "bash", "zsh", "dash", "fish"}
RUNS_A_COMMAND = {"-c", "-lc", "-ic", "-lic"}
PROJECTS = Path.home() / ".claude" / "projects"
SESSION_ID = re.compile(r"^[0-9a-fA-F-]{36}$")  # it goes into a path
LOOK_AGAIN_S = 5.0
CLOCKS_DIFFER_S = 5.0


@dataclass
class Running:
    """One agent, as far as it's known: any of these can be missing."""
    session: str = ""
    pid: int | None = None
    started: float = 0.0   # when its process began, in time.time()'s terms
    cwd: str = ""
    alone: bool = True     # the only agent of its kind in that folder: what's known by folder is its own
    in_app: bool = False   # a thread of the agent's own app, not something in a terminal


def runs_a_command(command: str) -> bool:
    """`zsh -c ...`: a shell given something to run, not one someone is typing into."""
    words = command.split()
    return bool(words) and words[0].rsplit("/", 1)[-1].lstrip("-") in SHELLS and any(w in RUNS_A_COMMAND for w in words[1:3])


class Harness:
    """A kind of agent. `subagents` and `shells` say how sure each count is; None: it can't tell."""
    subagents: str | None = None
    shells: str | None = None

    def count_subagents(self, agent: Running) -> int | None:
        return None

    def is_shell(self, command: str) -> bool:
        return runs_a_command(command)

    def counts(self, agent: Running, processes) -> dict:
        """What goes with its row: `sub` and `sh`, and in `guess` which of them are estimates."""
        out, guess = {}, []
        if self.subagents:
            n = self.count_subagents(agent)
            if n is not None:
                out["sub"] = n
                if self.subagents == ESTIMATED:
                    guess.append("sub")
        if self.shells and agent.pid is not None:
            out["sh"] = sum(1 for _, command in processes.children(agent.pid) if self.is_shell(command))
            if self.shells == ESTIMATED:
                guess.append("sh")
        if guess:
            out["guess"] = guess
        return out


class InATerminal(Harness):
    """An agent that runs its commands as shells of its own: they can be counted under its process."""
    shells = ESTIMATED


class Claude(Harness):
    """Claude Code keeps a record of each subagent beside the session's log, marked when it finishes.
    The format is its own and can change: a record that isn't as expected counts as nothing."""
    subagents = EXACT
    shells = ESTIMATED

    def __init__(self, root: Path = PROJECTS, clock=time.monotonic):
        self.root, self.clock = root, clock
        self.folders: dict[str, Path] = {}
        self.looked: dict[str, float] = {}
        self.over: dict[str, set[str]] = {}   # session -> records that can't be running any more

    def _folder(self, session: str) -> Path | None:
        folder = self.folders.get(session)
        if folder:
            return folder
        if self.clock() - self.looked.get(session, -LOOK_AGAIN_S) < LOOK_AGAIN_S:
            return None
        self.looked[session] = self.clock()
        found = list(self.root.glob(f"*/{session}/subagents"))
        if found:
            self.folders[session] = found[0]
            return found[0]
        return None

    def count_subagents(self, agent: Running) -> int | None:
        if not SESSION_ID.match(agent.session):
            return None
        folder = self._folder(agent.session)
        if folder is None:
            return 0  # it has started none
        over = self.over.setdefault(agent.session, set())
        running = 0
        try:
            records = list(folder.glob("agent-*.meta.json"))
        except OSError:
            return None
        for record in records:
            if record.name in over:
                continue
            try:
                info = json.loads(record.read_text())
                written = record.stat().st_mtime
            except (OSError, ValueError):
                continue  # being written: look again next time
            # One that a process left unfinished when it was killed stays so for good: only what
            # this process started can still be running.
            if not isinstance(info, dict) or info.get("finished") or written < agent.started - CLOCKS_DIFFER_S:
                over.add(record.name)
            else:
                running += 1
        return running

    def is_shell(self, command: str) -> bool:
        # Its own commands start from a snapshot of your shell. Other things it starts through a shell don't.
        return runs_a_command(command) and "/shell-snapshots/" in command


class OpenCode(InATerminal):
    """OpenCode 2 answers for itself: see opencode.py. It knows its sessions by folder, not by terminal,
    so two in one folder can't be told apart and neither gets a count. Without its server (OpenCode 1)
    it's an agent in a terminal like any other."""
    subagents = EXACT
    shells = EXACT

    def __init__(self, server=None):
        self.server = server or opencode.Server()

    def counts(self, agent: Running, processes) -> dict:
        seen = self.server.look()
        if seen is None:
            return self._from_outside(agent, processes)
        place = opencode.folder(agent.cwd)
        if not place or not agent.alone:
            return {}
        return {"sub": seen.subagents.get(place, 0), "sh": seen.shells.get(place, 0)}

    def _from_outside(self, agent: Running, processes) -> dict:
        if agent.pid is None:
            return {}
        return {"sh": sum(1 for _, command in processes.children(agent.pid) if self.is_shell(command)), "guess": ["sh"]}


class Codex(InATerminal):
    """Codex records which thread spawned which, and its app lists the commands its threads have
    running: see codex_spawns.py. In a terminal its commands are shells under its process."""
    subagents = EXACT

    def __init__(self, record=None):
        self.record = record or codex_spawns.Record()

    def counts(self, agent: Running, processes) -> dict:
        out = {}
        seen = self.record.look() if codex_spawns.SAFE_ID.match(agent.session) else None
        if seen is not None:
            out["sub"] = seen.subagents.get(agent.session, 0)
        if agent.pid is not None:
            out["sh"] = sum(1 for _, command in processes.children(agent.pid) if self.is_shell(command))
            out["guess"] = ["sh"]
        elif seen is not None and agent.in_app:
            out["sh"] = seen.commands.get(agent.session, 0)
        return out


def known() -> dict[str, Harness]:
    """By the name herdr gives each agent. Anything else reports no counts."""
    return {"claude": Claude(), "codex": Codex(), "opencode": OpenCode(), "pi": InATerminal()}
