"""Where an agent's terminal is, when that's somewhere its screen can be read.

Most terminals show their screens to nobody but you: an agent in one is known to be running, and no more.
A place is somewhere that will say what a terminal in it shows, so that what an agent there is doing can be
read off its screen, by the same rules for every place (screen_rules.py). tmux is one: it's asked by its
command line, on each of your servers.

A place is only ever read, and asked to bring a terminal forward. Nothing is typed into one.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from discovery import Found, place_in_tmux, tmux_panes, tmux_place


@dataclass
class Shown:
    """What a terminal shows now: its title, and its screen as text."""
    title: str = ""
    text: str = ""


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
