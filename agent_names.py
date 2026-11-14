"""Names people give their agents, so two in the same folder can be told apart. ttycat keeps them itself, in
~/.ttycat/agent-names.json, and the same way for every agent, whatever it runs in: herdr, a terminal, tmux or the
Codex app. Nothing is told to the agent, and nothing is written to herdr.

A name is kept under what its agent is known by, which has to be something no later agent will go by too: see
Fleet.name_key in sources.py. It outlives its agent by a month, in case that session comes back. One known only by its
process is forgotten ten minutes after the process goes, because process numbers come round again."""
from __future__ import annotations

import json
import threading
import time
import unicodedata
from pathlib import Path

from discovery import PROCESS_PREFIX

NAMES_FILE = Path.home() / ".ttycat" / "agent-names.json"
NAME_MAX = 40
KEEP_S = 30 * 86400
KEEP_PROCESS_S = 600
MAX_NAMES = 500     # more than anyone has agents: a file that only ever grew would be a bug
TIDY_EVERY_S = 60
SAVE_SEEN_S = 3600  # how often "still here" is written down, when nothing else is


def clean_name(text) -> str:
    """One line of what can be shown, no longer than a name is."""
    if not isinstance(text, str):
        return ""
    return "".join(c for c in " ".join(text.split()) if c.isprintable())[:NAME_MAX].strip()


def for_board(name: str) -> str:
    """The name in what the board's fonts have, which is ASCII: an accent goes and its letter stays, and what has no
    such letter goes altogether. "" if that leaves nothing."""
    plain = unicodedata.normalize("NFKD", name)
    return " ".join("".join(c for c in plain if 32 <= ord(c) < 127).split())


class AgentNames:
    """The names, by what each agent is known by. path=None keeps them in memory only, as tests and the demo do."""

    def __init__(self, path: Path | None = None, clock=time.time):
        self.path, self.clock = path, clock
        self.lock = threading.Lock()  # machines over SSH are polled on threads of their own
        self.started = self.tidied_at = self.saved_at = clock()
        self.names: dict[str, dict] = self._load()  # known by -> {"name", "seen": when it was last listed}

    def _load(self) -> dict[str, dict]:
        if not self.path:
            return {}
        try:
            kept = json.loads(self.path.read_text())["names"]
            names = {str(key): {"name": clean_name(entry["name"]), "seen": float(entry["seen"])} for key, entry in kept.items()}
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return {}
        return {key: entry for key, entry in names.items() if entry["name"]}

    def _save(self):
        self.saved_at = self.clock()
        if not self.path:
            return
        try:
            self.path.parent.mkdir(mode=0o700, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps({"names": self.names}))
            tmp.replace(self.path)
        except OSError:
            pass

    def get(self, key: str) -> str:
        """The name kept for it, "" if there's none. Asking is what says it's still here."""
        with self.lock:
            entry = self.names.get(key)
            if not entry:
                return ""
            entry["seen"] = self.clock()
            return entry["name"]

    def set(self, key: str, name: str) -> str:
        """Keep a name for it, or with "" forget the one it had. Gives back the name as kept."""
        name = clean_name(name)
        with self.lock:
            if name:
                self.names[key] = {"name": name, "seen": self.clock()}
            else:
                self.names.pop(key, None)
            self._forget()
            self._save()
        return name

    def tidy(self):
        """Forget the names of agents long gone, and now and then write down which are still here. Cheap enough
        for every poll: it looks once a minute."""
        with self.lock:
            now = self.clock()
            if now - self.tidied_at < TIDY_EVERY_S:
                return
            self.tidied_at = now
            if self._forget() or (self.names and now - self.saved_at >= SAVE_SEEN_S):
                self._save()

    def _forget(self) -> bool:
        now = self.clock()

        def gone(key: str, entry: dict) -> bool:
            if key.startswith(PROCESS_PREFIX):
                # Since this daemon started, at least: what ended while it was down gets its ten minutes too.
                return now - max(entry["seen"], self.started) > KEEP_PROCESS_S
            return now - entry["seen"] > KEEP_S

        old = [key for key, entry in self.names.items() if gone(key, entry)]
        if len(self.names) - len(old) > MAX_NAMES:
            rest = sorted((key for key in self.names if key not in old), key=lambda key: self.names[key]["seen"])
            old += rest[:len(rest) - MAX_NAMES]
        for key in old:
            del self.names[key]
        return bool(old)
