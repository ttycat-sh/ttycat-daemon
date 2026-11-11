"""What ttycat can add to an agent's own setup, and whether it's there. Nothing here is ever done unasked.

Most of what ttycat shows needs nothing added: it reads what the agent keeps. Some things an agent only
gives to something it was told about, as with a hook: each of those is an Addition. It says in words what
it gives and what it writes, so that can be shown before anyone says yes, and it's installed or removed
only by the command that names it. An agent that turns up on this Mac later is found the same way, and
its additions wait to be asked about like any other.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import harness

MISSING, INSTALLED, BROKEN, UNREADABLE = "missing", "installed", "broken", "unreadable"
# Where a program you installed yourself tends to be, when the PATH this runs with is the system's.
PROGRAM_FOLDERS = ("~/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", "~/.opencode/bin", "~/.bun/bin", "~/.npm-global/bin")


@dataclass
class Addition:
    id: str
    title: str                      # what you get
    detail: str                     # a sentence more
    writes: str                     # what it changes of yours, and where
    state: Callable[[], str]        # MISSING, INSTALLED, BROKEN (there, but what it runs is gone) or UNREADABLE
    install: Callable[[], None]     # also what repairs a broken one
    remove: Callable[[], None]


@dataclass
class Agent:
    id: str
    name: str
    programs: tuple[str, ...]
    folders: tuple[str, ...]        # its own, in your home: there once it has been run
    additions: list[Addition] = field(default_factory=list)


def on_this_mac(agent: Agent, home: Path | None = None, which=shutil.which) -> bool:
    home = home or Path.home()
    if any((home / folder).is_dir() for folder in agent.folders):
        return True
    for program in agent.programs:
        if which(program) or any(os.access(os.path.join(os.path.expanduser(folder), program), os.X_OK) for folder in PROGRAM_FOLDERS):
            return True
    return False


def shows(kind: harness.Harness | None) -> list[str]:
    """What's shown of this kind of agent with nothing added."""
    out = ["What it's doing"]
    if kind and kind.subagents:
        out.append("Subagents" if kind.subagents == harness.EXACT else "Subagents, as an estimate")
    if kind and kind.shells:
        out.append("Open shells" if kind.shells == harness.EXACT else "Open shells, as an estimate")
    return out


def can(kind: harness.Harness | None) -> dict:
    """The same, for a table: how sure each count is, with what it can't tell left out."""
    out = {"state": harness.EXACT}
    if kind and kind.subagents:
        out["sub"] = kind.subagents
    if kind and kind.shells:
        out["sh"] = kind.shells
    return out


def state_of(addition: Addition) -> str:
    try:
        return addition.state()
    except Exception:  # noqa: BLE001  a settings file that can't be read is a state, not a crash
        return UNREADABLE


def listing(agents: list[Agent], kinds: dict[str, harness.Harness], found=on_this_mac) -> dict:
    return {"agents": [
        {"id": a.id, "name": a.name, "found": found(a), "shows": shows(kinds.get(a.id)), "can": can(kinds.get(a.id)),
         "adds": [{"id": x.id, "title": x.title, "detail": x.detail, "writes": x.writes, "state": state_of(x)} for x in a.additions]}
        for a in agents]}


def change(agents: list[Agent], action: str, addition_id: str) -> dict:
    """Installs or removes the one addition named. Whatever goes wrong, it says so and the state is read again."""
    addition = next((x for a in agents for x in a.additions if x.id == addition_id), None)
    if addition is None:
        return {"ok": False, "msg": f"nothing called {addition_id}"}
    try:
        (addition.install if action == "install" else addition.remove)()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "msg": str(e) or type(e).__name__, "state": state_of(addition)}
    return {"ok": True, "state": state_of(addition)}


def supported(additions: dict[str, list[Addition]]) -> list[Agent]:
    """The agents ttycat knows by name, each with what can be added to it."""
    return [
        Agent("claude", "Claude Code", ("claude",), (".claude",), additions.get("claude", [])),
        Agent("codex", "Codex", ("codex",), (".codex",), additions.get("codex", [])),
        Agent("opencode", "OpenCode", ("opencode",), (".config/opencode", ".local/share/opencode"), additions.get("opencode", [])),
        Agent("pi", "Pi", ("pi",), (".pi",), additions.get("pi", [])),
    ]
