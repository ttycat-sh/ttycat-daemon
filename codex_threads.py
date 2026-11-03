"""Codex threads: the app's, which never run in a terminal, and the CLI's where no screen can be read.

Everything comes from the two records Codex keeps: its thread database and each thread's session log.
Neither is a documented interface, so they're read loosely, read-only: anything unfamiliar means fewer
rows, never a wrong answer. What the log can't say is whether Codex is waiting for approval: a thread
waiting looks just like one running a long command, so it says it may be, and doesn't claim either.
"""
from __future__ import annotations

import collections
import json
import os
import re
import sqlite3
import subprocess
import time
from pathlib import Path

from activity import Event, _one_line, _when, prompt_summary, said_summary
from sources import HerdrSource

CODEX_HOME = Path.home() / ".codex"
PREFIX = "c:"
ID_LEN = 8             # from the end: the start of these ids is the time, shared by threads made together
RECENT_S = 30 * 60     # a thread with no process to watch: this long after its last activity, it's gone
STALE_S = 15 * 60      # mid-turn and silent this long: it may have died with its app, so say we can't tell
QUIET_S = 20           # silent this long, with a tool call open, in a thread that can ask for approval
TAIL_BYTES = 2_000_000 # the first look at a log. They reach 70 MB, and only the end matters.
MAX_EVENTS = 40
SCAN_EVERY_S = 2.0
SAFE_ID = re.compile(r"^[0-9A-Fa-f-]{16,64}$")
DB_NAME = re.compile(r"^state_(\d+)\.sqlite$")
APP_SOURCE, CLI_SOURCE = "vscode", "cli"  # the app says vscode; `exec` and subagents aren't anyone's to watch
STOPPED = "Stopped"
MAY_BE_WAITING = "quiet for {}s: may be waiting for approval"


def thread_pane(thread_id: str) -> str:
    return PREFIX + thread_id[-ID_LEN:]


def newest_db(home: Path = CODEX_HOME) -> Path | None:
    """state_5.sqlite, today: the number goes up with Codex's own migrations."""
    best = None
    try:
        for path in home.iterdir():
            hit = DB_NAME.match(path.name)
            if hit and (best is None or int(hit.group(1)) > best[0]):
                best = (int(hit.group(1)), path)
    except OSError:
        return None
    return best[1] if best else None


def read_threads(home: Path = CODEX_HOME, sources=(APP_SOURCE, CLI_SOURCE), since: float = 0) -> list[dict]:
    db = newest_db(home)
    if not db:
        return []
    columns = ["id", "rollout_path", "cwd", "title", "source", "approval_mode", "updated_at"]
    marks = ",".join("?" * len(sources))
    try:
        connection = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.2)
        try:
            rows = connection.execute(
                f"select {','.join(columns)} from threads where archived = 0 and source in ({marks}) and updated_at >= ? "
                "order by updated_at desc limit 80", (*sources, int(since))).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return []  # busy, or a shape this doesn't know
    root = str(home / "sessions")
    return [t for t in (dict(zip(columns, row)) for row in rows)
            if isinstance(t["id"], str) and SAFE_ID.match(t["id"]) and str(t["rollout_path"]).startswith(root)]


class Trail:
    """What one session log says, newest last."""

    def __init__(self, path: str):
        self.path = path
        self.offset: int | None = None
        self.running = False
        self.heard_turn = False
        self.tool_open = False
        self.events: collections.deque = collections.deque(maxlen=MAX_EVENTS)

    def feed(self, data: bytes):
        for raw in data.split(b"\n"):
            if raw.strip():
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(entry, dict):
                    self._entry(entry)

    def _add(self, at: float, tone: int, text: str | None, kind: str):
        if text:
            self.events.append(Event(at, tone, text, kind))

    def _entry(self, entry: dict):
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            return
        at, kind = _when(entry), entry.get("type")
        name = payload.get("type")
        if kind == "event_msg":
            if name == "task_started":
                self.running, self.heard_turn, self.tool_open = True, True, False
            elif name == "task_complete":
                self.running, self.heard_turn, self.tool_open = False, True, False
            elif name == "turn_aborted":
                self.running, self.heard_turn, self.tool_open = False, True, False
                self._add(at, 1, STOPPED, "tool")
            elif name == "user_message" and isinstance(payload.get("message"), str):
                self._add(at, 0, prompt_summary(payload["message"]), "you")
            elif name == "item_completed" and isinstance(payload.get("item"), dict):
                self._item(at, payload["item"])
        elif kind == "response_item":
            if name in ("function_call", "custom_tool_call"):
                self.tool_open = True
                self._tool(at, payload)
            elif name in ("function_call_output", "custom_tool_call_output"):
                self.tool_open = False
            elif name == "message" and payload.get("role") == "assistant":
                self._add(at, 0, said_summary(_message_text(payload)), "said")

    def _tool(self, at: float, payload: dict):
        name = str(payload.get("name") or "")
        if name == "apply_patch":
            hit = re.search(r"\*\*\* (?:Update|Add|Delete) File: (.+)", str(payload.get("input") or ""))
            self._add(at, 0, f"Edit {hit.group(1).rsplit('/', 1)[-1].strip()}" if hit else "Edit files", "tool")
        elif name in ("exec_command", "shell", "shell_command"):
            try:
                args = json.loads(payload.get("arguments") or "{}")
            except (ValueError, TypeError):
                args = {}
            command = args.get("cmd") or args.get("command") or ""
            if isinstance(command, list):
                command = " ".join(str(c) for c in command)
            self._add(at, 0, f"$ {_one_line(command)}" if command else "Running a command", "tool")
        elif name == "exec":
            self._add(at, 0, "Running a command", "tool")  # what, comes when it's finished

    def _item(self, at: float, item: dict):
        kind = item.get("type")
        if kind == "CommandExecution":
            command = item.get("command")
            command = " ".join(str(c) for c in command) if isinstance(command, list) else str(command or "")
            failed = item.get("exit_code") not in (0, None)
            text = f"{'Failed: ' if failed else ''}$ {_one_line(command)}"
            if self.events and self.events[-1].text in ("Running a command", f"$ {_one_line(command)}"):
                self.events.pop()  # now that it's known what it was
            self._add(at, 1 if failed else 0, text, "tool")
        elif kind == "McpToolCall" and item.get("tool"):
            self._add(at, 0, f"{item.get('server') or 'tool'}: {item['tool']}", "tool")


def _message_text(payload: dict) -> str:
    parts = payload.get("content")
    if not isinstance(parts, list):
        return ""
    return " ".join(str(p.get("text") or "") for p in parts if isinstance(p, dict))


class Logs:
    """Stands where activity.Activity stands for Claude, with the same questions answered."""

    def __init__(self, clock=time.time):
        self.clock = clock
        self.trails: dict[str, Trail] = {}
        self.notes: dict[str, str] = {}
        self.seen_at: dict[str, float] = {}

    def sync(self, paths: dict[str, str]):
        for pane in set(self.trails) - set(paths):
            del self.trails[pane]
            self.seen_at.pop(pane, None)
        for pane, path in paths.items():
            trail = self.trails.get(pane)
            if trail is None or trail.path != path:
                trail = self.trails[pane] = Trail(path)
            self._read(pane, trail)

    def _read(self, pane: str, trail: Trail):
        try:
            stat = os.stat(trail.path)
        except OSError:
            return
        self.seen_at[pane] = stat.st_mtime
        if trail.offset is not None and stat.st_size == trail.offset:
            return
        start = trail.offset
        if start is None or stat.st_size < start:  # first look, or it started over
            start = max(0, stat.st_size - TAIL_BYTES)
            trail.events.clear()
        try:
            with open(trail.path, "rb") as log:
                log.seek(start)
                data = log.read()
        except OSError:
            return
        if start and trail.offset is None:  # came in partway through a line
            first = data.find(b"\n")
            data, start = data[first + 1:], start + first + 1
        end = data.rfind(b"\n") + 1  # a line still being written waits for the next look
        trail.offset = start + end
        trail.feed(data[:end])

    def running(self, pane: str) -> bool:
        trail = self.trails.get(pane)
        if not trail:
            return False
        # A very long turn can start before the part of the log that was read.
        return trail.running or (not trail.heard_turn and self.silent(pane) < QUIET_S and trail.offset is not None and bool(trail.events))

    def silent(self, pane: str) -> float:
        return max(0.0, self.clock() - self.seen_at.get(pane, 0))

    def tool_open(self, pane: str) -> bool:
        trail = self.trails.get(pane)
        return bool(trail and trail.tool_open)

    def events(self, pane: str) -> list[Event]:
        trail = self.trails.get(pane)
        return list(trail.events) if trail else []

    def now(self, pane: str) -> str:
        note = self.notes.get(pane)
        if note:
            return note
        events = self.events(pane)
        return events[-1].shown if events else ""

    def last_said(self, pane: str) -> str:
        for event in reversed(self.events(pane)):
            if event.text == STOPPED:
                return STOPPED  # what it said before that isn't how the turn ended
            if event.kind == "said":
                return event.text
            if event.kind == "you":
                return ""
        return ""

    def status(self, pane: str, can_ask: bool, ran: set[str]) -> str:
        self.notes.pop(pane, None)
        if self.running(pane):
            silent = self.silent(pane)
            if silent >= STALE_S:
                return "unknown"
            ran.add(pane)
            if can_ask and silent >= QUIET_S and self.tool_open(pane):
                self.notes[pane] = MAY_BE_WAITING.format(int(silent))
            return "working"
        return "done" if pane in ran else "idle"


class CodexThreads(HerdrSource):
    """The Codex app's threads, as rows like any other. Nothing here writes to Codex, and nothing it
    asks can be answered from here yet."""

    def __init__(self, focus_app="Ghostty", pet=None, home: Path = CODEX_HOME, read=read_threads, clock=time.time, run=subprocess.run):
        super().__init__(focus_app=focus_app, hub=None, pet=pet)
        self.home, self.read, self.clock, self.run = home, read, clock, run
        self.activity = Logs(clock)
        self.threads: dict[str, dict] = {}
        self.ran: set[str] = set()
        self.scanned_at = 0.0

    def _follow(self, agents: list[dict], now: float):
        pass  # read already, in _herdr: the state comes from the same log

    def _herdr(self, *args) -> dict | None:
        if args[:2] != ("agent", "list"):
            return None
        now = self.clock()
        if now - self.scanned_at >= SCAN_EVERY_S:
            self.scanned_at = now
            fresh = {thread_pane(t["id"]): t for t in self.read(self.home, (APP_SOURCE,), now - RECENT_S)}
            # One that's mid-turn stays, however long that turn takes.
            self.threads = {**{p: t for p, t in self.threads.items() if self.activity.running(p)}, **fresh}
        self.activity.sync({pane: t["rollout_path"] for pane, t in self.threads.items()})
        # The database can outlive a log that's been moved or removed: then there's nothing to show.
        self.threads = {pane: t for pane, t in self.threads.items() if pane in self.activity.seen_at}
        agents = []
        for pane, thread in self.threads.items():
            can_ask = thread.get("approval_mode") != "never"
            agents.append({"pane_id": pane, "agent": "codex", "agent_status": self.activity.status(pane, can_ask, self.ran),
                           "cwd": str(thread.get("cwd") or ""), "terminal_title_stripped": str(thread.get("title") or "")})
        self.ran &= set(self.threads)
        return {"result": {"agents": agents}}

    def _open(self, pane: str) -> tuple[bool, str]:
        thread = self.threads.get(pane)
        if not thread:
            return False, "that thread has gone"
        try:
            done = self.run(["open", f"codex://threads/{thread['id']}"], capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return False, "couldn't open the Codex app"
        return (True, "") if done.returncode == 0 else (False, "couldn't open the Codex app")
