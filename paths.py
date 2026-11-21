"""Where the daemon keeps what it keeps, and which Claude settings it changes. Every other module asks here.

TTYCAT_HOME moves the state folder: sockets, the lock, the pet, settings, downloads. Two daemons with two folders
don't meet, so a second one can run beside the one in use, for trying a change. TTYCAT_CLAUDE_SETTINGS points
`hook install|remove|repair` at another file, so that daemon can't change the hook Claude really runs.
TTYCAT_SESSIONS_DIR moves the folder hosted sessions are looked for in: see places.py.
"""
from __future__ import annotations

import os
from pathlib import Path


def state_dir(home: str | Path | None = None) -> Path:
    """TTYCAT_HOME when it names a folder by its whole path, else ~/.ttycat. `home`: whose ~, for tests."""
    chosen = os.environ.get("TTYCAT_HOME", "")
    if chosen and os.path.isabs(chosen):
        return Path(chosen)
    return Path(home if home is not None else Path.home()) / ".ttycat"


def sessions_dir() -> Path:
    """TTYCAT_SESSIONS_DIR when it names a folder by its whole path, else `sessions` in the state folder."""
    chosen = os.environ.get("TTYCAT_SESSIONS_DIR", "")
    if chosen and os.path.isabs(chosen):
        return Path(chosen)
    return state_dir() / "sessions"


def claude_settings() -> Path:
    """TTYCAT_CLAUDE_SETTINGS when it names a file by its whole path, else ~/.claude/settings.json."""
    chosen = os.environ.get("TTYCAT_CLAUDE_SETTINGS", "")
    if chosen and os.path.isabs(chosen):
        return Path(chosen)
    return Path.home() / ".claude" / "settings.json"
