"""Claude Code permission requests, arriving from hook.py over a unix socket.

Each hook process stays connected until its prompt is settled, and a decision goes
only to the process that asked. That binds an answer to one exact request: there is
no terminal screen to read and no keypress that could land on something else.

What real Claude Code does, and what follows from it (tested on 2.1.316):
- The terminal prompt stays up while a hook runs, so a hook can wait as long as it
  likes, and a hook that crashes or says nothing leaves the prompt open.
- Answering Yes in the terminal does not end the hook, so requests are dropped here
  once herdr says the pane stopped waiting.
- A late decision on an answered prompt is ignored by Claude.
- Parallel tool calls ask one at a time: a new request in a pane replaces the last.
- Allow with `answers` in updatedInput answers an AskUserQuestion, including free text
  and several questions at once. Allow alone would dismiss it without an answer.
"""
from __future__ import annotations

import difflib
import json
import os
import secrets
import socket
import time
from dataclasses import dataclass
from pathlib import Path

HOOK_SOCK = Path.home() / ".ttycat" / "hook.sock"
MAX_BODY = 1000      # what the board holds. Anything longer isn't offered: nothing is approved unseen.
MAX_REQUEST = 4 * 1024 * 1024
GRACE_S = 3.0        # herdr notices a prompt a moment after the hook does

TERMINAL_NOTE = "Answer this one in the terminal"
TOO_LONG_NOTE = "Too long to read here. Answer it in the terminal"
MAX_QUESTIONS, MAX_OPTIONS = 4, 4   # what Claude's tool allows, and what the board has room for
MAX_TYPED = 1000                    # the Mac app's field stops here; the board's keyboard sooner

DENIALS = {
    "deny": ("The person denied this from ttycat and wants to decide what happens next.", True),
    "deny-retry": ("The person denied this from ttycat. Try a different approach.", False),
    "deny-explain": ("The person denied this from ttycat. Explain why it is needed before trying again.", False),
}


def fit(text: str, n: int) -> str:
    """The board's fonts are ASCII plus the middle dot."""
    text = "".join(c if 32 <= ord(c) < 127 or c == "\n" else "?" for c in text.replace("·", "-"))
    return text[:n]


@dataclass
class View:
    """A request as the board shows it."""
    kind: str = "approve"      # approve | choice | terminal
    tool: str = "TOOL"         # at most 6 characters: the big word on the card
    verb: str = "wants to use"
    body: str = ""
    detail: str = ""
    file: str = ""
    add: int = 0
    delete: int = 0
    note: str = ""
    questions: tuple = ()      # for a choice: what the board shows
    asked: tuple = ()          # for a choice: the original text, which answers must use


def _short_path(path: str, cwd: str) -> str:
    """Enough of a path to tell ./config from ~/.ssh/config."""
    if cwd and path.startswith(cwd.rstrip("/") + "/"):
        path = path[len(cwd.rstrip("/")) + 1:]
    elif path.startswith(str(Path.home()) + "/"):
        path = "~" + path[len(str(Path.home())):]
    return path if len(path) <= 31 else "..." + path[-28:]


def _first_line(text: str) -> str:
    return next((l.strip() for l in text.splitlines() if l.strip()), "")


def _text(value) -> str:
    return (value if isinstance(value, str) else "").replace("\t", "  ")


def _diff_view(view: View, old: str, new: str) -> View:
    rows = []
    for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0):
        if line.startswith(("---", "+++", "@@")):
            continue
        rows.append(f"{line[0]} {line[1:]}")
        view.add += line[0] == "+"
        view.delete += line[0] == "-"
    view.body = "\n".join(rows) or "(no visible change)"
    view.detail = f"{view.file} +{view.add} -{view.delete}"
    return view


def _arguments(name: str, args: dict) -> str:
    lines = [name]
    for key, value in args.items():
        lines.append(f"{key}: {value if isinstance(value, str) else json.dumps(value)}")
    return "\n".join(lines)


def _question_view(args: dict) -> View:
    """A choice the board can offer, or a terminal-only ask if it's not one it understands."""
    questions = args.get("questions")
    questions = questions if isinstance(questions, list) else []
    texts = [q.get("question") if isinstance(q, dict) else None for q in questions]
    lines = []
    shown, asked = [], []
    ok = 1 <= len(questions) <= MAX_QUESTIONS and len(set(map(str, texts))) == len(texts)
    for q, text in zip(questions, texts):
        options = q.get("options") if isinstance(q, dict) else None
        options = options if isinstance(options, list) else []
        labels = [o.get("label") if isinstance(o, dict) else None for o in options]
        if not (isinstance(text, str) and text.strip()) or not 1 <= len(options) <= MAX_OPTIONS or \
                not all(isinstance(l, str) and l.strip() for l in labels):
            ok = False
        if isinstance(text, str):
            lines.append(text)
        rows = [{"l": fit(str(l), 60), "d": fit(_text(o.get("description")) if isinstance(o, dict) else "", 90)}
                for l, o in zip(labels, options)]
        lines += [f"- {r['l']}: {r['d']}" if r["d"] else f"- {r['l']}" for r in rows]
        shown.append({"q": fit(str(text), 300), "m": bool(q.get("multiSelect")) if isinstance(q, dict) else False, "o": rows})
        asked.append((text, tuple(labels)))
    body = "\n".join(lines) or "Waiting at its terminal"
    if not ok:
        return View("terminal", "ASK", "is asking", body, note=TERMINAL_NOTE)
    return View("choice", "ASK", "is asking", body, questions=tuple(shown), asked=tuple(asked))


def build_answers(view: View, picks) -> dict | None:
    """What the board chose, as Claude expects it. Labels come from the original request:
    the board only sends option numbers, so it can't pass off anything else as a choice."""
    if not isinstance(picks, list) or len(picks) != len(view.asked):
        return None
    answers = {}
    for pick, shown, (question, labels) in zip(picks, view.questions, view.asked):
        if not isinstance(pick, dict):
            return None
        numbers = pick.get("o", [])
        typed = pick.get("t", "")
        if not isinstance(numbers, list) or not isinstance(typed, str):
            return None
        if any(not isinstance(n, int) or isinstance(n, bool) or not 0 <= n < len(labels) for n in numbers):
            return None
        typed = "".join(c for c in typed if c.isprintable()).strip()[:MAX_TYPED]
        parts = [labels[n] for n in sorted(set(numbers))] + ([typed] if typed else [])
        if not parts or (len(parts) > 1 and not shown["m"]):  # one answer to a single choice
            return None
        answers[question] = ", ".join(parts)
    return answers


def describe(request: dict) -> View:
    name = str(request.get("tool_name") or "")
    args = request.get("tool_input")
    args = args if isinstance(args, dict) else {}
    cwd = str(request.get("cwd") or "")

    if name == "AskUserQuestion":
        view = _question_view(args)
    elif name == "ExitPlanMode":  # Allow would dismiss it without the answer it's waiting for
        view = View("terminal", "PLAN", "has a plan for you", _text(args.get("plan")) or "Waiting at its terminal",
                    note=TERMINAL_NOTE)
    elif name == "Bash":
        view = View(tool="BASH", verb="wants to run", body=_text(args.get("command")))
    elif name == "Edit":
        verb = "wants to change all" if args.get("replace_all") else "wants to change"
        view = _diff_view(View(tool="EDIT", verb=verb, file=_short_path(_text(args.get("file_path")), cwd)),
                          _text(args.get("old_string")), _text(args.get("new_string")))
    elif name == "Write":
        view = _diff_view(View(tool="WRITE", verb="wants to write", file=_short_path(_text(args.get("file_path")), cwd)),
                          "", _text(args.get("content")))
    elif name == "Read":
        view = View(tool="READ", verb="wants to read", body=_text(args.get("file_path")))
    elif name == "WebFetch":
        view = View(tool="FETCH", verb="wants to fetch", body=_text(args.get("url")))
    elif name == "WebSearch":
        view = View(tool="SEARCH", verb="wants to search", body=_text(args.get("query")))
    elif name.startswith("mcp__"):
        view = View(tool="MCP", body=_arguments(name[5:].replace("__", "."), args))
    else:
        view = View(body=_arguments(name, args))

    if not view.body.strip():  # never offer a blank card
        view.kind, view.note, view.body = "terminal", TERMINAL_NOTE, "Waiting at its terminal"
    if len(view.body) > MAX_BODY:
        view.kind, view.note = "terminal", TOO_LONG_NOTE
    view.body = view.body[:MAX_BODY]
    view.detail = view.detail or _first_line(view.body)
    return view


@dataclass
class Pending:
    id: str
    pane: str
    view: View
    conn: socket.socket
    at: float
    confirmed: bool = False  # something other than the hook has said it's waiting


class HubScope:
    """Part of a hub, for one source of agents: one that knows nothing of a prompt elsewhere must
    not take its silence for that prompt having gone."""

    def __init__(self, hub, mine):
        self.hub, self.mine = hub, mine

    def get(self, pane: str):
        return self.hub.get(pane) if self.mine(pane) else None

    def prune(self, statuses: dict[str, str]):
        self.hub.prune(statuses, self.mine)

    def keep_only(self, alive: set[str]):
        self.hub.keep_only(alive, self.mine)

    def answer(self, pane: str, rev: str, action: str, picks=None) -> tuple[bool, str]:
        if not self.mine(pane):
            return False, "prompt already gone"
        return self.hub.answer(pane, rev, action, picks)


class Hub:
    """Non-blocking: the daemon's select loop calls handle() for each ready socket."""

    def __init__(self, path: Path = HOOK_SOCK, clock=time.monotonic):
        self.path, self.clock = path, clock
        self.grace = GRACE_S
        path.parent.mkdir(mode=0o700, exist_ok=True)
        path.unlink(missing_ok=True)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        os.chmod(path, 0o600)
        self.listener.listen(16)
        self.listener.setblocking(False)
        self.incoming: dict[socket.socket, bytearray] = {}
        self.pending: dict[str, Pending] = {}

    def fds(self) -> list[socket.socket]:
        return [self.listener, *self.incoming, *(p.conn for p in self.pending.values())]

    def owns(self, sock) -> bool:
        return sock is self.listener or sock in self.incoming or any(p.conn is sock for p in self.pending.values())

    def get(self, pane: str) -> Pending | None:
        return self.pending.get(pane)

    def handle(self, sock: socket.socket):
        if sock is self.listener:
            try:
                conn, _ = sock.accept()
            except OSError:
                return
            conn.setblocking(False)
            self.incoming[conn] = bytearray()
            return
        try:
            data = sock.recv(65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if sock not in self.incoming:
            if not data:  # the hook ended: Claude cancelled it, or the prompt was answered
                self._drop_conn(sock)
            return
        if not data:
            self.incoming.pop(sock)
            sock.close()
            return
        buf = self.incoming[sock]
        buf += data
        if len(buf) > MAX_REQUEST:  # too big to show anyway: the terminal prompt stands
            self.incoming.pop(sock)
            sock.close()
        elif b"\n" in buf:
            self.incoming.pop(sock)
            self._admit(sock, bytes(buf))

    def _admit(self, conn: socket.socket, raw: bytes):
        try:
            msg = json.loads(raw)
            pane, request = msg["pane"], msg["request"]
            view = describe(request)
        except (ValueError, KeyError, TypeError, AttributeError):
            conn.close()
            return
        self.close_pane(pane)  # asks arrive one at a time, so the older one is settled
        self.pending[pane] = Pending(secrets.token_hex(6), pane, view, conn, self.clock())

    def _drop_conn(self, conn: socket.socket):
        for pane, p in list(self.pending.items()):
            if p.conn is conn:
                del self.pending[pane]
        conn.close()

    def close_pane(self, pane: str):
        """Lets go of a request without deciding it. Any terminal prompt stands."""
        p = self.pending.pop(pane, None)
        if p:
            p.conn.close()

    def prune(self, statuses: dict[str, str], only=None):
        """Drops requests whose prompt is gone. `statuses` is herdr's view, by pane."""
        now = self.clock()
        for pane, p in list(self.pending.items()):
            if only and not only(pane):
                continue
            if statuses.get(pane) == "blocked":
                p.confirmed = True
            elif p.confirmed or now - p.at >= self.grace:  # the wait is only for it to notice in the first place
                self.close_pane(pane)

    def keep_only(self, alive: set[str], only=None):
        """Lets go of requests from panes that no longer exist."""
        for pane in set(self.pending) - alive:
            if not only or only(pane):
                self.close_pane(pane)

    def settle(self):
        """Called from the select loop. Nothing's held back here: see remote_hook.RemoteHub."""

    def answer(self, pane: str, rev: str, action: str, picks=None) -> tuple[bool, str]:
        p = self.pending.get(pane)
        if not p or p.id != rev:
            return False, "prompt already gone"
        if p.view.kind == "choice" and action == "answer":
            answers = build_answers(p.view, picks)
            if answers is None:
                return False, "answer every question"
            reply = {"behavior": "allow", "answers": answers}
        elif p.view.kind != "approve":
            return False, "answer it in the terminal"
        elif action == "allow":
            reply = {"behavior": "allow"}
        elif action in DENIALS:
            message, interrupt = DENIALS[action]
            reply = {"behavior": "deny", "message": message, "interrupt": interrupt}
        else:
            return False, "unknown action"
        try:
            p.conn.setblocking(True)
            p.conn.settimeout(1)
            p.conn.sendall(json.dumps(reply).encode() + b"\n")
        except OSError:
            self.close_pane(pane)
            return False, "prompt already gone"
        self.close_pane(pane)
        return True, ""

    def close(self):
        for conn in [*self.incoming, *(p.conn for p in self.pending.values())]:
            conn.close()
        self.listener.close()
        self.path.unlink(missing_ok=True)
