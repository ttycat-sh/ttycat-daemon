"""Recognising agents from what any machine can tell you, with no terminal of ours involved.

Used for this Mac and, over SSH, for other machines, so it only works on text and does nothing itself.

Which process is the agent is worked out as herdr does (src/detect/mod.rs in its source, Apache-2.0):
the terminal's foreground job, its leader first, by name, seeing through runtimes such as node. An
agent found this way shows as running and nothing more, unless it's in tmux: tmux can show its
screen, and screen_rules says what that means. Elsewhere only something like herdr can read a screen.
Claude Code is different: it records its own state.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import shlex
from dataclasses import dataclass

SESSION_PREFIX, PROCESS_PREFIX = "s:", "p:"
ID_LEN = 8

# name of the program -> the agent, as herdr knows them
NAMES = {
    "claude": "claude", "claude-code": "claude", "codex": "codex", "opencode": "opencode", "opencode2": "opencode",
    "pi": "pi", "gemini": "gemini", "cursor-agent": "cursor", "copilot": "copilot", "github-copilot": "copilot",
    "droid": "droid", "amp": "amp", "amp-local": "amp", "kiro-cli": "kiro", "kiro": "kiro", "kimi": "kimi",
    "kilo": "kilo", "grok": "grok", "hermes": "hermes", "qwen": "qwen", "qodercli": "qodercli", "letta": "letta",
    "cline": "cline", "devin": "devin", "goose": "goose", "aider": "aider", "crush": "crush", "omp": "omp",
    "mastracode": "mastracode", "maki": "maki", "agy": "agy", "antigravity": "agy", "muse": "muse",
}
# What an npm package's folder says, when the script itself is called cli.js or index.js.
PACKAGES = {"codex": "codex", "claude-code": "claude", "opencode-ai": "opencode", "pi-coding-agent": "pi",
            "gemini-cli": "gemini", "cline": "cline", "qwen-code": "qwen", "letta-code": "letta"}
RUNTIMES = re.compile(r"^(node|nodejs|bun|deno|tsx|python(\d(\.\d+)?)?|sh|bash|zsh|dash)$")
INLINE_CODE = {"-c", "-e", "-p", "--eval", "--print", "-m", "--command"}  # `python -c "codex"` isn't codex
OPTION_WITH_VALUE = {"--require", "-r", "--import", "--loader", "--experimental-loader", "--inspect-port"}
BUILD_SUFFIX = re.compile(r"^(codex|opencode)-(aarch64|arm64|x86_64|x64|darwin|linux)")
PACKAGE_PATH = re.compile(r"node_modules/(?:@[^/]+/)?([^/]+)/")
SCRIPT_EXT = re.compile(r"\.(js|mjs|cjs|ts|py)$")
# For a first pass on another machine, so it only looks closer at what might be an agent.
ROUGH_MATCH = "|".join(sorted(set(NAMES) | set(PACKAGES)))


def _base(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1].lstrip("-")  # "-zsh" is a login shell


def _named(name: str) -> str | None:
    name = name.lower()
    hit = BUILD_SUFFIX.match(name)
    return NAMES.get(hit.group(1)) if hit else NAMES.get(name)


def agent_in_command(command: str) -> str | None:
    """The agent a command line runs, if any. Only ever the program or the script it's running,
    never an argument: a prompt or a file that mentions an agent isn't one."""
    words = command.split()
    if not words:
        return None
    program = _base(words[0])
    if not RUNTIMES.match(program.lower()):
        return _named(program)
    skip = False
    for word in words[1:]:
        if skip:
            skip = False
        elif word in INLINE_CODE:
            return None
        elif word in OPTION_WITH_VALUE:
            skip = True
        elif not word.startswith("-"):
            package = PACKAGE_PATH.findall(word)
            if package and package[-1] in PACKAGES:
                return PACKAGES[package[-1]]
            return _named(SCRIPT_EXT.sub("", _base(word)))
    return None


@dataclass
class Found:
    pid: int
    agent: str
    tty: str
    in_herdr: bool = False
    cwd: str = ""
    tmux_socket: str = ""
    tmux_pane: str = ""
    status: str = "unknown"
    title: str = ""
    screen: str | None = None


def foreground_agents(rows: list[tuple[int, int, int, str, str]]) -> list[Found]:
    """(pid, process group, the terminal's foreground group, tty, command) to one agent per terminal.
    The job's leader decides if it's an agent; failing that, whatever in the job is one."""
    leaders: dict[str, Found] = {}
    others: dict[str, Found] = {}
    for pid, group, foreground, tty, command in rows:
        if group != foreground or not tty or tty.startswith("?"):
            continue  # in the background, or no terminal: not something you're running
        agent = agent_in_command(command)
        if not agent:
            continue
        found = Found(pid, agent, tty)
        if pid == group:
            leaders[tty] = found
        else:
            others.setdefault(tty, found)
    return list({**others, **leaders}.values())


def parse_ps(text: str) -> list[tuple[int, int, int, str, str]]:
    """Lines of `ps -axo pid=,pgid=,tpgid=,tty=,command=`."""
    rows = []
    for line in text.splitlines():
        parts = line.split(None, 4)
        if len(parts) == 5 and parts[0].isdigit() and parts[1].isdigit() and parts[2].lstrip("-").isdigit():
            rows.append((int(parts[0]), int(parts[1]), int(parts[2]), parts[3], parts[4]))
    return rows


def session_entry(entry) -> bool:
    """Claude's record of a session, as far as anything here relies on it."""
    return (isinstance(entry, dict) and isinstance(entry.get("pid"), int) and isinstance(entry.get("sessionId"), str)
            and bool(entry["sessionId"]) and entry.get("kind", "interactive") == "interactive")


def session_pane(session_id: str) -> str:
    return SESSION_PREFIX + session_id[:ID_LEN]


def process_pane(pid: int) -> str:
    return PROCESS_PREFIX + str(pid)


def is_ours(pane: str) -> bool:
    return pane.startswith((SESSION_PREFIX, PROCESS_PREFIX))


class SessionStates:
    """Turns Claude's records into rows of herdr's agent list, remembering what it takes to tell a
    session that's finished something from one that's just sitting there."""

    def __init__(self):
        self.ran: set[str] = set()
        self.waiting: dict[str, str] = {}

    def _status(self, pane: str, value) -> str:
        if value == "busy":
            self.ran.add(pane)
            return "working"
        if value == "waiting":
            return "blocked"
        if value in ("idle", "shell"):
            return "done" if pane in self.ran else "idle"
        return "unknown"

    def agents(self, entries: list[dict]) -> list[dict]:
        out, waiting = [], {}
        for entry in entries:
            pane = session_pane(entry["sessionId"])
            if isinstance(entry.get("waitingFor"), str):
                waiting[pane] = entry["waitingFor"]
            out.append({"pane_id": pane, "agent": "claude", "agent_status": self._status(pane, entry.get("status")),
                        "agent_session": {"value": entry["sessionId"]}, "cwd": str(entry.get("cwd") or "")})
        self.waiting = waiting
        self.ran &= {a["pane_id"] for a in out}
        return out

    def asking(self, pane: str) -> tuple[str, str]:
        """What it's waiting for, with no screen to read: (raw text, as shown)."""
        what = self.waiting.get(pane, "")
        return what, f"Waiting for a {what} in its terminal" if what else "Waiting at its terminal"


def process_agent(found: Found) -> dict:
    return {"pane_id": process_pane(found.pid), "agent": found.agent, "agent_status": found.status, "cwd": found.cwd,
            "terminal_title_stripped": found.title}


SAFE_SOCKET = re.compile(r"^/[A-Za-z0-9_./-]+$")
SAFE_PANE = re.compile(r"^%\d+$")


def tmux_place(found: Found) -> bool:
    """Whether it's somewhere in tmux that can be asked for, safely, in a command."""
    return bool(SAFE_SOCKET.match(found.tmux_socket) and SAFE_PANE.match(found.tmux_pane))


def tmux_socket_of(value: str) -> str:
    """$TMUX is socket,server,session."""
    return value.split(",", 1)[0]


TMUX_LINE = re.compile(r"^@@tmux (/\S+) /dev/(\S+) (%\d+)$", re.M)


def tmux_panes(text: str) -> dict[str, tuple[str, str]]:
    """tty -> (socket, pane), from `tmux list-panes -a` on each server. By terminal, not from a
    process's environment: an agent that sets its own process title, as pi does, hides that."""
    return {tty: (socket, pane) for socket, tty, pane in TMUX_LINE.findall(text)}


def place_in_tmux(found: list[Found], panes: dict[str, tuple[str, str]]):
    for f in found:
        if f.tty in panes:
            f.tmux_socket, f.tmux_pane = panes[f.tty]
            f.in_herdr = False  # in tmux itself: even inside a herdr pane, herdr can't see it


class ScreenStates:
    """What herdr's rules say about each screen, kept steady the way herdr keeps it."""

    def __init__(self, rules):
        self.rules = rules
        self.last: dict[str, str] = {}
        self.ran: set[str] = set()     # seen at work: it can be "done", not just idle
        self.held: set[str] = set()

    def update(self, pane: str, agent: str, screen: str, title: str = "") -> str:
        if not self.rules.knows(agent):
            return "unknown"
        seen = self.rules.detect(agent, screen, title)
        before = self.last.get(pane)
        if seen.skip_state_update:  # a viewer over the agent, say: this says nothing about the agent
            return before or "idle"
        state = seen.state
        if state == "working":
            self.ran.add(pane)
            self.held.discard(pane)
        elif state == "idle":
            # Work can leave the screen for a moment. Unless it plainly says it's ready, look again.
            if before == "working" and not seen.visible_idle and pane not in self.held:
                self.held.add(pane)
                return "working"
            self.held.discard(pane)
            if pane in self.ran:
                state = "done"
        self.last[pane] = state
        return state

    def forget_others(self, alive: set[str]):
        self.last = {p: s for p, s in self.last.items() if p in alive}
        self.ran &= alive
        self.held &= alive


def capture_script(found: list[Found]) -> str:
    """Asks that machine's tmux for these panes' screens, and nothing else. Encoded so that what's
    on a screen can't pass for anything but a screen. Only ever for what's known to be an agent."""
    lines = ['PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"']
    for f in found:
        if not tmux_place(f):
            continue
        sock, pane = shlex.quote(f.tmux_socket), shlex.quote(f.tmux_pane)
        lines.append(f"t=$(tmux -S {sock} display-message -p -t {pane} '#{{pane_title}}' 2>/dev/null | base64 | tr -d '\\n')")
        lines.append(f"s=$(tmux -S {sock} capture-pane -p -t {pane} 2>/dev/null | base64 | tr -d '\\n')")
        lines.append(f'echo "@@pane {f.pid} ${{t:--}} ${{s:--}}"')
    return "\n".join(lines + ["exit 0"]) + "\n"


def parse_captures(text: str) -> dict[int, tuple[str, str]]:
    """pid -> (title, screen)."""
    out = {}
    for line in text.splitlines():
        parts = line.split(" ")
        if len(parts) != 4 or parts[0] != "@@pane" or not parts[1].isdigit():
            continue
        try:
            title, screen = (base64.b64decode(p, validate=True).decode(errors="replace") if p != "-" else "" for p in parts[2:])
        except (ValueError, binascii.Error):
            continue
        out[int(parts[1])] = (title.strip(), screen)
    return out


# ---------------------------------------------------------------- another machine

# A POSIX shell, not Python: a machine only needs Python if you add the hook to it. Everything that
# leaves it is either an agent's own record or a line that looks like an agent.
REPORT_SCRIPT = r'''
PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
# herdr clears TMUX from its panes, so having both means tmux inside a pane: out of herdr's sight.
# An agent that sets its own process title hides its environment, so then it's who its parents are.
in_herdr() {
  case "$(ps eww -o command= -p "$1" 2>/dev/null)" in
    *" TMUX="*) echo 0; return ;;
    *HERDR_PANE_ID=*) echo 1; return ;;
    *=*) echo 0; return ;;
  esac
  p=$1; n=0
  while [ "$n" -lt 10 ] && [ "$p" -gt 1 ] 2>/dev/null; do
    l=$(ps -o ppid=,comm= -p "$p" 2>/dev/null); [ -n "$l" ] || break
    case "$l" in *tmux*) echo 0; return ;; *herdr*) echo 1; return ;; esac
    p=$(echo $l | cut -d' ' -f1); n=$((n+1))
  done
  echo 0
}
for s in "${TMUX_TMPDIR:-/tmp}/tmux-$(id -u)"/*; do
  [ -S "$s" ] && tmux -S "$s" list-panes -a -F "@@tmux $s #{pane_tty} #{pane_id}" 2>/dev/null
done
for f in "$HOME"/.claude/sessions/*.json; do
  [ -f "$f" ] || continue
  pid=${f##*/}; pid=${pid%.json}
  kill -0 "$pid" 2>/dev/null || continue
  echo "@@session $(in_herdr "$pid")"
  cat "$f"; echo
done
ps -axo pid=,pgid=,tpgid=,tty=,command= 2>/dev/null | awk '$2==$3 && $4 !~ /^\?/' | grep -E '{NAMES}' | while read -r pid rest; do
  cwd=$(lsof -a -d cwd -p "$pid" -Fn 2>/dev/null | sed -n 's/^n//p' | head -1)
  echo "@@process $(in_herdr "$pid") $cwd"
  echo "$pid $rest"
done
exit 0
'''.replace("{NAMES}", ROUGH_MATCH)


def parse_report(text: str) -> tuple[list[dict], list[Found]]:
    """What REPORT_SCRIPT printed, or something else entirely if that machine is odd: whatever
    doesn't parse is left out. Sessions come with `in_herdr` set."""
    entries: list[dict] = []
    herdr_pids: set[int] = set()
    cwds: dict[int, str] = {}
    rows = []
    panes = tmux_panes(text)
    text = TMUX_LINE.sub("", text)
    for block in re.split(r"^(?=@@(?:session|process) )", text, flags=re.M):
        head, _, body = block.partition("\n")
        if head.startswith("@@session "):
            try:
                entry = json.loads(body)
            except ValueError:
                continue
            if session_entry(entry):
                entry = dict(entry)
                entry["in_herdr"] = head.split()[1:2] != ["0"]
                entries.append(entry)
        elif head.startswith("@@process "):
            parsed = parse_ps(body)
            if not parsed:
                continue
            pid = parsed[0][0]
            parts = head.split(" ", 2)
            if len(parts) > 1 and parts[1] != "0":
                herdr_pids.add(pid)
            cwds[pid] = parts[2].strip() if len(parts) > 2 else ""
            rows.append(parsed[0])
    found = foreground_agents(rows)
    for item in found:
        item.in_herdr = item.pid in herdr_pids
        item.cwd = cwds.get(item.pid, "")
    place_in_tmux(found, panes)
    return entries, found


def merge_agents(herdr: list[dict], entries: list[dict], found: list[Found], states: SessionStates) -> list[dict]:
    """One list: herdr's own first, then what it doesn't have. Nothing appears twice."""
    known = {(a.get("agent_session") or {}).get("value", "") for a in herdr} - {""}
    sessions = [e for e in entries if e["sessionId"] not in known and not e.get("in_herdr")]
    claimed = {e["pid"] for e in entries}
    processes = [f for f in found if f.pid not in claimed and not f.in_herdr]
    return [*herdr, *states.agents(sessions), *map(process_agent, processes)]
