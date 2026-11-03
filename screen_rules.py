"""What an agent's screen means, for agents that can't say themselves.

A port of herdr's detection engine (src/detect/manifest.rs in https://github.com/herdrdev/herdr,
Apache-2.0), using its rules, in agent_rules.json. Each rule has a priority, a region of the screen,
and things that must or mustn't be there. The highest priority match wins, the first of equals, and
no match means idle. It only answers "what state is this": nothing here ever answers the agent.

Checked rule by rule against herdr's own `agent explain`: see tests/test_screen_rules.py.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

RULES = Path(__file__).resolve().parent / "agent_rules.json"
_BRACED_HEX = re.compile(r"\\[xu]\{([0-9A-Fa-f]+)\}")
_COUNTED = re.compile(r"^(bottom_lines|bottom_non_empty_lines|top_non_empty_lines)\((\d+)\)$")


def _python_regex(pattern: str) -> re.Pattern:
    """The rules are written for Rust's regex crate. What differs is spelled differently here."""
    translated = _BRACED_HEX.sub(lambda m: f"\\U{int(m.group(1), 16):08x}", pattern)
    translated = translated.replace(r"\z", r"\Z")
    translated = translated.replace(r"\p{Alphabetic}", r"[^\W\d_]")
    return re.compile(translated)


def _lines(text: str) -> list[str]:
    """As Rust's str::lines: only \\n ends a line, a \\r before it goes, and no empty last line."""
    parts = text.split("\n")
    if parts and parts[-1] == "":
        parts.pop()
    return [p[:-1] if p.endswith("\r") else p for p in parts]


@dataclass
class Gate:
    contains: list[str]
    regex: list[re.Pattern]
    line_regex: list[re.Pattern]
    all: list["Gate"]
    any: list["Gate"]
    not_: list["Gate"]

    @staticmethod
    def of(raw: dict) -> "Gate":
        return Gate([n.lower() for n in raw.get("contains", [])],
                    [_python_regex(p) for p in raw.get("regex", [])],
                    [_python_regex(p) for p in raw.get("line_regex", [])],
                    [Gate.of(g) for g in raw.get("all", [])],
                    [Gate.of(g) for g in raw.get("any", [])],
                    [Gate.of(g) for g in raw.get("not", [])])

    def matches(self, text: str, lower: str, lines: list[str]) -> bool:
        return (all(n in lower for n in self.contains)
                and all(r.search(text) for r in self.regex)
                and all(any(r.search(line) for line in lines) for r in self.line_regex)
                and all(g.matches(text, lower, lines) for g in self.all)
                and (not self.any or any(g.matches(text, lower, lines) for g in self.any))
                and not any(g.matches(text, lower, lines) for g in self.not_))


@dataclass
class Rule:
    id: str
    state: str
    priority: int
    region: str
    gate: Gate
    visible_idle: bool
    visible_blocker: bool
    visible_working: bool
    skip_state_update: bool


@dataclass
class Detection:
    state: str  # idle | working | blocked | unknown
    rule: str = ""
    visible_idle: bool = False
    visible_blocker: bool = False
    skip_state_update: bool = False


def _join_from(lines: list[str], index: int) -> str:
    return "".join(line + "\n" for line in lines[index:])


def _join_to(lines: list[str], index: int) -> str:
    return "".join(line + "\n" for line in lines[:index])


def _is_rule_line(line: str) -> bool:
    trimmed = line.strip()
    count = len(trimmed) - len(trimmed.lstrip("─"))
    if not count:
        return False
    return not trimmed[count:].strip() or count >= 3


def _prompt_line(line: str) -> bool:
    return line == "›" or line.startswith("› ")


def _block_marker(line: str) -> bool:
    return line.startswith(("•", "■", "✗", "✓"))


def _current_prompt(lines: list[str]) -> int | None:
    index = next((i for i in range(len(lines) - 1, -1, -1) if _prompt_line(lines[i])), None)
    if index is None or any(_block_marker(line) for line in lines[index + 1:]):
        return None
    return index


def _box_top(lines: list[str]) -> int | None:
    seen = 0
    for index in range(len(lines) - 1, -1, -1):
        if _is_rule_line(lines[index]):
            seen += 1
            if seen == 2:
                return index
    return None


def region_of(spec: str, screen: str, title: str = "", progress: str = "") -> str:
    spec = spec.strip()
    if spec == "osc_title":
        return title
    if spec == "osc_progress":
        return progress
    lines = _lines(screen)
    if spec == "whole_recent":
        return screen
    if spec == "after_last_prompt_marker":
        index = next((i for i in range(len(lines) - 1, -1, -1) if _prompt_line(lines[i])), None)
        return screen if index is None else _join_from(lines, index + 1)
    if spec == "before_current_prompt_marker":
        index = _current_prompt(lines)
        return screen if index is None else _join_to(lines, index)
    if spec == "whole_recent_without_current_prompt_marker":
        return "" if _current_prompt(lines) is not None else screen
    if spec == "current_prompt_block_marker":
        index = _current_prompt(lines)
        return next((line for line in reversed(lines[:index]) if _block_marker(line)), "") if index is not None else ""
    if spec == "after_current_prompt_block_marker":
        index = _current_prompt(lines)
        if index is None:
            return ""
        marker = next((i for i in range(index - 1, -1, -1) if _block_marker(lines[i])), None)
        return "" if marker is None else _join_from(lines, marker)
    if spec == "prompt_box_body":
        top = _box_top(lines)
        if top is None:
            return ""
        end = next((i for i in range(top + 1, len(lines)) if _is_rule_line(lines[i])), len(lines))
        return "".join(line + "\n" for line in lines[top + 1:end])
    if spec in ("above_prompt_box", "last_non_empty_above_prompt_box"):
        top = _box_top(lines)
        above = screen if top is None else _join_to(lines, top)
        if spec == "above_prompt_box":
            return above
        return next((line for line in reversed(_lines(above)) if line.strip()), "")
    if spec == "after_last_horizontal_rule":
        index = next((i for i in range(len(lines) - 1, -1, -1) if _is_rule_line(lines[i])), None)
        return screen if index is None else _join_from(lines, index + 1)
    counted = _COUNTED.match(spec)
    if counted:
        name, count = counted.group(1), int(counted.group(2))
        if name == "bottom_lines":
            return _join_from(lines, max(0, len(lines) - count))
        filled = [i for i, line in enumerate(lines) if line.strip()]
        if not filled or not count:
            return ""
        if name == "bottom_non_empty_lines":
            return _join_from(lines, filled[-count:][0])
        return _join_to(lines, filled[:count][-1] + 1)
    return ""


_shared = None


def shared_rules() -> "ScreenRules":
    global _shared
    if _shared is None:
        _shared = ScreenRules()
    return _shared


class ScreenRules:
    def __init__(self, path: Path = RULES):
        self.agents: dict[str, list[Rule]] = {}
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return  # without rules, agents are shown as running: nothing's guessed
        for agent, manifest in data.get("agents", {}).items():
            self.agents[agent] = [
                Rule(r["id"], r.get("state", "unknown"), r.get("priority", 0), r.get("region", "whole_recent"), Gate.of(r),
                     r.get("visible_idle", False), r.get("visible_blocker", False), r.get("visible_working", False),
                     r.get("skip_state_update", False))
                for r in manifest["rules"]]

    def knows(self, agent: str) -> bool:
        return agent in self.agents

    def verdicts(self, agent: str, screen: str, title: str = "", progress: str = "") -> dict[str, bool]:
        """Every rule's answer: what's compared with herdr's own."""
        return {rule.id: self._matches(rule, screen, title, progress) for rule in self.agents.get(agent, [])}

    @staticmethod
    def _matches(rule: Rule, screen: str, title: str, progress: str) -> bool:
        text = region_of(rule.region, screen, title, progress)
        return rule.gate.matches(text, text.lower(), _lines(text))

    def detect(self, agent: str, screen: str, title: str = "", progress: str = "") -> Detection:
        best = None
        for rule in self.agents.get(agent, []):
            if (best is None or rule.priority > best.priority) and self._matches(rule, screen, title, progress):
                best = rule
        if best is None:
            return Detection("idle" if agent in self.agents else "unknown")
        return Detection(best.state, best.id, best.visible_idle and best.state == "idle",
                         best.visible_blocker and best.state == "blocked", best.skip_state_update)
