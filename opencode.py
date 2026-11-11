"""What OpenCode 2 says about itself, asked of its own server on this Mac.

OpenCode 2 runs one local server for all its sessions and writes where it is, with the password it
wants, to a file of yours. Everything here is a GET: nothing is started, answered or changed there,
and the password only ever goes back to this Mac. A place the server hasn't opened is never asked
about, since asking would make it open it. Without that server (OpenCode 1, or none running) there is
nothing to ask, and the caller falls back on what it can see from outside.
"""
from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

SERVICE = Path.home() / ".local" / "state" / "opencode" / "service.json"
THIS_MAC = ("http://127.0.0.1:", "http://localhost:", "http://[::1]:")
USER = "opencode"
LOOK_EVERY_S = 2.0
LOOK_AGAIN_S = 30.0      # after it didn't answer: it must not be waited for with every look
TIMEOUT_S = 0.5
STAYED_S = 3.0           # a command that runs and returns is the agent working, not something left open
MOST_SESSIONS = 200      # remembered; an id is asked about once


def read_service(path: Path = SERVICE) -> tuple[str, str] | None:
    """(url, password) of the server, if it says it's on this Mac."""
    try:
        info = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict):
        return None
    url, password = info.get("url"), info.get("password") or ""
    if not isinstance(url, str) or not url.startswith(THIS_MAC) or not isinstance(password, str):
        return None
    return url.rstrip("/"), password


def fetch(url: str, password: str, path: str):
    """The `data` of a reply, or None."""
    request = urllib.request.Request(url + path)
    if password:
        request.add_header("Authorization", "Basic " + base64.b64encode(f"{USER}:{password}".encode()).decode())
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as reply:
            body = json.loads(reply.read(4_000_000))
    except (OSError, ValueError, urllib.error.URLError):
        return None
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return body


def folder(value) -> str:
    return os.path.realpath(value) if isinstance(value, str) and value else ""


@dataclass
class Seen:
    """By folder: the subagents at work there, and the shells left open by its sessions."""
    subagents: dict[str, int] = field(default_factory=dict)
    shells: dict[str, int] = field(default_factory=dict)


class Server:
    def __init__(self, service=read_service, get=fetch, clock=time.monotonic, now=time.time):
        self.service, self.get, self.clock, self.now = service, get, clock, now
        self.looked_at: float | None = None
        self.wait = LOOK_EVERY_S
        self.seen: Seen | None = None
        self.sessions: dict[str, tuple[bool, str]] = {}   # id -> (it's a subagent, its folder): neither changes

    def look(self) -> Seen | None:
        """None: there's no server to ask, or it didn't answer."""
        if self.looked_at is None or self.clock() - self.looked_at >= self.wait:
            self.looked_at = self.clock()
            self.seen = self._ask()
            self.wait = LOOK_EVERY_S if self.seen else LOOK_AGAIN_S
        return self.seen

    def _ask(self) -> Seen | None:
        found = self.service()
        if not found:
            return None
        url, password = found
        active = self.get(url, password, "/api/session/active")
        opened = self.get(url, password, "/api/debug/location")
        if not isinstance(active, dict) or not isinstance(opened, list):
            return None
        seen = Seen()
        if len(self.sessions) > MOST_SESSIONS:
            self.sessions = {sid: known for sid, known in self.sessions.items() if sid in active}
        for sid in active:
            if sid not in self.sessions:
                info = self.get(url, password, "/api/session/" + urllib.parse.quote(str(sid), safe=""))
                if not isinstance(info, dict):
                    continue
                where = info.get("location")
                self.sessions[sid] = (bool(info.get("parentID")), folder(where.get("directory") if isinstance(where, dict) else ""))
            subagent, place = self.sessions[sid]
            if subagent and place:
                seen.subagents[place] = seen.subagents.get(place, 0) + 1
        for place in opened:
            directory = place.get("directory") if isinstance(place, dict) else None
            if not isinstance(directory, str) or not directory:
                continue
            shells = self.get(url, password, "/api/shell?" + urllib.parse.urlencode({"location[directory]": directory}))
            if isinstance(shells, list):
                seen.shells[folder(directory)] = sum(1 for shell in shells if self._left_open(shell))
        return seen

    def _left_open(self, shell) -> bool:
        """Running, started by a session and not just now."""
        if not isinstance(shell, dict) or shell.get("status") != "running":
            return False
        about, when = shell.get("metadata"), shell.get("time")
        if not isinstance(about, dict) or not about.get("sessionID"):
            return False  # one you started yourself
        started = when.get("started") if isinstance(when, dict) else None
        return isinstance(started, (int, float)) and self.now() - started / 1000 >= STAYED_S
