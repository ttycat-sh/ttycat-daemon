"""Claude Code sessions in any terminal, without herdr.

herdr works out an agent's state by finding its process and reading what it draws: it can, because
it is the terminal (src/detect in its source). Outside herdr nothing can read another terminal's
screen, but Claude Code keeps its own record of every running session, with its state, in
~/.claude/sessions/<pid>.json. That file isn't a documented interface, so everything here reads it
loosely: anything missing or unfamiliar means fewer rows, never a wrong answer. Answering still goes
through the permission hook, as in herdr, and if this stops working the terminal prompt stands.

A session inside herdr is herdr's to show: it never appears twice.
"""
from __future__ import annotations

import collections
import json
import os
import re
import subprocess
import time
from pathlib import Path

import prompts
from discovery import (SESSION_PREFIX, Found, ScreenStates, SessionStates, foreground_agents, parse_ps, place_in_tmux,
                       process_agent, process_pane, session_entry, session_pane, tmux_panes, tmux_place, tmux_socket_of)
from codex_threads import CLI_SOURCE, CODEX_HOME, RECENT_S, Logs, read_threads
from screen_rules import shared_rules
from sources import APP_IN_PATH, HerdrSource, app_among_parents  # noqa: F401

REGISTRY = Path.home() / ".claude" / "sessions"
# TERM_PROGRAM, for when no app can be found among a session's parents (under tmux, say).
TERMINALS = {"ghostty": "Ghostty", "Apple_Terminal": "Terminal", "iTerm.app": "iTerm", "vscode": "Visual Studio Code",
             "WezTerm": "WezTerm", "kitty": "kitty", "alacritty": "Alacritty", "Hyper": "Hyper", "WarpTerminal": "Warp"}
ENV_WANTED = ("HERDR_PANE_ID", "TERM_PROGRAM", "TMUX", "TMUX_PANE")


pane_of = session_pane  # hook.py names a session the same way

SCAN_EVERY_S = 3.0


def is_session_pane(pane: str) -> bool:
    return pane.startswith(SESSION_PREFIX)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def read_registry(folder: Path = REGISTRY, is_alive=alive) -> list[dict]:
    """The running sessions. A file that's being rewritten, or isn't what's expected, is skipped."""
    out = []
    try:
        files = list(folder.glob("*.json"))
    except OSError:
        return out
    for path in files:
        try:
            entry = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if session_entry(entry) and is_alive(entry["pid"]):  # a crash leaves its file behind
            out.append(entry)
    return out


def process_env(pid: int) -> dict[str, str]:
    """The few variables that say where it's running. Readable only for your own processes."""
    try:
        out = subprocess.run(["ps", "eww", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    found = {}
    for name in ENV_WANTED:
        hit = re.search(rf"(?:^|\s){name}=(\S+)", out.stdout)
        if hit:
            found[name] = hit.group(1)
    return found


def under_herdr(pid: int) -> bool:
    """For a process whose environment can't be seen, as one that sets its own title. Stops at tmux:
    inside tmux, herdr can't see it, even if tmux is in a herdr pane."""
    for _ in range(10):
        try:
            out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True, text=True, timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            return False
        parts = out.stdout.strip().split(None, 1)
        if len(parts) != 2:
            return False
        name = parts[1].rsplit("/", 1)[-1].lstrip("-")
        if name.startswith("tmux"):
            return False
        if name.startswith("herdr"):
            return True
        pid = int(parts[0])
        if pid <= 1:
            return False
    return False


def scan_processes() -> list[tuple[int, int, int, str, str]]:
    try:
        out = subprocess.run(["ps", "-axo", "pid=,pgid=,tpgid=,tty=,command="], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return parse_ps(out.stdout)


def process_cwd(pid: int) -> str:
    try:
        out = subprocess.run(["lsof", "-a", "-d", "cwd", "-p", str(pid), "-Fn"], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return next((line[1:] for line in out.stdout.splitlines() if line.startswith("n")), "")


class _InATerminal(HerdrSource):
    """What's the same for anything found in a terminal that isn't ours."""

    def __init__(self, focus_app="Ghostty", hub=None, pet=None, env_of=process_env, app_of=app_among_parents,
                 run=subprocess.run, parent_check=under_herdr):
        super().__init__(focus_app=focus_app, hub=hub, pet=pet)
        self.env_of, self.app_of, self.run, self.parent_check = env_of, app_of, run, parent_check
        self.envs: dict[int, dict[str, str]] = {}
        self.parents: dict[int, bool] = {}
        self.pids: dict[str, int] = {}
        self.herdr_up = False

    def _env(self, pid: int) -> dict[str, str]:
        env = self.envs.get(pid)
        if env is None:
            env = self.envs[pid] = self.env_of(pid)  # it doesn't change: asked once per process
        return env

    def _in_herdr_pane(self, pid: int) -> bool:
        # herdr clears TMUX from its panes, so having both means tmux inside a pane, out of herdr's sight.
        if not self.herdr_up:
            return False
        env = self._env(pid)
        if env:
            return "HERDR_PANE_ID" in env and "TMUX" not in env
        if pid not in self.parents:  # nothing to go on: it may have hidden its environment
            self.parents[pid] = self.parent_check(pid)
        return self.parents[pid]

    def _forget_others(self, running: set[int]):
        self.envs = {pid: env for pid, env in self.envs.items() if pid in running}
        self.parents = {pid: seen for pid, seen in self.parents.items() if pid in running}

    def _tmux_of(self, pid: int) -> tuple[str, str]:
        """(socket, pane), blank if not in tmux or not known."""
        env = self._env(pid)
        return tmux_socket_of(env.get("TMUX", "")), env.get("TMUX_PANE", "")

    def _open(self, pane: str) -> tuple[bool, str]:
        pid = self.pids.get(pane)
        if pid is None:
            return False, "that session has ended"
        env = self._env(pid)
        try:
            socket, tmux = self._tmux_of(pid)
            if tmux:
                base = ["tmux", "-S", socket] if socket else ["tmux"]
                self.run([*base, "select-window", "-t", tmux], capture_output=True, timeout=3)
                self.run([*base, "select-pane", "-t", tmux], capture_output=True, timeout=3)
            app = self.app_of(pid) or TERMINALS.get(env.get("TERM_PROGRAM", ""))
            if not app:
                return False, "couldn't tell which terminal it's in"
            done = self.run(["open", "-a", app], capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return False, "couldn't open its terminal"
        return (True, "") if done.returncode == 0 else (False, "couldn't open its terminal")


class ClaudeSessions(_InATerminal):
    """Presents the registry as herdr's agent list would, so everything built on that list works
    as it does for herdr: rows, what it's doing, the pet, notifications and answers."""

    def __init__(self, focus_app="Ghostty", hub=None, pet=None, folder: Path = REGISTRY, read=read_registry, **kw):
        super().__init__(focus_app=focus_app, hub=hub, pet=pet, **kw)
        self.folder, self.read = folder, read
        self.claude = SessionStates()
        self.herdr_sessions: set[str] = set()
        self.all_pids: set[int] = set()  # every Claude running, whoever shows it: none is a plain process

    def _herdr(self, *args) -> dict | None:
        if args[:2] != ("agent", "list"):
            return None
        everything = self.read(self.folder)
        self.all_pids = {e["pid"] for e in everything}
        self._forget_others(self.all_pids)
        mine = [e for e in everything if e["sessionId"] not in self.herdr_sessions and not self._in_herdr_pane(e["pid"])]
        self.pids = {session_pane(e["sessionId"]): e["pid"] for e in mine}
        return {"result": {"agents": self.claude.agents(mine)}}

    def _read_prompt(self, pane: str, agent: str) -> tuple[str, prompts.Ask]:
        text, body = self.claude.asking(pane)
        return text, prompts.Ask(body=body, detail=(text or "waiting")[:70])


def list_tmux_panes() -> str:
    """Every tmux server of yours, as lines discovery.tmux_panes reads."""
    folder = Path(os.environ.get("TMUX_TMPDIR") or "/tmp") / f"tmux-{os.getuid()}"
    out = []
    try:
        sockets = list(folder.iterdir())
    except OSError:
        return ""
    for socket in sockets:
        try:
            done = subprocess.run(["tmux", "-S", str(socket), "list-panes", "-a", "-F", f"@@tmux {socket} #{{pane_tty}} #{{pane_id}}"],
                                  capture_output=True, text=True, timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if done.returncode == 0:
            out.append(done.stdout)
    return "".join(out)


def capture_pane(socket: str, pane: str) -> tuple[str, str] | None:
    """(title, what's on screen), from tmux."""
    try:
        title = subprocess.run(["tmux", "-S", socket, "display-message", "-p", "-t", pane, "#{pane_title}"],
                               capture_output=True, text=True, timeout=2)
        screen = subprocess.run(["tmux", "-S", socket, "capture-pane", "-p", "-t", pane],
                                capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return (title.stdout.strip(), screen.stdout) if screen.returncode == 0 else None


def started_at(pid: int) -> float | None:
    """When a process began, by how long ps says it's been running."""
    try:
        out = subprocess.run(["ps", "-o", "etime=", "-p", str(pid)], capture_output=True, text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None
    hit = re.fullmatch(r"(?:(?:(\d+)-)?(\d+):)?(\d+):(\d+)", out)
    if not hit:
        return None
    days, hours, minutes, seconds = (int(g or 0) for g in hit.groups())
    return time.time() - (days * 86400 + hours * 3600 + minutes * 60 + seconds)


class RunningAgents(_InATerminal):
    """Any other agent running in a terminal. In tmux, its screen says what it's doing, by herdr's
    rules. Codex keeps a log, which says most of that anywhere. Otherwise it's known to be there and
    no more, and says so. Nothing it asks can be answered from here: no keys are ever sent."""

    def __init__(self, focus_app="Ghostty", pet=None, scan=scan_processes, cwd_of=process_cwd, clock=time.monotonic,
                 capture=capture_pane, rules=None, panes=list_tmux_panes, codex_home=CODEX_HOME, codex_threads=read_threads,
                 begun=started_at, **kw):
        super().__init__(focus_app=focus_app, hub=None, pet=pet, **kw)
        self.scan, self.cwd_of, self.clock, self.capture = scan, cwd_of, clock, capture
        self.screen_states = ScreenStates(rules or shared_rules())
        self.list_panes = panes
        self.codex_home, self.codex_threads, self.begun = codex_home, codex_threads, begun
        self.activity = Logs()     # Codex's: nothing else found here has a log to read
        self.codex_list: list[dict] = []
        self.codex_paths: dict[str, str] = {}
        self.codex_ran: set[str] = set()
        self.started: dict[int, float | None] = {}
        self.screens: dict[str, str] = {}
        self.claude_pids: set[int] = set()
        self.cwds: dict[int, str] = {}
        self.found: list[Found] = []
        self.scanned_at = -SCAN_EVERY_S

    def _herdr(self, *args) -> dict | None:
        if args[:2] != ("agent", "list"):
            return None
        if self.clock() - self.scanned_at >= SCAN_EVERY_S:  # a look at every process: not twice a second
            self.scanned_at = self.clock()
            self.found = foreground_agents(self.scan())
            place_in_tmux(self.found, tmux_panes(self.list_panes()) if self.found else {})
            running = {f.pid for f in self.found}
            self.started = {pid: when for pid, when in self.started.items() if pid in running}
            self.codex_list = (self.codex_threads(self.codex_home, (CLI_SOURCE,), time.time() - RECENT_S * 48)
                               if any(f.agent == "codex" and not f.tmux_pane for f in self.found) else [])
            self._forget_others(running)
            self.cwds = {pid: cwd for pid, cwd in self.cwds.items() if pid in running}
        mine = [f for f in self.found if f.pid not in self.claude_pids and (f.tmux_pane or not self._in_herdr_pane(f.pid))]
        for f in mine:
            if f.pid not in self.cwds:
                self.cwds[f.pid] = self.cwd_of(f.pid)
            f.cwd = self.cwds[f.pid]
            self._read_screen(f)
        self._read_codex_logs(mine)
        self.pids = {process_pane(f.pid): f.pid for f in mine}
        self.screen_states.forget_others(set(self.pids))
        self.screens = {pane: text for pane, text in self.screens.items() if pane in self.pids}
        return {"result": {"agents": [process_agent(f) for f in mine]}}

    def _read_screen(self, f: Found):
        """Every look, not just every scan: this is where it changes."""
        pane = process_pane(f.pid)
        self.screens.pop(pane, None)
        f.status, f.title, f.screen = "unknown", "", None
        if not tmux_place(f) or not self.screen_states.rules.knows(f.agent):
            return
        got = self.capture(f.tmux_socket, f.tmux_pane)
        if got:
            f.title, f.screen = got
            f.status = self.screen_states.update(pane, f.agent, f.screen, f.title)
            self.screens[pane] = f.screen

    def _follow(self, agents: list[dict], now: float):
        pass  # no Claude here, and Codex's logs are read with the state they give

    def _read_codex_logs(self, mine: list[Found]):
        """A Codex outside tmux: its thread is the newest in its folder since it started. If two share
        a folder there's no telling them apart, so neither gets a state it may not have."""
        plain = [f for f in mine if f.agent == "codex" and f.screen is None]
        per_folder = collections.Counter(f.cwd for f in plain)
        paths, asks = {}, {}
        for f in plain:
            if per_folder[f.cwd] != 1 or not f.cwd:
                continue
            if f.pid not in self.started:
                self.started[f.pid] = self.begun(f.pid)
            began = self.started[f.pid]
            if began is None:
                continue
            thread = next((t for t in self.codex_list if t.get("cwd") == f.cwd and t.get("updated_at", 0) >= began - 5), None)
            if thread:
                paths[process_pane(f.pid)] = thread["rollout_path"]
                asks[process_pane(f.pid)] = thread.get("approval_mode") != "never"
        self.activity.sync(paths)
        self.codex_ran &= set(paths)
        for f in plain:
            pane = process_pane(f.pid)
            if pane in paths:
                f.status = self.activity.status(pane, asks[pane], self.codex_ran)

    def _tmux_of(self, pid: int) -> tuple[str, str]:
        found = next((f for f in self.found if f.pid == pid), None)
        return (found.tmux_socket, found.tmux_pane) if found and found.tmux_pane else super()._tmux_of(pid)

    def _read_prompt(self, pane: str, agent: str) -> tuple[str, prompts.Ask]:
        text = self.screens.get(pane, "")
        ask = prompts.parse(agent, text)
        ask.note = "Answer this one in its terminal"
        return text, ask
