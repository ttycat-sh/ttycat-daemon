"""Which device on the USB port may approve prompts.

Whatever's on the port can send `allow`, and anyone can build one now that the firmware is open.
A device that has no way to show the whole prompt shouldn't be able to say yes to it, so only
ttycat's own firmware may approve without being asked about. Any other device can show what's
going on, open a session and Deny, because those can only stop an agent. Approving needs the
person to turn it on, for that device, in Setup.

A device is known by the name it gives in `hello`. Only a release build gives a plain version
number (FW_NAME in firmware/main/model.h). Anything built from source gives a name that comes from
its source, so it counts as someone's own, and each change gets asked about again.

This guards against accidents, such as a lamp with a button or an agent's overeager firmware. It
can't stop a device built to lie about its name. Anything running as you can already answer
prompts: see the README.
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


LABEL_BYTES = 32  # as the board keeps it: link.c


def clean_label(name) -> str:
    """A name someone gave their board, as the board can keep it: printable, without quotes or
    backslashes, and up to 32 bytes. "" if nothing's left."""
    if not isinstance(name, str):
        return ""
    kept = "".join(c for c in name if c.isprintable() and c not in '"\\').strip()
    while len(kept.encode()) > LABEL_BYTES:
        kept = kept[:-1]
    return kept.strip()


def is_custom(fw: str | None) -> bool:
    """It said what it runs, and that isn't ttycat's firmware."""
    return fw is not None and not VERSION.fullmatch(fw)


class Devices:
    def __init__(self, path: Path | None = None):
        self.path = path or DEVICES_FILE

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _approving(self) -> list[str]:
        """Anything unreadable means nobody's been allowed."""
        names = self._read().get("approve", [])
        return [n for n in names if isinstance(n, str)] if isinstance(names, list) else []

    def label(self, key: str | None) -> str | None:
        """The name kept on this Mac for a board: by its chip's ID, or by its model if it doesn't say one."""
        names = self._read().get("names", {})
        name = names.get(key) if isinstance(names, dict) and key else None
        return clean_label(name) or None

    def set_label(self, key: str, name: str):
        data = self._read()
        names = data.get("names") if isinstance(data.get("names"), dict) else {}
        if name:
            names[key] = name
        else:
            names.pop(key, None)
        self._write({**data, "names": names})

    def may_approve(self, fw: str | None) -> bool:
        if fw is None:
            return False  # it hasn't said what it is
        return not is_custom(fw) or fw in self._approving()

    def permits(self, action: str, fw: str | None) -> bool:
        return action not in APPROVING or self.may_approve(fw)

    def set_approve(self, fw: str, on: bool):
        names = [n for n in self._approving() if n != fw] + ([fw] if on else [])
        self._write({**self._read(), "approve": names})

    def _write(self, data: dict):
        self.path.parent.mkdir(mode=0o700, exist_ok=True)
        temp = self.path.with_suffix(".tmp")
        temp.write_text(json.dumps(data))
        os.chmod(temp, 0o600)
        os.replace(temp, self.path)
