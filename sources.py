"""Where the board's sessions come from: herdr, or the design's sample data."""
from __future__ import annotations

import collections
import hashlib
import json
import os
import re
import shlex
import subprocess
import threading
import time
from pathlib import Path

import prompts
from discovery import is_ours
from activity import Activity, LocalLogs
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
RANK = {"blocked": 0, "error": 1, "done": 2, "working": 3, "idle": 4}


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
        return self.sessions

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


class HerdrSource:
    def __init__(self, focus_app="Ghostty", only_panes=None, hub=None, pet=None):
        self.chosen_app = focus_app  # None: whichever terminal herdr turns out to be in
        self.found_app = (0.0, FALLBACK_TERMINAL)
        self.only_panes = only_panes
        self.hub = hub
        self.pet_stats = pet or Pet()
        self.reachable = True
        self.herdr_timeout = 3
        self.since: dict[str, tuple[str, float]] = {}
        self.hist: dict[str, collections.deque] = {}
        self.seen_done: set[str] = set()
        self.answered: dict[str, tuple[float, str]] = {}   # pane -> when it was last answered, and where
        self.noted: dict[str, str] = {}        # pane -> the prompt already in its history
        self.last: list[dict] = []
        self.last_poll = 0.0
        self.agents: dict[str, dict] = {}
        self.states: dict[str, collections.deque] = {}  # status changes: all there is without a session log
        self.peek: set[str] = set()                     # panes someone is looking at
        self.activity = Activity(self._logs())

    def _logs(self):
        return LocalLogs()

    @property
    def focus_app(self) -> str:
        if self.chosen_app:
            return self.chosen_app
        now = time.monotonic()
        if not self.found_app[0] or now - self.found_app[0] >= TERMINAL_LOOK_S:
            self.found_app = (now, herdr_terminal() or FALLBACK_TERMINAL)
        return self.found_app[1]

    def _wanted(self, agents: list[dict], now: float) -> set[str]:
        return {a["pane_id"] for a in agents}

    def _follow(self, agents: list[dict], now: float):
        sessions = {a["pane_id"]: (a.get("agent_session") or {}).get("value", "") for a in agents
                    if a.get("agent") == "claude"}
        self.activity.sync(sessions, self._wanted(agents, now))

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

    def _read_prompt(self, pane: str, agent: str) -> tuple[str, prompts.Ask]:
        text = self._run("agent", "read", pane, "--source", "detection", "--lines", "60") or ""
        return text, prompts.parse(agent, text)

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
        r = self._herdr("agent", "list")
        self.reachable = r is not None
        if r is None:
            return self.last
        agents = r.get("result", {}).get("agents", [])
        self.agents = {a["pane_id"]: a for a in agents}
        if self.hub:
            self.hub.prune({a["pane_id"]: a.get("agent_status", "unknown") for a in agents})
        self._follow(agents, now)
        out = []
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
                if status != "done":
                    self.seen_done.discard(pane)
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
                    s.update(s="idle", d=fit(title or "waiting for prompt", 70))
                elif status == "done":
                    s.update(s="done", d=fit(self.activity.last_said(pane) or title or "finished", 70))
                elif status == "working":
                    s.update(s="working", d=fit(self.activity.now(pane) or title or "working", 70))
                elif status == "idle":
                    s.update(s="idle", d=fit(title or "waiting for prompt", 70))
                else:
                    s.update(s="idle", d="state unknown")
            out.append(s)
        for gone in set(self.since) - alive:
            for table in (self.since, self.hist, self.states, self.answered, self.noted):
                table.pop(gone, None)
            self.seen_done.discard(gone)
            self.peek.discard(gone)
        if self.hub:
            self.hub.keep_only(alive)
        self.last = out
        return out

    def desk(self) -> dict[str, dict]:
        """For the app's notifications, not the board: the pane you're on, and how each
        finished turn ended, in full characters."""
        out = {}
        for pane, a in self.agents.items():
            extra = {}
            if self._focused_here(a):
                extra["foc"] = True
            if a.get("agent_status") in ("done", "idle"):
                said = self.activity.last_said(pane)
                if said:
                    extra["said"] = "".join(c for c in " ".join(said.split()) if c.isprintable())[:MAX_SAID]
            if extra:
                out[pane] = extra
        return out

    def _focused_here(self, a: dict) -> bool:
        return bool(a.get("focused"))

    def history(self, pane: str, limit: int = BOARD_ROWS, board: bool = True) -> list[dict]:
        """Newest first: what the agent did, and the answers given here. The board's fonts
        are ASCII; the app shows the text as it is."""
        self.peek.add(pane)
        # Newest first before sorting: calls made in one message share a timestamp, and the sort keeps ties in order.
        events = [(e.at, e.tone, e.shown) for e in reversed(self.activity.events(pane))]
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
        if not self._herdr("agent", "focus", pane):
            return False, "herdr could not focus it"
        try:
            done = subprocess.run(["open", "-a", self.focus_app], capture_output=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return False, f"could not open {self.focus_app}"
        return (True, "") if done.returncode == 0 else (False, f"could not open {self.focus_app}")

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


class Fleet:
    """This Mac's sessions plus each remote machine's, as one list for the board."""

    def __init__(self, local: HerdrSource, remotes: list = (), start=True, sessions=None, running=None, threads=None):
        self.local = local
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

    def _route(self, sid: str) -> tuple[HerdrSource | None, str]:
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
        rows.sort(key=lambda row: RANK.get(row.get("s"), 9))
        return rows

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
        return out

    def history(self, sid: str, limit: int = BOARD_ROWS, board: bool = True) -> list[dict]:
        source, pane = self._route(sid)
        return source.history(pane, limit, board) if source else []

    def act(self, action: str, sid: str, rev: str, picks=None, via: str = "board") -> tuple[bool, str]:
        if action.startswith("pet-"):
            return self.local.pet_stats.act(action[4:], rev)
        source, pane = self._route(sid)
        return source.act(action, pane, rev, picks, via) if source else (False, "unknown session")

    def pet(self):
        return self.local.pet()

    def rename_pet(self, name: str):
        self.local.pet_stats.rename(name)
