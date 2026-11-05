"""Which device on the USB port may approve prompts.

Whatever's on the port can send `allow`, and anyone can build one now that the firmware is open.
A device that has no way to show the whole prompt shouldn't be able to say yes to it, so only
ttycat's own firmware may approve without being asked about. Any other device can show what's
going on, open a session and Deny, because those can only stop an agent. Approving needs the
person to turn it on, for that device, in Setup.

A device is known by the name it gives in `hello`. ttycat's firmware gives a plain version number;
anything else is someone's own. New firmware under a new name gets asked about again.

This guards against accidents, such as a lamp with a button or an agent's overeager firmware. It
can't stop a device built to lie about its name, and a build that keeps the official version
number counts as official. Anything running as you can already answer prompts: see the README.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

DEVICES_FILE = Path.home() / ".ttycat" / "devices.json"
APPROVING = {"allow", "answer"}  # an answer to a question can be permission too
NOT_ALLOWED = "Can't approve from here: see Setup on the Mac"
MAX_NAME = 48
VERSION = re.compile(r"[0-9]+(\.[0-9]+)*")


def clean_name(fw) -> str | None:
    """What a device calls itself, safe to keep and show. Nil if it's not a usable name."""
    if not isinstance(fw, str):
        return None
    return "".join(c for c in fw if c.isprintable()).strip()[:MAX_NAME] or None


def is_custom(fw: str | None) -> bool:
    """It said what it runs, and that isn't ttycat's firmware."""
    return fw is not None and not VERSION.fullmatch(fw)


class Devices:
    def __init__(self, path: Path | None = None):
        self.path = path or DEVICES_FILE

    def _approving(self) -> list[str]:
        """Anything unreadable means nobody's been allowed."""
        try:
            names = json.loads(self.path.read_text()).get("approve", [])
        except (OSError, ValueError, AttributeError):
            return []
        return [n for n in names if isinstance(n, str)] if isinstance(names, list) else []

    def may_approve(self, fw: str | None) -> bool:
        if fw is None:
            return False  # it hasn't said what it is
        return not is_custom(fw) or fw in self._approving()

    def permits(self, action: str, fw: str | None) -> bool:
        return action not in APPROVING or self.may_approve(fw)

    def set_approve(self, fw: str, on: bool):
        names = [n for n in self._approving() if n != fw] + ([fw] if on else [])
        self.path.parent.mkdir(mode=0o700, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps({"approve": names}))
        os.chmod(temp, 0o600)
        os.replace(temp, self.path)
