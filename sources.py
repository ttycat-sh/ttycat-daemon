"""Where the board's sessions come from: herdr, or the design's sample data."""
from __future__ import annotations

import collections
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import threading
import time
from pathlib import Path

import children
import harness
import prompts
from discovery import is_ours
from activity import Activity, LocalLogs, LONGEST_FOLDER
from agent_names import AgentNames, clean_name, for_board
from approvals import fit
from petstate import Pet

AGENTS = {
    "claude": ("CC", "Claude"),
    "codex": ("CX", "Codex"),
    "opencode": ("OC", "OpenCode"),
    "pi": ("PI", "Pi"),
}
ANSWER_SETTLES_S = 3.0   # herdr can still say "blocked" for a moment after an answer
BOARD_ROWS = 5           # the Recent rows on the board
WHERE = {"board": "on board", "app": "in the app"}
MAX_SAID = 200
MAX_WORDS = 300  # what an agent is doing, in full, for the app: the board's line is 70
SESSION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")  # an ID, as agents make them: nothing else goes to the app as one
RANK = {"blocked": 0, "error": 1, "done": 2, "working": 3, "idle": 4}
MAX_BRANCH = 80
DETACHED = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")  # a commit, where a checkout is on no branch


def clean_branch(name) -> str | None:
    """A branch's name as the app is given it: git never takes a space or a control character in one, so a
    name with either isn't one, and goes nowhere."""
    if not isinstance(name, str):
        return None
    name = name.strip()
    if not name or len(name) > MAX_BRANCH or any(c.isspace() or not c.isprintable() for c in name):
        return None
    return name


# Folders macOS asks you about before an app may read in them, and other disks, which can be slow or gone: a
# branch isn't worth a prompt or a wait. An agent working there has its branch from its log instead.
ASKED_ABOUT = ("Desktop", "Documents", "Downloads", "Library/Mobile Documents", "Library/CloudStorage",
               "Pictures", "Movies", "Music")


def may_look_in(folder: str, home: str | None = None) -> bool:
    """Whether a folder's git may be read for its branch: told from its name alone, touching nothing."""
    path = os.path.normpath(folder)
    home = os.path.normpath(home or str(Path.home()))
    if not os.path.isabs(path) or path == "/Volumes" or path.startswith("/Volumes/"):
        return False
    return not any(path == f"{home}/{place}" or path.startswith(f"{home}/{place}/") for place in ASKED_ABOUT)


def _small_file(path: Path, most: int = 256) -> str | None:
    """A file's first bytes, if it's an ordinary file: never a pipe, a device, or anything else that could keep a
    read waiting, or never end. None: it isn't one, or it can't be read."""
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return None
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        return os.read(fd, most).decode(errors="replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def git_branch(folder: str) -> str | None:
    """The branch a folder's checkout is on, from git's own HEAD: the folder's, or the nearest above it that's
    a checkout, as git finds it. On no branch, the commit, cut short as git shows one. None: it isn't in a
    checkout that can be read, or what's there isn't what git writes. Reads a small file or two; runs nothing.
    It's asked once a second for each agent, on the loop the board and every answer go through: nothing it
    reads may make it wait."""
    start = Path(folder)
    for place in (start, *start.parents):
        dot = place / ".git"
        try:
            found = os.stat(dot)  # never opens it: a pipe here can't make it wait
        except OSError:
            continue
        if stat.S_ISDIR(found.st_mode):
            git = dot
        else:  # a worktree or a submodule: a file that says where its git is
            said = (_small_file(dot) or "").strip()
            if not said.startswith("gitdir:"):
                return None
            git = place / said[len("gitdir:"):].strip()
        head = (_small_file(git / "HEAD") or "").strip()
        if head.startswith("ref: refs/heads/"):
            name = head[len("ref: refs/heads/"):]
            return None if name == ".invalid" else clean_branch(name)  # .invalid: a reftable repo's, which keeps it elsewhere
        return head[:7] if DETACHED.fullmatch(head) else None
    return None


def row_order(row: dict):
    """What needs you first; among equals, the one that changed most recently."""
    return RANK.get(row.get("s"), 9), row.get("age", 0)


APP_IN_PATH = re.compile(r"^(/.*?\.app)/Contents/MacOS/")
TERMINAL_LOOK_S = 30.0
FALLBACK_TERMINAL = "Terminal"


def app_among_parents(pid: int) -> str | None:
    """The .app a process's terminal is, from its parent processes."""
    for _ in range(12):
        try:
            out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True, text=True, timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            return None
        parts = out.stdout.strip().split(None, 1)
        if len(parts) != 2:
            return None
        hit = APP_IN_PATH.match(parts[1])
        if hit:
            return hit.group(1)
        pid = int(parts[0])
        if pid <= 1:
            return None
    return None


def herdr_terminal() -> str | None:
    """The app herdr's window is in, as macOS names it. Its server has no terminal; its client does."""
    try:
        out = subprocess.run(["ps", "-axo", "pid=,tty=,comm="], capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[2].rsplit("/", 1)[-1] == "herdr" and not parts[1].startswith("?"):
            app = app_among_parents(int(parts[0]))
            if app:
                return app.rsplit("/", 1)[-1][: -len(".app")]
    return None


class DemoSource:
    """The sessions from the design's Main and Approval boards."""

    def __init__(self):
        self.start = time.time()
        self.all = [
            dict(id="d1", a="CC", n="Claude", p="checkout-api", s="blocked", age=12, tool="BASH",
                 d="npm run test:integration", verb="wants to run", ask="approve", rev="demo1",
                 body="npm run test:integration -- --grep payment && docker compose down"),
            dict(id="d2", a="OC", n="OpenCode", p="infra", s="blocked", age=40, tool="EDIT",
                 d="gateway.tf +12 -3", verb="wants to change", ask="approve", rev="demo2",
                 file="gateway.tf", add=12, **{"del": 3},
                 body="- timeout = 30\n+ timeout = 90\n+ retry { max = 3 }"),
            dict(id="d7", a="CX", n="Codex", p="api-gateway", s="error", age=180, d="pnpm build: exit 1"),
            dict(id="d3", a="CX", n="Codex", p="design-tokens", s="done", age=60, d="Migrated 214 tokens"),
            dict(id="d4", a="CC", n="Claude", p="web-shop", s="working", age=8, d="Edit: Cart.tsx"),
            dict(id="d5", a="PI", n="Pi", p="riff-9", s="working", age=120, d="write: sequencer.c"),
            dict(id="d6", a="CC", n="Claude", p="docs", s="idle", age=840, d="waiting for prompt"),
        ]
        self.sessions = self.all
        self.given: dict[str, str] = {}  # the names given here, in memory: demo mode keeps nothing
        # its own pet, kept in memory: demo mode doesn't touch the real one
        self.pet_stats = Pet(path=None, clock=time.time, local=lambda t: time.struct_time((2026, 1, 2, 12, 0, 0, 4, 2, 0)))
        self.pet_stats.total, self.pet_stats.today, self.pet_stats.born = 14, 5, time.time() - 3 * 86400
        self.pet_stats.grown = lambda: 14  # its clock is pinned to one day, so it couldn't grow on its own

    def scene(self, name):
        """Sample states for the design's other boards."""
        without = lambda *ids: [s for s in self.all if s["id"] not in ids]
        if name == "main":
            self.sessions = self.all
        elif name == "no-infra":  # then "main" again: an ask arrives
            self.sessions = without("d2")
        elif name == "done":
            self.sessions = without("d7")
        elif name == "long":
            self.sessions = [dict(self.all[0], body="rm -rf build dist .cache && npm ci --prefer-offline && "
                                  "npm run test:integration -- --grep payment --reporter=verbose && "
                                  "docker compose down --remove-orphans && echo finished")] + self.all[1:]
        elif name == "question":
            qs = [{"q": "Which database should the new service use?", "m": False, "o": [
                {"l": "Postgres", "d": "What the other services run on"},
                {"l": "SQLite", "d": "Simple, one file, no server"},
                {"l": "DynamoDB", "d": "Managed, but new to the team"}]}]
            self.sessions = [dict(id="q1", a="CC", n="Claude", p="checkout-api", s="blocked", age=4, tool="ASK",
                                  d=qs[0]["q"], verb="is asking", ask="choice", rev="ask1", qs=qs,
                                  body=qs[0]["q"] + "\n- Postgres: What the other services run on\n"
                                       "- SQLite: Simple, one file, no server\n- DynamoDB: Managed, but new to the team")] + self.all[3:]
        elif name == "questions":
            qs = [{"q": "Which areas should the audit cover? Pick as many as you like, it will take longer with more.",
                   "m": True, "o": [{"l": "Auth", "d": "Login and sessions"}, {"l": "Payments", "d": "Checkout and refunds"},
                                    {"l": "Uploads", "d": "File handling"}, {"l": "Admin", "d": "Internal tools"}]},
                  {"q": "How deep?", "m": False, "o": [{"l": "Quick", "d": ""}, {"l": "Thorough", "d": ""}]}]
            self.sessions = [dict(id="q2", a="CC", n="Claude", p="web-shop", s="blocked", age=4, tool="ASK",
                                  d=qs[0]["q"], verb="is asking", ask="choice", rev="ask2", qs=qs,
                                  body=qs[0]["q"] + "\n- Auth: Login and sessions\n- Payments: Checkout and refunds\n"
                                       "- Uploads: File handling\n- Admin: Internal tools\n" + qs[1]["q"] + "\n- Quick\n- Thorough")] + self.all[3:]
        elif name == "errored":  # an error, nothing else waiting
            self.sessions = without("d1", "d2")
        elif name == "busy":  # working and done, nothing waiting
            self.sessions = without("d1", "d2", "d7")
        elif name == "many":  # more long asks than the board has room for
            self.sessions = [dict(self.all[0], id=f"m{i}", rev=f"many{i}", body=f"echo {i} " + "x" * 950) for i in range(8)]
        elif name.startswith("pet-"):  # the pet's other states, for testing
            p, what = self.pet_stats, name[4:]
            p.local = lambda t: time.struct_time((2026, 1, 1 if what == "birthday" else 2, 23 if what == "asleep" else 12, 0, 0, 3, 1, 0))
            if what == "old":
                p.born = time.time() - 100 * 86400
            elif what.startswith("lv"):
                p.total = 3 * (int(what[2:]) - 1) ** 2
                p.grown = lambda total=p.total: total
        elif name.startswith("working-"):  # that many agents at work, nothing else
            self.sessions = [dict(id=f"w{i}", a="CC", n="Claude", p=f"project-{i}", s="working", age=30 + i, d="Edit: Cart.tsx")
                             for i in range(int(name[8:]))]
        elif name == "quiet":
            idle = lambda sid, a, n: dict(id=sid, a=a, n=n, p="docs", s="idle", age=600, d="waiting for prompt")
            self.sessions = [idle("q1", "CC", "Claude"), idle("q2", "CC", "Claude"), idle("q3", "CX", "Codex")]
        elif name == "terminal":
            self.sessions = [dict(id="t1", a="CX", n="Codex", p="design-tokens", s="blocked", age=60, tool="SHELL",
                                  d="pnpm add -D culori@4", verb="wants to run", ask="terminal", rev="demo3",
                                  body="pnpm add -D culori@4 --filter tokens",
                                  note="Codex can only be answered in its terminal")] + without("d1", "d2")

    def poll(self):
        return [under_its_name(s, self.given.get(s["id"], "")) for s in self.sessions]

    def desk(self):
        return {s["id"]: {"name": self.given[s["id"]], "folder": s["p"]} for s in self.sessions if s["id"] in self.given}

    def rename(self, sid, name):
        if not any(s["id"] == sid for s in self.all):
            return False, "that agent has gone"
        name = clean_name(name)
        if name:
            self.given[sid] = name
        else:
            self.given.pop(sid, None)
        return True, ""

    def pet(self):
        return self.pet_stats.message()

    def history(self, sid, limit=BOARD_ROWS, board=True):
        return [
            dict(age="-4s", tone=1, text="$ npm run test:int -- --filter payment_client --reporter dot"),
            dict(age="-31s", tone=2, text="allowed on board"),
            dict(age="-1m", tone=0, text="Edit payment_client.ts"),
            dict(age="-2m", tone=0, text="Read retry.ts"),
            dict(age="-3m", tone=0, text="You: add retry w/ backoff to the payment client and cover it with integration tests"),
        ][:limit]

    def act(self, action, sid, rev, picks=None, via="board"):
        if action.startswith("pet-"):
            return self.pet_stats.act(action[4:], rev)
        if action == "answer":
            self.answered = picks
            self.sessions = [s for s in self.sessions if s["id"] != sid]
        return True, ""


class AgentSource:
    """Agents, as rows for the board and the app. What lists them says what it sees (`_listed`), each as herdr
    lists an agent, and all that's made of that list is made here, the same for every source: the rows, what each
    agent is doing, the pet, what's noted of it and the answers."""

    def __init__(self, only_panes=None, hub=None, pet=None):
        self.only_panes = only_panes
        self.hub = hub
        self.pet_stats = pet or Pet()
        self.reachable = True
        self.since: dict[str, tuple[str, float]] = {}
        self.guessed: set[str] = set()         # panes found already at rest: when they came to rest isn't known yet
        self.hist: dict[str, collections.deque] = {}
        self.seen_done: set[str] = set()
        self.answered: dict[str, tuple[float, str]] = {}   # pane -> when it was last answered, and where
        self.noted: dict[str, str] = {}        # pane -> the prompt already in its history
        self.last: list[dict] = []
        self.last_poll = 0.0
        self.agents: dict[str, dict] = {}
        self.states: dict[str, collections.deque] = {}  # status changes: all there is without a session log
        self.peek: set[str] = set()                     # panes someone is looking at
        self.words: dict[str, str] = {}                 # pane -> what it's doing, where the board's line lost some of it
        self.activity = Activity(self._logs())
        self.harnesses = harness.known()
        self.processes = children.shared()
        self.procs: dict[str, tuple[int, float]] = {}   # Claude session -> (its process, when that began)

    def _logs(self):
        return LocalLogs()

    def _wanted(self, agents: list[dict], now: float) -> set[str]:
        return {a["pane_id"] for a in agents}

    def _follow(self, agents: list[dict], now: float):
        sessions = {a["pane_id"]: (a.get("agent_session") or {}).get("value", "") for a in agents
                    if a.get("agent") == "claude"}
        self.activity.sync(sessions, self._wanted(agents, now))

    def _listed(self) -> list[dict] | None:
        """The agents there are now, each as herdr lists one: its `pane_id`, `agent` and `agent_status`, and what
        else is known of it. None: there was no asking, so the rows stay as they were."""
        raise NotImplementedError

    def _read_prompt(self, pane: str, agent: str) -> tuple[str, prompts.Ask]:
        """What's on its screen, and the prompt read from that. Here, nothing of its screen is known."""
        return "", prompts.parse(agent, "")

    def _note(self, pane: str, tone: int, text: str):
        self.hist.setdefault(pane, collections.deque(maxlen=5)).appendleft((time.time(), tone, text))

    def _note_state(self, pane: str, tone: int, text: str):
        self.states.setdefault(pane, collections.deque(maxlen=5)).appendleft((time.time(), tone, text))

    def _rev(self, a: dict, text: str) -> str:
        session = (a.get("agent_session") or {}).get("value", "")
        return hashlib.sha1(f"{a['pane_id']}|{session}|{text}".encode()).hexdigest()[:12]

    def poll(self) -> list[dict]:
        now = time.time()
        if now - self.last_poll < 0.5:
            return self.last
        self.last_poll = now
        agents = self._listed()
        self.reachable = agents is not None
        if agents is None:
            return self.last
        self.agents = {a["pane_id"]: a for a in agents}
        if self.hub:
            self.hub.prune({a["pane_id"]: a.get("agent_status", "unknown") for a in agents})
        self._follow(agents, now)
        out = []
        words = {}

        def doing(s: dict, state: str, text: str, whole: str = ""):
            # The board's line, and for the app all of it, where the board's has lost some: its letters, its end,
            # or the middle of a long path, which the log's line cuts for the board before this does (`whole`).
            s.update(s=state, d=fit(text, 70))
            whole = whole or text
            if s["d"] != whole:
                words[s["id"]] = whole

        def now_whole(pane: str) -> str:
            said = getattr(self.activity, "now_whole", None)
            return said(pane) if said else ""
        alive = set()
        for a in agents:
            pane, status, kind = a["pane_id"], a.get("agent_status", "unknown"), a.get("agent", "")
            alive.add(pane)
            pending = self.hub.get(pane) if self.hub else None
            if pending:  # the hook knows before herdr does
                status = "blocked"
            code, name = AGENTS.get(kind, (kind[:2].upper(), kind.title()))
            # Pi's title starts with its π, which would reach the board as "?"
            title = (a.get("terminal_title_stripped") or "").encode("ascii", "ignore").decode().strip(" -·")
            if title.startswith(f"{kind} ") or title.startswith("claude --"):
                title = ""  # just the command line that started it
            cwd = a.get("foreground_cwd") or a.get("cwd") or ""
            project = "~" if cwd == os.path.expanduser("~") else os.path.basename(cwd.rstrip("/")) or "~"

            prior = self.since.get(pane)
            if not prior or prior[0] != status:
                if prior and status != "blocked":
                    self._note_state(pane, 2 if status == "done" else 0, f"{status}{': ' + title if title else ''}")
                # However it ended: a denied turn earns what an allowed one does, so the pet never favors yes.
                if prior and prior[0] in ("working", "blocked") and status in ("done", "idle"):
                    self.pet_stats.finished()
                self.since[pane] = (status, now)
                self.guessed.discard(pane)
                if not prior and status in ("done", "idle"):
                    self.guessed.add(pane)
                if status != "done":
                    self.seen_done.discard(pane)
            if pane in self.guessed:
                # Found at rest, as after a restart: it's been so since the last thing in its log, not since now.
                events = self.activity.events(pane)
                if events:
                    self.since[pane] = (status, min(now, events[-1].at))
                    self.guessed.discard(pane)
            age = int(now - self.since[pane][1])

            s = {"id": pane, "a": code, "n": name, "p": fit(project, 27), "age": age}
            answered = self.answered.get(pane)
            if answered is not None and (pending or status != "blocked" or time.monotonic() - answered[0] > ANSWER_SETTLES_S):
                del self.answered[pane]
                answered = None
            if status == "blocked" and answered is not None:
                s.update(s="working", d=f"answered {answered[1]}")
                out.append(s)
                continue
            if status == "blocked":
                if pending and self._enabled(pane):
                    v, rev = pending.view, pending.id
                    s.update(s="blocked", d=fit(v.detail, 70), tool=fit(v.tool, 7), verb=fit(v.verb, 19),
                             body=fit(v.body, 1000), rev=rev, ask=v.kind)
                    if v.note:
                        s["note"] = fit(v.note, 70)
                    if v.file:
                        s.update(file=fit(v.file, 31), add=v.add)
                        s["del"] = v.delete
                    if v.questions:
                        s["qs"] = list(v.questions)
                    summary = f"{v.tool} {v.detail}"
                else:
                    text, ask = self._read_prompt(pane, kind)
                    rev = self._rev(a, text)
                    s.update(s="blocked", d=fit(ask.detail, 70), tool=fit(ask.tool, 7), verb=fit(ask.verb, 19),
                             body=fit(ask.body, 1000), rev=rev, ask="terminal", note=fit(ask.note, 70))
                    summary = f"{ask.tool} {ask.detail}"
                if self.noted.get(pane) != rev:
                    self.noted[pane] = rev
                    self._note_state(pane, 1, summary)
            else:
                self.noted.pop(pane, None)
                if status == "done" and pane in self.seen_done:
                    doing(s, "idle", title or "waiting for prompt")
                elif status == "done":
                    doing(s, "done", self.activity.last_said(pane) or title or "finished")
                elif status == "working":
                    now_doing = self.activity.now(pane)
                    doing(s, "working", now_doing or title or "working", now_whole(pane) if now_doing else "")
                elif status == "idle":
                    doing(s, "idle", title or "waiting for prompt")
                else:
                    s.update(s="idle", d="state unknown")
            out.append(s)
        for gone in set(self.since) - alive:
            for table in (self.since, self.hist, self.states, self.answered, self.noted):
                table.pop(gone, None)
            self.seen_done.discard(gone)
            self.guessed.discard(gone)
            self.peek.discard(gone)
        if self.hub:
            self.hub.keep_only(alive)
        self.last = out
        self.words = words
        return out

    def desk(self) -> dict[str, dict]:
        """For the app, not the board: the pane you're on, how each finished turn ended, in full
        characters, the session each agent is in, as the agent itself names it, and the folder it's listed in."""
        out = {}
        for pane, a in self.agents.items():
            extra = {}
            if self._focused_here(a):
                extra["foc"] = True
            if a.get("agent_status") in ("done", "idle"):
                said = self.activity.last_said(pane)
                if said:
                    extra["said"] = "".join(c for c in " ".join(said.split()) if c.isprintable())[:MAX_SAID]
            made = getattr(self.activity, "requests", None)
            made = made(pane) if made else []
            if made:
                extra["prs"] = made
            extra.update(self._counts(pane, a))
            # Its own name for its session, where that's known: what an extension can look up in
            # what else it knows of the agent's sessions. It names nothing but the session.
            session = self._running(pane, a).session
            if session and SESSION_NAME.fullmatch(session):
                extra["ses"] = session
            whole = self.words.get(pane)
            if whole:
                extra["dl"] = "".join(c for c in " ".join(whole.split()) if c.isprintable())[:MAX_WORDS]
            branch = self._branch(pane, a)
            if branch:
                extra["br"] = branch
            # The folder it's listed in, in full: where whatever found it says it is, which for an agent is as a
            # rule where it was started. Where it works now is about()'s. Whole, or not at all.
            folder = a.get("cwd")
            if isinstance(folder, str) and os.path.isabs(folder) and len(folder) <= LONGEST_FOLDER and folder.isprintable():
                extra["from"] = folder
            if extra:
                out[pane] = extra
        return out

    def _branch(self, pane: str, a: dict) -> str | None:
        """The branch it works on, for its card. Its folder's, read here when the folder is on this Mac and may be
        read without asking (may_look_in): that's so now, whatever agent it is. Its folder is where its log says
        it is now, since an agent started above its repositories moves into the one it works in: only where the
        log names none is it where it was started. Otherwise, as for another machine's, whose folder isn't
        here, the branch its own log last named: Claude names it with everything it writes."""
        said = getattr(self.activity, "about", None)
        known = said(pane) if said else {}
        cwd = known.get("cwd") or a.get("foreground_cwd") or a.get("cwd") or ""
        if cwd and not getattr(self, "host", None) and may_look_in(cwd):
            found = git_branch(cwd)
            if found:
                return found
        return clean_branch(known.get("branch"))

    def _running(self, pane: str, a: dict) -> harness.Running:
        session = (a.get("agent_session") or {}).get("value", "")
        pid, started = self.procs.get(session, (None, 0.0))
        cwd = a.get("cwd") or ""
        alone = sum(1 for b in self.agents.values() if b.get("agent") == a.get("agent") and (b.get("cwd") or "") == cwd) <= 1
        return harness.Running(session, pid, started, cwd, alone)

    def _counts(self, pane: str, a: dict) -> dict:
        """Its subagents and open shells, as far as its kind of agent can tell: see harness.py."""
        kind = self.harnesses.get(a.get("agent", ""))
        return kind.counts(self._running(pane, a), self.processes) if kind else {}

    def _focused_here(self, a: dict) -> bool:
        return bool(a.get("focused"))

    def history(self, pane: str, limit: int = BOARD_ROWS, board: bool = True) -> list[dict]:
        """Newest first: what the agent did, and the answers given here. The board's fonts
        are ASCII; the app shows the text as it is."""
        self.peek.add(pane)
        # Newest first before sorting: calls made in one message share a timestamp, and the sort keeps ties in order.
        events = [(e.at, e.tone, e.shown if board else e.shown_whole) for e in reversed(self.activity.events(pane))]
        rows = events + list(self.hist.get(pane, ()))
        if not events:  # no log to read: an agent other than Claude, or a format this can't follow
            rows += list(self.states.get(pane, ()))
        rows.sort(key=lambda row: row[0], reverse=True)
        now = time.time()
        items = []
        for at, tone, text in rows[:limit]:
            ago = max(0, int(now - at))
            age = f"-{ago}s" if ago < 60 else f"-{ago // 60}m" if ago < 3600 else f"-{ago // 3600}h" if ago < 86400 else f"-{ago // 86400}d"
            line = " ".join(text.split())
            line = fit(line, 120) if board else "".join(c for c in line if c.isprintable())[:300]
            items.append({"age": age, "tone": tone, "text": line})
        return items

    def about(self, pane: str) -> dict:
        """For the app's page of one agent: what's known of it beside its state. What isn't known is left out.
        Its folder is the one it's working in now, as its log says: where the log names none, the one it was
        started in."""
        a = self.agents.get(pane)
        if a is None:
            return {}
        said = getattr(self.activity, "about", None)
        out = dict(said(pane)) if said else {}
        if not out.get("cwd") and a.get("cwd"):
            out["cwd"] = str(a["cwd"])[:300]
        started = self._running(pane, a).started
        if started:
            out["up"] = max(0, int(time.time() - started))
        return out

    def act(self, action: str, pane: str, rev: str, picks=None, via: str = "board") -> tuple[bool, str]:
        if action == "seen":
            self.seen_done.add(pane)
            self.last_poll = 0
            return True, ""
        if action == "open":
            return self._open(pane)
        if action in ("allow", "answer", "deny"):
            return self._answer(action, pane, rev, picks, via)
        return False, "unknown action"

    def _open(self, pane: str) -> tuple[bool, str]:
        return False, "it can't be opened from here"

    def _enabled(self, pane: str) -> bool:
        return self.only_panes is None or pane in self.only_panes

    def _answer(self, action: str, pane: str, rev: str, picks=None, via: str = "board") -> tuple[bool, str]:
        """Goes only to the hook process that asked, and only if it's still the one shown."""
        if not self._enabled(pane):
            return False, "pane not enabled"
        if not self.hub:
            return False, "answer it in the terminal"
        ok, msg = self.hub.answer(pane, rev, action, picks)
        if ok:
            where = WHERE.get(via, via)
            self.answered[pane] = (time.monotonic(), where)
            word = {"allow": "allowed", "answer": "answered"}.get(action, "denied")
            self._note(pane, 1 if word == "denied" else 2, f"{word} {where}")
            self.last_poll = 0
        return ok, msg

    def pet(self):
        return self.pet_stats.message()


class HerdrSource(AgentSource):
    """The agents herdr holds, asked of its command line."""

    def __init__(self, focus_app="Ghostty", only_panes=None, hub=None, pet=None):
        super().__init__(only_panes=only_panes, hub=hub, pet=pet)
        self.chosen_app = focus_app  # None: whichever terminal herdr turns out to be in
        self.found_app = (0.0, FALLBACK_TERMINAL)
        self.herdr_timeout = 3

    @property
    def focus_app(self) -> str:
        if self.chosen_app:
            return self.chosen_app
        now = time.monotonic()
        if not self.found_app[0] or now - self.found_app[0] >= TERMINAL_LOOK_S:
            self.found_app = (now, herdr_terminal() or FALLBACK_TERMINAL)
        return self.found_app[1]

    def _command(self, *args) -> list[str]:
        return ["herdr", *args]

    def _run(self, *args) -> str | None:
        """herdr's output, or None if it failed."""
        try:
            out = subprocess.run(self._command(*args), capture_output=True, text=True, timeout=self.herdr_timeout)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return out.stdout if out.returncode == 0 else None

    def _herdr(self, *args) -> dict | None:
        out = self._run(*args)
        if out is None:
            return None
        try:
            return json.loads(out)
        except ValueError:
            return None

    def _listed(self) -> list[dict] | None:
        listed = self._herdr("agent", "list")
        return None if listed is None else listed.get("result", {}).get("agents", [])

    def _read_prompt(self, pane: str, agent: str) -> tuple[str, prompts.Ask]:
        text = self._run("agent", "read", pane, "--source", "detection", "--lines", "60") or ""
        return text, prompts.parse(agent, text)

    def _open(self, pane: str) -> tuple[bool, str]:
        if not self._herdr("agent", "focus", pane):
            return False, "herdr could not focus it"
        try:
            done = subprocess.run(["open", "-a", self.focus_app], capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return False, f"could not open {self.focus_app}"
        return (True, "") if done.returncode == 0 else (False, f"could not open {self.focus_app}")


def under_its_name(row: dict, name: str) -> dict:
    """The row as the board lists it once its agent has a name: under that, in the letters the board has, where it
    listed the folder. The row itself is left alone: a source may hand out the same one again."""
    shown = fit(for_board(name), 27) if name else ""
    return {**row, "p": shown} if shown else row


class Fleet:
    """This Mac's sessions plus each remote machine's, as one list for the board."""

    def __init__(self, local: AgentSource, remotes: list = (), start=True, sessions=None, running=None, threads=None,
                 names: AgentNames | None = None):
        self.local = local
        self.names = names or AgentNames()   # the names people gave their agents: see agent_names.py
        self.given: dict[str, dict] = {}     # row -> its name and the folder it stood for, for the app
        self.remotes = list(remotes)
        self.sessions = sessions  # Claude sessions outside herdr: see claude_sessions.py
        self.running = running    # any other agent outside herdr
        self.threads = threads    # the Codex app's threads: see codex_threads.py
        if start:
            for r in self.remotes:
                threading.Thread(target=r.run_forever, daemon=True).start()

    @property
    def last_poll(self):
        return self.local.last_poll

    @last_poll.setter
    def last_poll(self, value):
        self.local.last_poll = value
        if self.sessions:
            self.sessions.last_poll = value
        if self.running:
            self.running.last_poll = value
        if self.threads:
            self.threads.last_poll = value

    def _route(self, sid: str) -> tuple[AgentSource | None, str]:
        head, sep, pane = sid.partition("/")
        if not sep:
            if sid.startswith("s:") and self.sessions:
                return self.sessions, sid
            if sid.startswith("p:") and self.running:
                return self.running, sid
            if sid.startswith("c:") and self.threads:
                return self.threads, sid
            return self.local, sid
        if head.isdigit() and 1 <= int(head) <= len(self.remotes):
            return self.remotes[int(head) - 1], pane
        return None, sid

    def _remote_rows(self, n: int, r) -> list[dict]:
        if r.stale:  # no news is not good news: its agents could be waiting on you
            return [{"id": f"{n}/down", "a": "--", "n": r.label, "host": r.label, "p": "not reachable", "s": "error",
                     "age": int(r.clock() - r.heard_at), "d": "its agents can't be seen"}]
        return [{**row, "id": f"{n}/{row['id']}", "host": r.label} for row in r.rows]

    def poll(self) -> list[dict]:
        rows = list(self.local.poll())
        if self.sessions:
            self.sessions.herdr_up = self.local.reachable
            self.local.procs = self.sessions.procs
            self.sessions.herdr_sessions = {(a.get("agent_session") or {}).get("value", "")
                                            for a in self.local.agents.values()} - {""}
            rows += self.sessions.poll()
        if self.running:
            self.running.herdr_up = self.local.reachable
            self.running.claude_pids = self.sessions.all_pids if self.sessions else set()
            rows += self.running.poll()
        if self.threads:
            rows += self.threads.poll()
        for n, r in enumerate(self.remotes, 1):
            rows += self._remote_rows(n, r)
        # the board holds 24: if there are more, what needs you must be among them
        rows.sort(key=row_order)
        return self._named(rows)

    def name_key(self, sid: str) -> str | None:
        """What an agent's name is kept under, which no later agent may go by. For one herdr holds, that's its
        terminal, which herdr numbers once and for all, where a pane's own number comes round again. For the rest
        it's what they're listed by: a session, a thread, or a process. Another machine's are kept apart from this
        Mac's by its host. None: not an agent that's there."""
        source, pane = self._route(sid)
        listed = getattr(source, "agents", {}).get(pane) if source else None
        if listed is None:
            return None
        own = str(listed.get("terminal_id") or pane)
        host = getattr(source, "host", None)
        return f"{host}/{own}" if host else own

    def _given(self, sid: str) -> str:
        """The name it goes by: the one given here, else the one herdr has for it, if it was given one there."""
        key = self.name_key(sid)
        if key is None:
            return ""
        source, pane = self._route(sid)
        return self.names.get(key) or clean_name(source.agents[pane].get("name"))

    def _named(self, rows: list[dict]) -> list[dict]:
        self.names.tidy()
        given, out = {}, []
        for row in rows:
            name = self._given(row["id"])
            if name:
                given[row["id"]] = {"name": name, "folder": row.get("p", "")}
            out.append(under_its_name(row, name))
        self.given = given
        return out

    def rename(self, sid: str, name: str) -> tuple[bool, str]:
        """Give an agent a name, or with "" take back the one given here, so it goes by its folder again."""
        key = self.name_key(sid)
        if key is None:
            return False, "that agent has gone"
        self.names.set(key, name)
        return True, ""

    @property
    def focus_app(self):
        return self.local.focus_app

    def desk(self) -> dict[str, dict]:
        out = dict(self.local.desk())
        for source in (self.sessions, self.running, self.threads):
            if source:
                out.update(source.desk())
        for n, r in enumerate(self.remotes, 1):
            out.update({f"{n}/{pane}": extra for pane, extra in r.desk().items()})
        # The name in full, as it was typed, and the folder the board's row no longer says.
        for sid, name in self.given.items():
            out[sid] = {**out.get(sid, {}), **name}
        return out

    def history(self, sid: str, limit: int = BOARD_ROWS, board: bool = True) -> list[dict]:
        source, pane = self._route(sid)
        return source.history(pane, limit, board) if source else []

    def about(self, sid: str) -> dict:
        source, pane = self._route(sid)
        return source.about(pane) if source else {}

    def act(self, action: str, sid: str, rev: str, picks=None, via: str = "board") -> tuple[bool, str]:
        if action.startswith("pet-"):
            return self.local.pet_stats.act(action[4:], rev)
        source, pane = self._route(sid)
        return source.act(action, pane, rev, picks, via) if source else (False, "unknown session")

    def pet(self):
        return self.local.pet()

    def rename_pet(self, name: str):
        self.local.pet_stats.rename(name)
