"""Where an agent's terminal is, when that's somewhere its screen can be read.

Most terminals show their screens to nobody but you: an agent in one is known to be running, and no more.
A place is somewhere that will say what a terminal in it shows, so that what an agent there is doing can be
read off its screen, by the same rules for every place (screen_rules.py). tmux is one: it's asked by its
command line, on each of your servers. A hosted session is another: a terminal held by a process of its own,
that says what it shows to whoever asks at its socket.

A place is only ever read, and asked to bring a terminal forward. Nothing is typed into one.


Hosted sessions

A session's host is a process that holds one terminal and outlives whatever started it. In the sessions folder
(paths.sessions_dir) it keeps, for a session it names ID:

  ID.json   what it is, as JSON: `v` (the version of all this it speaks), `id`, `host` (its own process),
            `child` (the program it started), `tty` (its terminal, as /dev/NAME), and what else it likes
  ID.sock   a socket, for as long as it's there

Frames go both ways over the socket: one byte for the kind, four for the length, big-endian, then that many
bytes. Kinds and JSON keys are only ever added: whatever isn't known is skipped. This reads version 2 and later.

  to the host:   h  hello, first: JSON, with `v` and a `role`. As "read", which is all that's ever said here,
                    nothing can be typed, resized or ended, and nothing is sent that wasn't asked for.
                 q  what does it show?
  from the host: h  hello back: JSON
                 Q  the answer to a q: JSON, with
                      title     its terminal's title
                      text      its screen as text, a line to a row
                      progress  what its program last said of its progress, as the text of an OSC 9;4 from the 4
                      said      what its program says of itself by OSC 7501, at the root: `state`, one of
                                idle, working, done, blocked, error, and what else it gave. Without it: null
                      alive     whether its program is still running
"""
from __future__ import annotations

import json
import os
import re
import socket
import struct
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import paths
from discovery import Found, place_in_tmux, tmux_panes, tmux_place


@dataclass
class Shown:
    """What a terminal shows now: its title, and its screen as text. Where the place keeps them, what its
    program last said of its progress (OSC 9;4), and what it says of itself (OSC 7501)."""
    title: str = ""
    text: str = ""
    progress: str = ""
    said: dict | None = None


class Place:
    """Somewhere terminals are, that says what they show. What it knows a terminal by is its own business: a
    source hands that back as it was given."""

    def find(self, found: list[Found]) -> dict[int, object]:
        """Which of these agents are in here: each one's process, to what its terminal is known by here."""
        return {}

    def shown(self, held) -> Shown | None:
        """What that terminal shows. None: it couldn't be read just now."""
        return None

    def bring_forward(self, held):
        """Turns to that terminal, where the place has more than one to show."""


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


class TmuxPlace(Place):
    """tmux on this machine. A terminal in it is known by its server's socket and its pane: (socket, pane)."""

    def __init__(self, panes=list_tmux_panes, capture=capture_pane, run=subprocess.run):
        self.panes, self.capture, self.run = panes, capture, run

    def find(self, found: list[Found]) -> dict[int, object]:
        # By terminal, and only asked when there's an agent to place. Each one found is marked as tmux's too,
        # which is what tells it from one herdr can see.
        place_in_tmux(found, tmux_panes(self.panes()) if found else {})
        return {f.pid: (f.tmux_socket, f.tmux_pane) for f in found if tmux_place(f)}

    def shown(self, held) -> Shown | None:
        got = self.capture(*held)
        return Shown(*got) if got else None

    def bring_forward(self, held):
        """The pane's window, then the pane. A blank socket is the server tmux finds by itself."""
        socket, pane = held
        base = ["tmux", "-S", socket] if socket else ["tmux"]
        self.run([*base, "select-window", "-t", pane], capture_output=True, timeout=3)
        self.run([*base, "select-pane", "-t", pane], capture_output=True, timeout=3)


SPEAKS = 2                       # the version of the hosts' frames this reads: see above
SESSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SAID_STATES = ("idle", "working", "done", "blocked", "error")
LARGEST_ANSWER = 1024 * 1024     # more than a screen is: a host that sends it isn't one of these
LONGEST_SCREEN = 256 * 1024      # of a screen's text that's read
ANSWER_WITHIN_S = 0.25           # how long one look waits for a host: its answer counts next time if it's later
SILENT_FOR_S = 5.0               # a host that has said nothing for this long is asked afresh


def frame(kind: bytes, payload: bytes = b"") -> bytes:
    return kind + struct.pack(">I", len(payload)) + payload


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def connect_to(path: Path):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(ANSWER_WITHIN_S)
        sock.connect(str(path))
    except OSError:
        sock.close()
        raise
    return sock


class _Host:
    """One host's socket, greeted as a reader, with what it last answered."""

    def __init__(self, sock, now: float):
        self.sock, self.inbox = sock, bytearray()
        self.asked = False          # a q is out that no Q has answered yet
        self.heard_at = now
        self.last: Shown | None = None
        sock.sendall(frame(b"h", json.dumps({"v": SPEAKS, "role": "read"}).encode()))

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def _take(self, wait: float) -> bool:
        """Reads what's there, waiting at most `wait` for it. False: the host has gone."""
        try:
            self.sock.settimeout(wait)
            data = self.sock.recv(65536)
        except (BlockingIOError, InterruptedError, TimeoutError):
            return True
        except OSError:
            return False
        if not data:
            return False
        self.inbox += data
        return True

    def _frames(self):
        while len(self.inbox) >= 5:
            size = struct.unpack(">I", self.inbox[1:5])[0]
            if size > LARGEST_ANSWER:
                raise ValueError("a frame that large isn't one of these")
            if len(self.inbox) < 5 + size:
                return
            kind, payload = bytes(self.inbox[:1]), bytes(self.inbox[5:5 + size])
            del self.inbox[:5 + size]
            yield kind, payload

    def look(self, clock, patience: float) -> bool:
        """Asks, and takes its answer if that comes within `patience`, by `clock`. One that's later counts the
        next time, which neither asks again nor waits again: a host that's slow is waited for once. False: it's
        gone, it has said nothing for too long, or it isn't a host that can be read."""
        try:
            wait = 0.0
            if not self.asked:
                self.sock.settimeout(ANSWER_WITHIN_S)
                self.sock.sendall(frame(b"q"))
                self.asked, wait = True, patience
            until = clock() + wait
            while True:
                if not self._take(max(0.0, until - clock())):
                    return False
                for kind, payload in self._frames():
                    if kind == b"Q":
                        self.last, self.asked, self.heard_at = shown_in(payload), False, clock()
                if not self.asked or clock() >= until:
                    break
        except (OSError, ValueError):
            return False
        return clock() - self.heard_at < SILENT_FOR_S


def shown_in(payload: bytes) -> Shown | None:
    """What a host answered, read loosely: anything that isn't what's expected is left out."""
    try:
        answer = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(answer, dict):
        return None

    def text(key: str, most: int) -> str:
        value = answer.get(key)
        return value[:most] if isinstance(value, str) else ""
    said = answer.get("said")
    if not (isinstance(said, dict) and said.get("state") in SAID_STATES):
        said = None
    return Shown(title=text("title", 300), text=text("text", LONGEST_SCREEN), progress=text("progress", 40), said=said)


class HostedPlace(Place):
    """Sessions that are each held by a host of their own: see the top of this file. A terminal here is known
    by its session's name. Only what's in this daemon's sessions folder is looked at, and only hosts that are
    still running."""

    def __init__(self, folder: Path | None = None, connect=connect_to, alive=process_alive, clock=time.monotonic,
                 patience: float = ANSWER_WITHIN_S):
        self.folder = Path(folder) if folder is not None else paths.sessions_dir()
        self.connect, self.alive, self.clock, self.patience = connect, alive, clock, patience
        self.records: dict[str, dict] = {}    # session -> what its host wrote down
        self.hosts: dict[str, _Host] = {}     # session -> its socket, once it's been looked at

    def _read_records(self) -> dict[str, dict]:
        out = {}
        try:
            files = list(self.folder.glob("*.json"))
        except OSError:
            return out
        for file in files:
            try:
                record = json.loads(file.read_text())
            except (OSError, ValueError):
                continue   # being written, or not one of these
            if not isinstance(record, dict):
                continue
            name, version, host = record.get("id"), record.get("v"), record.get("host")
            if not (isinstance(name, str) and SESSION_ID.fullmatch(name) and name == file.stem):
                continue
            if not (isinstance(version, int) and not isinstance(version, bool) and version >= SPEAKS):
                continue   # an older host: it doesn't say what it shows
            if not (isinstance(host, int) and not isinstance(host, bool) and host > 1 and self.alive(host)):
                continue
            out[name] = record
        return out

    def find(self, found: list[Found]) -> dict[int, object]:
        self.records = self._read_records()
        for gone in set(self.hosts) - set(self.records):
            self.hosts.pop(gone).close()
        by_tty = {}
        for name, record in self.records.items():
            tty = record.get("tty")
            if isinstance(tty, str) and tty.startswith("/dev/"):
                by_tty[tty[len("/dev/"):]] = name
        return {f.pid: by_tty[f.tty] for f in found if f.tty in by_tty}

    def knows(self, name) -> bool:
        """Whether that's a session hosted here, as of the last look."""
        return isinstance(name, str) and name in self.records

    def shown(self, held) -> Shown | None:
        if held not in self.records:
            return None
        host = self.hosts.get(held)
        if host is None:
            try:
                host = self.hosts[held] = _Host(self.connect(self.folder / f"{held}.sock"), self.clock())
            except OSError:
                self.hosts.pop(held, None)
                return None
        if not host.look(self.clock, self.patience):
            self.hosts.pop(held).close()
            return None
        return host.last
