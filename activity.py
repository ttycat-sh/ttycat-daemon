"""What an agent is really doing, read from its session log. Claude Code only, so far.

Read-only: nothing here can affect an agent, and no setting has to change on either
machine. Claude's docs say the log's format is internal and can change with any release,
so this only looks at a few fields and skips whatever it doesn't recognise. If a release
breaks it, the worst outcome is an empty list, and the callers then show status changes.
"""
from __future__ import annotations

import collections
import json
import re
import shlex
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

PROJECTS = Path.home() / ".claude" / "projects"
SESSION_ID = re.compile(r"^[0-9a-fA-F-]{36}$")  # it goes into a path, and into a remote shell command
KEEP = 30                 # events kept per agent
READ_BACK = 8_000_000     # how far back to look at first; logs reach hundreds of MB
LONGEST_PATH = 50         # rows on the board are short
LONGEST_LINE = 200_000    # longer ones are big file reads and images, with nothing to show
FIRST_READS_PER_SYNC = 3  # a long first read must not hold up the board for all agents at once
LOOK_AGAIN_S = 5.0        # a new session has no log until its first prompt
USEFUL = (b'"type":"assistant"', b'"type":"user"')

COMMAND_NAME = re.compile(r"<command-name>\s*(\S+?)\s*</command-name>")
NOT_A_FAILURE = ("denied", "doesn't want to proceed", "rejected")  # someone said no: that's shown separately
QUIET_TOOLS = {"ToolSearch", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "TaskOutput", "TaskStop"}


def _path(value, cwd: str) -> str:
    path = value if isinstance(value, str) else ""
    home = str(Path.home())
    if cwd and path.startswith(cwd.rstrip("/") + "/"):
        path = path[len(cwd.rstrip("/")) + 1:]
    elif path.startswith(home + "/"):
        path = "~" + path[len(home):]
    return path if len(path) <= LONGEST_PATH else "..." + path[-(LONGEST_PATH - 3):]  # the file name is what matters


def _one_line(value) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def tool_summary(name: str, args: dict, cwd: str = "") -> str | None:
    """One line about a tool call. None: there's nothing worth showing."""
    if name in QUIET_TOOLS:
        return None
    if name == "Bash":
        return _one_line(args.get("description")) or f"$ {_one_line(args.get('command'))}"
    if name in ("Read", "Edit", "Write"):
        return f"{name} {_path(args.get('file_path'), cwd)}".strip()
    if name == "NotebookEdit":
        return f"Edit {_path(args.get('notebook_path'), cwd)}".strip()
    if name == "Grep":
        return f'Search for "{_one_line(args.get("pattern"))}"'
    if name == "Glob":
        return f"Find {_one_line(args.get('pattern'))}"
    if name == "WebFetch":
        url = _one_line(args.get("url"))
        try:
            parts = urlsplit(url)
            url = parts.netloc + parts.path if parts.netloc else url
        except ValueError:
            pass
        return f"Fetch {url}"
    if name == "WebSearch":
        return f'Search the web for "{_one_line(args.get("query"))}"'
    if name in ("Agent", "Task"):
        return f"Subagent: {_one_line(args.get('description')) or _one_line(args.get('subagent_type'))}"
    if name == "TodoWrite":
        todos = args.get("todos")
        active = [t for t in todos if isinstance(t, dict) and t.get("status") == "in_progress"] if isinstance(todos, list) else []
        return _one_line(active[0].get("activeForm") or active[0].get("content")) or None if active else None
    if name == "Skill":
        return f"Skill {_one_line(args.get('skill'))}"
    if name == "AskUserQuestion":
        questions = args.get("questions")
        first = questions[0] if isinstance(questions, list) and questions and isinstance(questions[0], dict) else {}
        return f"Asked: {_one_line(first.get('question'))}"
    if name == "ExitPlanMode":
        return "Has a plan for you"
    if name.startswith("mcp__"):
        _, _, rest = name.partition("__")
        server, _, tool = rest.partition("__")
        return f"{server}: {tool}"
    return name


def said_summary(text: str) -> str:
    """The first real line of what the agent said, without its markdown."""
    for line in text.splitlines():
        line = line.strip().lstrip("#>*- ").replace("**", "").replace("`", "")
        if line:
            return _one_line(line)
    return ""


def prompt_summary(text: str) -> str | None:
    """What you typed. Claude also files its own wrappers under "user": those aren't you."""
    text = text.strip()
    command = COMMAND_NAME.search(text)
    if command:
        return f"You: {command.group(1)}"
    if not text or text.startswith(("<", "[", "Caveat:")):
        return None
    return f"You: {_one_line(text)}"


def _when(entry: dict) -> float:
    try:
        return datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00")).timestamp()
    except (KeyError, AttributeError, TypeError, ValueError):
        return time.time()


@dataclass
class Event:
    at: float
    tone: int        # 0 plain, 1 orange (it went wrong), 2 green: the board's dots
    text: str
    kind: str        # tool | said | you
    tool_id: str = ""
    count: int = 1

    @property
    def shown(self) -> str:
        return f"{self.text} x{self.count}" if self.count > 1 else self.text


class Trail:
    """The events of one session, newest last."""

    def __init__(self, session: str):
        self.session = session
        self.offset: int | None = None  # how far the log has been read
        self.events: collections.deque[Event] = collections.deque(maxlen=KEEP)

    def feed(self, data: bytes):
        for raw in data.split(b"\n"):
            if not raw or len(raw) > LONGEST_LINE or not any(mark in raw for mark in USEFUL):
                continue
            try:
                self._entry(json.loads(raw))
            except (ValueError, TypeError, AttributeError, KeyError, IndexError):
                continue  # half a line, or a format this doesn't know: skipped, never fatal

    def _entry(self, entry: dict):
        if entry.get("isSidechain") or entry.get("isMeta") or entry.get("isCompactSummary") or entry.get("isVisibleInTranscriptOnly"):
            return
        content = (entry.get("message") or {}).get("content")
        at = _when(entry)
        if entry.get("type") == "assistant" and isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_use":
                    text = tool_summary(str(block.get("name") or ""), block.get("input") or {}, str(entry.get("cwd") or ""))
                    if text:
                        self._add(Event(at, 0, text, "tool", str(block.get("id") or "")))
                elif block.get("type") == "text":
                    text = said_summary(str(block.get("text") or ""))
                    if text:
                        self._add(Event(at, 0, text, "said"))
        elif entry.get("type") == "user":
            if isinstance(content, str):
                self._prompt(content, at)
                return
            for block in content if isinstance(content, list) else ():
                if block.get("type") == "text":
                    self._prompt(str(block.get("text") or ""), at)
                elif block.get("type") == "tool_result" and block.get("is_error"):
                    self._failed(block)

    def _prompt(self, text: str, at: float):
        shown = prompt_summary(text)
        if shown:
            self._add(Event(at, 0, shown, "you"))

    def _failed(self, block: dict):
        why = block.get("content")
        why = why if isinstance(why, str) else json.dumps(why)
        if any(word in why[:300] for word in NOT_A_FAILURE):
            return
        for event in reversed(self.events):
            if event.tool_id and event.tool_id == block.get("tool_use_id"):
                event.tone = 1
                event.text = f"Failed: {event.text}"
                return

    def _add(self, event: Event):
        last = self.events[-1] if self.events else None
        if last and last.kind == event.kind and last.text == event.text:
            last.at, last.tool_id = event.at, event.tool_id
            last.count += 1
        else:
            self.events.append(event)


class LocalLogs:
    def __init__(self, root: Path = PROJECTS, clock=time.monotonic):
        self.root, self.clock = root, clock
        self.paths: dict[str, Path] = {}
        self.looked: dict[str, float] = {}

    def _find(self, session: str) -> Path | None:
        path = self.paths.get(session)
        if path and path.exists():
            return path
        if self.clock() - self.looked.get(session, -LOOK_AGAIN_S) < LOOK_AGAIN_S:
            return None
        self.looked[session] = self.clock()
        found = sorted(self.root.glob(f"*/{session}.jsonl"), key=lambda p: p.stat().st_mtime)
        if found:
            self.paths[session] = found[-1]
            return found[-1]
        return None

    def read(self, wanted: dict[str, int | None]) -> dict[str, tuple[int, bytes, bool]]:
        """session -> (new offset, whole new lines, whether the log started over)."""
        out = {}
        for session, offset in wanted.items():
            path = self._find(session)
            if not path:
                continue
            try:
                out[session] = self._read(path, offset)
            except OSError:
                self.paths.pop(session, None)
        return out

    def _read(self, path: Path, offset: int | None) -> tuple[int, bytes, bool]:
        size = path.stat().st_size
        if offset == size:
            return size, b"", False
        restart = offset is not None and offset > size
        jumped = offset is None or restart or size - offset > READ_BACK
        start = max(0, size - READ_BACK) if jumped else offset
        with path.open("rb") as f:
            f.seek(start)
            data = f.read(size - start)
        cut = data.rfind(b"\n") + 1  # a line still being written is read next time
        return start + cut, data[:cut], restart


class RemoteLogs:
    """The same over SSH: one call per poll, and only new lines of the useful kind travel."""

    def __init__(self, run):
        self.run = run  # takes a command for `sh`, returns its output as bytes or None

    @staticmethod
    def script(wanted: dict[str, int | None]) -> str:
        pairs = " ".join(f"{session}:{-1 if offset is None else offset}" for session, offset in wanted.items())
        return f"""cd "$HOME/.claude/projects" 2>/dev/null || exit 0
for pair in {pairs}; do
  id=${{pair%:*}}; off=${{pair#*:}}; file=
  for f in ./*/"$id".jsonl; do [ -f "$f" ] && file=$f; done
  [ -n "$file" ] || continue
  size=$(wc -c <"$file" | tr -d ' ')
  echo "@@ $id $size"
  [ "$off" = "$size" ] && continue
  if [ "$off" -lt 0 ] || [ "$off" -gt "$size" ] || [ $((size - off)) -gt {READ_BACK} ]; then
    off=$((size > {READ_BACK} ? size - {READ_BACK} : 0))
  fi
  tail -c +$((off + 1)) "$file" | head -c $((size - off)) | LC_ALL=C grep -a -E '"type":"(assistant|user)"' | LC_ALL=C awk 'length($0) < {LONGEST_LINE}'
done"""

    def read(self, wanted: dict[str, int | None]) -> dict[str, tuple[int, bytes, bool]]:
        if not wanted:
            return {}
        output = self.run(self.script(wanted))
        if output is None:
            return {}
        out: dict[str, tuple[int, bytes, bool]] = {}
        session, size, lines = None, 0, []

        def finish():
            if session is not None:
                offset = wanted.get(session)
                out[session] = (size, b"\n".join(lines), offset is not None and offset > size)

        for line in output.split(b"\n"):
            if line.startswith(b"@@ "):
                finish()
                parts = line.split()
                session, size, lines = parts[1].decode(errors="replace"), int(parts[2]), []
            elif session is not None:
                lines.append(line)
        finish()
        return out


class Activity:
    def __init__(self, logs):
        self.logs = logs
        self.trails: dict[str, Trail] = {}

    def sync(self, sessions: dict[str, str], wanted) -> None:
        """`sessions`: pane -> session id. Only panes in `wanted` are read."""
        for pane in set(self.trails) - set(sessions):
            del self.trails[pane]
        asks: dict[str, int | None] = {}
        by_session: dict[str, Trail] = {}
        first_reads = 0
        for pane, session in sessions.items():
            if not SESSION_ID.match(session or ""):
                self.trails.pop(pane, None)
                continue
            trail = self.trails.get(pane)
            if trail is None or trail.session != session:  # /clear starts a new session in the same pane
                trail = self.trails[pane] = Trail(session)
            if pane not in wanted:
                continue
            if trail.offset is None:
                first_reads += 1
                if first_reads > FIRST_READS_PER_SYNC:
                    continue
            asks[session] = trail.offset
            by_session[session] = trail
        try:
            found = self.logs.read(asks)
        except (OSError, ValueError, IndexError):
            return
        for session, (offset, data, restarted) in found.items():
            trail = by_session.get(session)
            if trail is None:
                continue
            if restarted:
                trail.events.clear()
            trail.offset = offset
            trail.feed(data)

    def events(self, pane: str) -> list[Event]:
        trail = self.trails.get(pane)
        return list(trail.events) if trail else []

    def now(self, pane: str) -> str:
        """The newest thing it did."""
        events = self.events(pane)
        return events[-1].shown if events else ""

    def last_said(self, pane: str) -> str:
        """What it said at the end of this turn. An earlier turn's words would be about something else."""
        for event in reversed(self.events(pane)):
            if event.kind == "said":
                return event.text
            if event.kind == "you":
                return ""
        return ""


def shell_command(script: str) -> str:
    return "sh -c " + shlex.quote(script)
