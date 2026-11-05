#!/usr/bin/env python3
"""ttycat daemon: connects herdr to the touch board over USB.

  ttycatd.py run                 live sessions from this Mac
  ttycatd.py demo                the design's sample data (nothing is answered)
  ttycatd.py hook install|remove|status   the Claude Code permission hook
  ttycatd.py pet [--name NAME]   see the pet, or rename it
  ttycatd.py shot out.png        save the board's current frame
  ttycatd.py tap X Y | swipe L|R|U|D | goto SCREEN     drive a running daemon
  ttycatd.py scene NAME | silence SECONDS              demo states, for testing

Standard library only. Serial protocol: one JSON object per line, both ways.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import glob
import json
import os
import re
import select
import shlex
import signal
import socket
import struct
import sys
import termios
import time
import zlib
from pathlib import Path

import plugins
from approvals import HOOK_SOCK, Hub, HubScope
from claude_sessions import ClaudeSessions, RunningAgents, is_session_pane
from codex_threads import CodexThreads
from flasher import Flasher
from look import Look
from devices import NOT_ALLOWED, Devices, clean_name, is_custom
from petstate import PET_FILE, Pet
import token_days
from sources import DemoSource, Fleet, HerdrSource

CTL_PATH = Path.home() / ".ttycat" / "ctl.sock"
LOCK_PATH = Path.home() / ".ttycat" / "daemon.lock"
ALREADY_RUNNING = 3  # exit status the Mac app looks for
# All the app may do. An answer needs the exact prompt's rev: see approvals.Hub.answer.
APP_ACTIONS = {"open", "seen", "allow", "answer", "deny",
               "pet-feed", "pet-clean", "pet-sleep", "pet-pat", "pet-play", "pet-here"}
APP_ACTIVITY_ROWS = 30
MAX_CTL_REQUEST = 256 * 1024
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"
HOOK_SCRIPT = Path(__file__).resolve().parent / "hook.py"
HOOK_TIMEOUT_S = 86400  # the terminal prompt stays usable meanwhile: see approvals.py
HEARTBEAT_S = 1.0
HELLO_WAIT_S = 6.0  # a board says what it runs after 2 s without hearing anything, and booting takes a moment
PROTOCOL = 1  # what a device has to speak. Goes up when a change would break one that's already built
SERIAL_PORTS = ("/dev/cu.usbmodem*", "/dev/cu.usbserial*", "/dev/cu.wchusbserial*", "/dev/cu.SLAB_USBtoUART*")


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def serial_ports() -> list[str]:
    """Native USB first. Which of these is a board only comes out when one says hello."""
    return [path for pattern in SERIAL_PORTS for path in sorted(glob.glob(pattern))]


class Board:
    """The serial port, opened raw. Reopens when the board comes back.

    Anything can be on a serial port, and opening one can restart what's on it. So each port gets
    one try and nothing is sent to it until it says hello. One that stays silent is left alone
    until it's unplugged or something's written to it. It might still need firmware, so Setup
    can offer that.
    """

    def __init__(self, port: str | None):
        self.want = port
        self.path: str | None = None
        self.fw: str | None = None
        self.name: str | None = None  # which folder in firmware/boards it was built from
        self.protocol = None
        self.silent: set[str] = set()
        self.asks = 0  # times it's asked for a prompt to be shown on the Mac
        self.asked: str | None = None
        self.fd: int | None = None
        self.buf = b""
        self.out = b""
        self.opened_at = 0.0
        self.dim = False  # its screen has gone dim, and Bit dozes there

    def next_port(self) -> str | None:
        if self.want:
            return self.want if os.path.exists(self.want) else None
        ports = serial_ports()
        self.silent &= set(ports)  # unplugged: worth another try when it comes back
        return next((path for path in ports if path not in self.silent), None)

    def give_up_if_silent(self, now: float):
        if self.fd is None or self.fw is not None or now - self.opened_at < HELLO_WAIT_S:
            return
        self.silent.add(self.path)
        if not self.want:
            log("nothing said hello on", self.path)
            self.close()

    def said_hello(self, m: dict):
        self.fw = clean_name(m.get("fw")) or self.fw
        self.name = clean_name(m.get("b"))
        self.protocol = m.get("p")
        self.silent.discard(self.path)

    @property
    def speaks(self) -> bool:
        return self.fw is not None and self.protocol == PROTOCOL

    def open(self) -> bool:
        path = self.next_port()
        if not path:
            return False
        try:
            fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            attrs = termios.tcgetattr(fd)
            attrs[0] = attrs[1] = attrs[3] = 0
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
            attrs[4] = attrs[5] = termios.B230400  # a USB-to-serial chip needs this; native USB ignores it
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except OSError as e:
            log("cannot open", path, e)
            return False
        self.fd, self.buf, self.out = fd, b"", b""
        self.path, self.fw, self.name, self.protocol = path, None, None, None
        self.opened_at = time.monotonic()
        self.dim = False
        log("board on", path)
        return True

    def close(self):
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            log("board gone")
        self.fd = None

    def send(self, obj: dict):
        if self.fd is None:
            return
        self.out += json.dumps(obj, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"
        self.flush()

    def flush(self):
        # A line the board has no room for yet stays queued. A stuck board must not
        # stall the daemon, so a backlog of stale state is simply dropped.
        if len(self.out) > 256 * 1024:
            self.out = b""
        while self.out and self.fd is not None:
            try:
                n = os.write(self.fd, self.out[:512])
            except BlockingIOError:
                return
            except OSError:
                self.close()
                return
            self.out = self.out[n:]

    def read_lines(self) -> list[dict]:
        try:
            data = os.read(self.fd, 65536)
        except BlockingIOError:
            return []
        except OSError:
            self.close()
            return []
        if not data:
            self.close()
            return []
        self.buf += data
        *lines, self.buf = self.buf.split(b"\n")
        msgs = []
        for raw in lines:
            raw = raw.strip()
            if raw.startswith(b"{"):
                try:
                    msgs.append(json.loads(raw))
                    continue
                except ValueError:
                    pass
            if raw:
                log("board:", raw.decode(errors="replace")[:160])
        return msgs


def rgb565_to_png(path: str, frame: bytes, w=240, h=320):
    rows = bytearray()
    for y in range(h):
        rows.append(0)
        for x in range(w):
            hi, lo = frame[(y * w + x) * 2], frame[(y * w + x) * 2 + 1]
            v = hi << 8 | lo  # the framebuffer is big-endian RGB565
            r, g, b = v >> 11, v >> 5 & 63, v & 31
            rows += bytes((r << 3 | r >> 2, g << 2 | g >> 4, b << 3 | b >> 2))

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data))

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(bytes(rows))))
        f.write(chunk(b"IEND", b""))


def serve(source, port: str | None, hubs: list[Hub] = (), api: plugins.Api | None = None):
    api = api or plugins.Api()
    look = Look()
    devices = Devices()
    board = Board(port)
    flasher = Flasher(log)
    CTL_PATH.parent.mkdir(mode=0o700, exist_ok=True)
    CTL_PATH.unlink(missing_ok=True)
    ctl = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    ctl.bind(str(CTL_PATH))
    os.chmod(CTL_PATH, 0o600)
    ctl.listen(4)
    ctl.setblocking(False)

    next_beat = 0.0
    silent_until = 0.0
    watching: str | None = None
    shot_client: socket.socket | None = None
    shot_parts: dict[int, str] = {}

    def state():
        return state_message(source, watching, look, devices.may_approve(board.fw))

    try:
        while True:
            for hub in hubs:
                hub.settle()
            if board.fd is None and (flasher.holds_port or not board.open()):
                source.poll()  # keep watching: histories and ages stay right
            board.give_up_if_silent(time.monotonic())
            readers = [ctl, *(sock for hub in hubs for sock in hub.fds())] + ([board.fd] if board.fd is not None else [])
            if flasher.fd is not None:
                readers.append(flasher.fd)
            r, w, _ = select.select(readers, [board.fd] if board.fd is not None and board.out else [], [],
                                    0.1 if board.fd is not None or flasher.busy else 1)
            if flasher.fd is not None and flasher.fd in r:
                flasher.on_output()
            flasher.tick()
            for sock in r:
                if sock is ctl or sock == board.fd or sock == flasher.fd:
                    continue
                for hub in hubs:
                    if hub.owns(sock):
                        hub.handle(sock)
                        source.last_poll = next_beat = 0  # show a new ask right away
                        break
            if board.fd is not None and board.fd in w:
                board.flush()
            if board.fd is not None and board.fd in r:
                for m in board.read_lines():
                    t = m.get("t")
                    if t == "shot":
                        shot_parts[m["i"]] = m["d"]
                        if len(shot_parts) == m["n"] and shot_client:
                            frame = b"".join(base64.b64decode(shot_parts[i]) for i in range(m["n"]))
                            shot_client.sendall(frame)
                            shot_client.close()
                            shot_client, shot_parts = None, {}
                    elif t == "hello" and time.monotonic() >= silent_until:
                        board.said_hello(m)
                        board.send({"t": "hello", "p": PROTOCOL})
                        flasher.board_said(board.fw)
                        next_beat = 0
                        if not board.speaks:
                            log("firmware", board.fw, "speaks protocol", board.protocol, "and this speaks", PROTOCOL)
                    elif not board.speaks:
                        pass  # it hasn't said who it is, or it wouldn't understand
                    elif t == "dim":
                        board.dim = m.get("on") is True
                    elif t == "touch":
                        log("touch", m.get("x"), m.get("y"))
                        source.act("pet-here", "", "")
                    elif t == "watch":
                        watching = m.get("id")
                        next_beat = 0
                    elif t == "act":
                        if m.get("a") == "ask-mac":
                            board.asks, board.asked = board.asks + 1, m.get("id")
                            ok, msg = True, ""
                        else:
                            ok, msg = device_action(source, board.fw, devices, m)
                        log("action", m.get("a"), m.get("id"), "ok" if ok else f"refused: {msg}")
                        board.send(state())
                        board.send({"t": "ack", "ok": ok, "msg": msg})
                        next_beat = time.monotonic() + HEARTBEAT_S
            if ctl in r:
                conn, _ = ctl.accept()
                conn.settimeout(2)
                try:
                    cmd = read_request(conn)
                except (ValueError, OSError):
                    conn.close()
                    continue
                if cmd.get("t") in api.ctl:
                    try:
                        reply = api.ctl[cmd["t"]](cmd)
                    except Exception as e:  # noqa: BLE001  an extension's bug mustn't stop the board
                        log("plugin", cmd.get("t"), repr(e))
                        reply = {"ok": False, "msg": "that didn't work"}
                    try:
                        conn.sendall(json.dumps(reply, separators=(",", ":")).encode())
                    except OSError:
                        pass
                    conn.close()
                    continue
                if cmd.get("t") in ("status", "app-act", "activity", "flash", "look", "device"):  # the Mac app
                    if cmd["t"] == "status":
                        reply = status_message(source, board, flasher, look, devices)
                    elif cmd["t"] == "look":
                        reply = look.set(cmd.get("skin"), cmd.get("accent"))
                        next_beat = 0
                    elif cmd["t"] == "device":
                        reply = device_setting(board, devices, cmd)
                    elif cmd["t"] == "flash":
                        reply = start_flash(board, flasher, cmd)
                    elif cmd["t"] == "activity":
                        reply = {"items": source.history(str(cmd.get("id", "")), APP_ACTIVITY_ROWS, False)}
                    else:
                        reply = app_action(source, cmd)
                        next_beat = 0  # the board shows it right away too
                    try:
                        conn.sendall(json.dumps(reply, separators=(",", ":")).encode())
                    except OSError:
                        pass
                    conn.close()
                    continue
                if cmd.get("t") == "scene":
                    if hasattr(source, "scene"):
                        source.scene(cmd.get("name"))
                    next_beat = 0
                    conn.close()
                    continue
                if cmd.get("t") == "pet-name":
                    if hasattr(source, "rename_pet"):
                        source.rename_pet(str(cmd.get("name", "")))
                    next_beat = 0
                    conn.close()
                    continue
                if cmd.get("t") == "silence":  # the link-lost screens, for testing
                    silent_until = next_beat = time.monotonic() + float(cmd.get("s", 7))
                    conn.close()
                    continue
                board.send(cmd)
                if cmd.get("t") == "shot" and board.fd is not None:
                    shot_client, shot_parts = conn, {}
                    conn.settimeout(30)
                else:
                    conn.close()
            if board.fd is not None and board.speaks and time.monotonic() >= next_beat:
                board.send(state())
                next_beat = time.monotonic() + HEARTBEAT_S
    finally:
        flasher.close()
        CTL_PATH.unlink(missing_ok=True)
        for hub in hubs:
            hub.close()


def read_request(conn: socket.socket) -> dict:
    """One JSON object. A typed answer can take more than one read."""
    data = b""
    while len(data) <= MAX_CTL_REQUEST:
        part = conn.recv(65536)
        if not part:
            break
        data += part
        try:
            cmd = json.loads(data)
        except ValueError:
            continue
        if isinstance(cmd, dict):
            return cmd
        break
    raise ValueError("not a request")


def state_message(source, watching: str | None, look: Look | None = None, may_approve: bool = True) -> dict:
    now = time.time()
    offset = time.localtime(now).tm_gmtoff or 0
    msg = {"t": "state", "now": int(now) + offset, "s": source.poll(), "pet": source.pet()}
    if look and (look.skin or look.accent):
        msg["look"] = [look.skin, look.accent]
    if not may_approve:
        msg["na"] = True  # so it can say so, instead of offering a button that won't work
    if watching:
        msg["hist"] = {"id": watching, "items": source.history(watching)}
    return msg


def status_message(source, board: Board, flasher: Flasher | None = None, look: Look | None = None,
                   devices: Devices | None = None) -> dict:
    """What the Mac app shows. Reading it changes nothing."""
    msg = state_message(source, None, look)
    msg.pop("look", None)
    if look:
        msg["look"] = look.info()
    desk = source.desk() if hasattr(source, "desk") else {}
    msg["s"] = [{**row, **desk.get(row["id"], {})} for row in msg["s"]]
    msg.update(t="status", pid=os.getpid(), term=getattr(source, "focus_app", None),
               board={"connected": board.fd is not None and board.fw is not None, "port": board.path, "fw": board.fw,
                      "dim": board.dim, "approves": (devices or Devices()).may_approve(board.fw), "name": board.name,
                      "outdated": board.fw is not None and not board.speaks, "silent": sorted(board.silent),
                      "probing": board.fd is not None and board.fw is None and board.path not in board.silent,
                      "asks": board.asks, "asked": board.asked},
               machines=[{"host": r.host, "up": r.reachable and not r.stale, "answers": bool(getattr(r, "tunnel", None) and r.tunnel.up)}
                         for r in getattr(source, "remotes", ())])
    if flasher:
        msg["flash"] = flasher.info()
    return msg


def device_action(source, fw: str | None, devices: Devices, m: dict) -> tuple[bool, str]:
    """What the device on the USB port asked for. Only one that's allowed to can approve: see devices.py."""
    action = m.get("a", "")
    if not devices.permits(action, fw):
        return False, NOT_ALLOWED
    return source.act(action, m.get("id", ""), m.get("rev", ""), m.get("ans"))


def start_flash(board: Board, flasher: Flasher, cmd: dict) -> dict:
    """A named port is one that's stayed silent: the person was told which one would be overwritten."""
    connected = board.fd is not None and board.fw is not None
    port, fw, name = (board.path, board.fw, board.name) if connected else (None, None, None)
    asked = cmd.get("port")
    if asked and asked != port:
        if connected:
            return {"ok": False, "msg": f"{board.fw} is connected: unplug it first"}
        if asked not in board.silent and asked not in serial_ports():
            return {"ok": False, "msg": f"no such port: {asked}"}
        port = asked
    ok, msg = flasher.start(str(cmd.get("dir", "")), port, fw, cmd.get("unknown") is True, name)
    if ok:
        board.silent.clear()  # whatever comes back deserves a new try
        if board.fd is not None:
            board.close()  # esptool needs the port to itself
    return {"ok": ok, "msg": msg}


def device_setting(board: Board, devices: Devices, cmd: dict) -> dict:
    """The person's choice in Setup, for the device that's plugged in now and no other."""
    fw = cmd.get("fw")
    if board.fd is None or fw != board.fw or not is_custom(fw):
        return {"ok": False, "msg": "that device isn't connected"}
    devices.set_approve(fw, cmd.get("approve") is True)
    return {"ok": True, "msg": ""}


def app_action(source, cmd: dict) -> dict:
    action = str(cmd.get("a", ""))
    if action not in APP_ACTIONS:
        return {"ok": False, "msg": "not available from the app"}
    ok, msg = source.act(action, str(cmd.get("id", "")), str(cmd.get("rev", "")), cmd.get("ans"), via="app")
    log("app action", action, cmd.get("id"), "ok" if ok else f"refused: {msg}")
    return {"ok": ok, "msg": msg}


def only_instance(path: Path = LOCK_PATH):
    """Two daemons would fight over the serial port and the sockets. Held until exit."""
    path.parent.mkdir(mode=0o700, exist_ok=True)
    lock = open(path, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another ttycat daemon is already running", file=sys.stderr, flush=True)
        sys.exit(ALREADY_RUNNING)
    return lock


def ctl_send(cmd: dict, want_reply=False) -> bytes:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(str(CTL_PATH))
    except OSError:
        sys.exit("the daemon isn't running (start: ttycatd.py run)")
    s.sendall(json.dumps(cmd).encode())
    if not want_reply:
        s.close()
        return b""
    s.settimeout(30)
    data = bytearray()
    while len(data) < 240 * 320 * 2:
        part = s.recv(65536)
        if not part:
            break
        data += part
    s.close()
    return bytes(data)


# Wherever it's run from: this repo, or inside the app, wherever the app has been put.
OUR_HOOK = re.compile(r"(?:^|[\s/'\"])daemon/hook\.py['\"]?\s*$")


def _is_ours(entry: dict) -> bool:
    return any(OUR_HOOK.search(str(h.get("command", ""))) for h in entry.get("hooks", []))


def hook_shell_command() -> str:
    """The app's own Python when it's in the app: there may be no other. In the repo, python3."""
    python = sys.executable if ".app/Contents/Resources/python/" in sys.executable else "python3"
    return f"{shlex.quote(python)} -I {shlex.quote(str(HOOK_SCRIPT))}"


def _files_exist(command: str) -> bool:
    """Whether what the command names is still there. A bare `python3` is the shell's to find."""
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    return all(os.path.exists(word) for word in words if word.startswith("/"))


def hook_command(action: str, path: Path = CLAUDE_SETTINGS) -> str:
    """Adds or removes our one PermissionRequest entry. Other settings stay as they are."""
    text = path.read_text() if path.exists() else "{}"
    settings = json.loads(text)  # a broken file raises: never overwrite what can't be read
    hooks = settings.setdefault("hooks", {})
    entries = hooks.get("PermissionRequest", [])
    ours = [e for e in entries if _is_ours(e)]
    if action == "status":
        return "installed" if ours else "not installed"
    if action == "repair":
        # The app was moved or replaced: only ever fixes an entry you already chose to have.
        commands = [str(h.get("command", "")) for e in ours for h in e.get("hooks", [])]
        if not ours or all(_files_exist(c) for c in commands):
            return "installed" if ours else "not installed"
        action = "install"
    others = [e for e in entries if not _is_ours(e)]
    if action == "install":
        others.append({"matcher": "*", "hooks": [{"type": "command", "command": hook_shell_command(),
                                                  "timeout": HOOK_TIMEOUT_S}]})
    if others:
        hooks["PermissionRequest"] = others
    else:
        hooks.pop("PermissionRequest", None)
        if not hooks:
            settings.pop("hooks")
    if path.exists():
        path.with_name(path.name + ".ttycat-backup").write_text(text)
    tmp = path.with_name(path.name + ".ttycat-tmp")
    tmp.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(path)
    return "installed" if action == "install" else "removed"


def main():
    early = argparse.ArgumentParser(add_help=False)
    early.add_argument("--plugin")
    api = plugins.load(early.parse_known_args()[0].plugin, log)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port (default: the first one that says hello)")
    ap.add_argument("--plugin", help="a folder holding ttycat_plugin.py: see plugins.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run")
    run.add_argument("--focus-app", help="app to bring forward for Open on Mac (default: the terminal herdr is in)")
    run.add_argument("--only-pane", action="append", default=None,
                     help="answer prompts only in this pane (repeatable); for testing")
    for configure in api.run_arguments:
        configure(run)
    for name, (configure, _) in api.commands.items():
        configure(sub.add_parser(name))
    sub.add_parser("demo")
    s = sub.add_parser("hook")
    s.add_argument("action", choices=["install", "remove", "status", "repair"])
    s = sub.add_parser("pet")
    s.add_argument("--name")
    s = sub.add_parser("flash", help="put a build on the board, through the running daemon")
    s.add_argument("folder", help="from tools/firmware_bundle.py")
    s.add_argument("--replace", action="store_true", help="overwrite custom firmware, or a port that stays silent")
    s = sub.add_parser("shot")
    s.add_argument("path")
    s = sub.add_parser("tap")
    s.add_argument("x", type=int)
    s.add_argument("y", type=int)
    s = sub.add_parser("swipe")
    s.add_argument("dir", choices=list("LRUD"))
    s = sub.add_parser("goto")
    s.add_argument("screen")
    s = sub.add_parser("scene")
    s.add_argument("name")
    s = sub.add_parser("silence")
    s.add_argument("seconds", type=float)
    a = ap.parse_args()

    if a.cmd in ("run", "demo"):
        lock = only_instance()  # noqa: F841  (the lock lasts as long as this reference)
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # so the sockets get cleaned up
    if a.cmd == "run":
        try:
            hub = Hub(HOOK_SOCK)
            pet = Pet()
            token_days.watch(pet, log)
            remotes = api.machines(a, pet, a.focus_app) if api.machines else []
            in_herdr = HubScope(hub, lambda pane: not is_session_pane(pane))
            local = HerdrSource(focus_app=a.focus_app, only_panes=a.only_pane, hub=in_herdr, pet=pet)
            sessions = ClaudeSessions(focus_app=a.focus_app, hub=HubScope(hub, is_session_pane), pet=pet)
            try:
                running = RunningAgents(focus_app=a.focus_app, pet=pet)
                threads = CodexThreads(focus_app=a.focus_app, pet=pet)
                serve(Fleet(local, remotes, sessions=sessions, running=running, threads=threads), a.port,
                      [hub, *(r.hub for r in remotes)], api)
            finally:
                for r in remotes:
                    r.stop()
        except KeyboardInterrupt:
            pass
    elif a.cmd == "demo":
        try:
            serve(DemoSource(), a.port)
        except KeyboardInterrupt:
            pass
    elif a.cmd == "pet":
        if a.name:
            ctl_send({"t": "pet-name", "name": a.name})
        else:
            print(json.dumps(json.loads(PET_FILE.read_text()), indent=2) if PET_FILE.exists() else "No pet yet.")
    elif a.cmd == "hook":
        print("Permission hook:", hook_command(a.action))
    elif a.cmd in api.commands:
        api.commands[a.cmd][1](a)
    elif a.cmd == "flash":
        request = {"t": "flash", "dir": os.path.abspath(a.folder), "unknown": a.replace}
        if a.port:
            request["port"] = a.port
        reply = json.loads(ctl_send(request, want_reply=True))
        if not reply["ok"]:
            sys.exit(reply["msg"])
        while True:
            time.sleep(1)
            flash = json.loads(ctl_send({"t": "status"}, want_reply=True))["flash"]
            print(flash["state"], f'{flash["pct"]}%' if flash["state"] == "flashing" else "", flush=True)
            if flash["state"] == "failed":
                sys.exit(flash["msg"])
            if flash["state"] == "done":
                break
    elif a.cmd == "shot":
        frame = ctl_send({"t": "shot"}, want_reply=True)
        if len(frame) != 240 * 320 * 2:
            sys.exit(f"incomplete frame: {len(frame)} bytes")
        rgb565_to_png(a.path, frame)
    elif a.cmd == "tap":
        ctl_send({"t": "tap", "x": a.x, "y": a.y})
    elif a.cmd == "swipe":
        ctl_send({"t": "swipe", "dir": a.dir})
    elif a.cmd == "goto":
        ctl_send({"t": "goto", "screen": a.screen})
    elif a.cmd == "scene":
        ctl_send({"t": "scene", "name": a.name})
    elif a.cmd == "silence":
        ctl_send({"t": "silence", "s": a.seconds})


if __name__ == "__main__":
    main()
