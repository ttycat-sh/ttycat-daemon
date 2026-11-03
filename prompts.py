"""Describes a blocked agent that the board can't answer, from its terminal text.

Only for showing. Nothing here answers anything: the board answers Claude Code tool
prompts through its permission hook (approvals.py), and everything else, such as
folder trust or other agents, is answered in the terminal.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

TERMINAL_NOTE = "Answer this one in the terminal"
MAX_BODY = 340

RULE = re.compile(r"^\s*─{10,}\s*$")
DASHED = re.compile(r"^\s*╌{10,}\s*$")
OPTION = re.compile(r"^\s*(?:❯\s*)?(\d+)\.\s+(.*\S)\s*$")


@dataclass
class Ask:
    tool: str = "ASK"
    verb: str = "is asking"
    body: str = ""
    detail: str = ""
    note: str = TERMINAL_NOTE


def _region(text: str) -> list[str]:
    """The lines under the last full-width rule: where agents draw their prompts."""
    lines = text.splitlines()
    last = -1
    for i, line in enumerate(lines):
        if RULE.match(line):
            last = i
    return lines[last + 1 :]


def _before_options(lines: list[str]) -> list[str]:
    """Stops at the first numbered option. What follows is options, their wrapped
    continuation lines, and key hints: none of it says what's being asked."""
    for i, line in enumerate(lines):
        if OPTION.match(line.strip()):
            return lines[:i]
    return lines


def _generic(lines: list[str], note: str = TERMINAL_NOTE) -> Ask:
    useful = []
    for line in _before_options(lines):
        s = line.strip()
        if not s or s.startswith(("❯", "›", "Enter to", "Esc to", "Press enter", "Tip:")) or DASHED.match(s):
            continue
        useful.append(s)
    body = " ".join(useful)[:MAX_BODY] or "Waiting at its terminal"
    return Ask(body=body, detail=(useful[0] if useful else "waiting")[:70], note=note)


def _claude_bash(lines: list[str]) -> Ask | None:
    """Claude Code's shell prompt, shown as what it is: the command, without the words around it.
    The command sits between dashed lines; without them, on the lines above the question."""
    lines = [line.strip() for line in _before_options(lines)]
    lines = [line for line in lines if line and not line.startswith("Tip:")]
    if not lines or lines[0] != "Bash command":
        return None
    dashes = [i for i, line in enumerate(lines) if DASHED.match(line)]
    if len(dashes) >= 2:
        command = lines[dashes[0] + 1 : dashes[1]]
    else:
        command = [line for line in lines[1:] if not line.startswith("Do you want")]
    if not command:
        return None
    text = " ".join(command)
    return Ask(tool="BASH", verb="wants to run", body=text[:MAX_BODY], detail=text[:70])


def parse_codex(text: str) -> Ask:
    lines = _region(text)
    note = "Codex can only be answered in its terminal"
    for line in lines:
        s = line.strip()
        if s.startswith("$ "):
            cmd = s[2:]
            return Ask(tool="SHELL", verb="wants to run", body=cmd[:MAX_BODY], detail=cmd[:70], note=note)
    return _generic(lines, note)


def parse(agent: str, text: str) -> Ask:
    if agent == "codex":
        return parse_codex(text)
    lines = _region(text)
    return _claude_bash(lines) or _generic(lines)
