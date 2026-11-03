"""How Bit and the board look: a number for each, into the tables in firmware/main/model.h and the
app's Look.swift. 0 is the default for both."""
from __future__ import annotations

import json
import os
from pathlib import Path

LOOK_FILE = Path.home() / ".ttycat" / "look.json"
SKINS = 6
ACCENTS = 5


def clean(value, count: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value < count else 0


class Look:
    def __init__(self, path: Path | None = LOOK_FILE):
        self.path = path
        self.skin = self.accent = 0
        try:
            saved = json.loads(path.read_text()) if path else {}
        except (OSError, ValueError):
            saved = {}
        if isinstance(saved, dict):
            self.skin, self.accent = clean(saved.get("skin"), SKINS), clean(saved.get("accent"), ACCENTS)

    def set(self, skin, accent) -> dict:
        self.skin, self.accent = clean(skin, SKINS), clean(accent, ACCENTS)
        if self.path:
            tmp = self.path.with_suffix(".tmp")
            tmp.parent.mkdir(mode=0o700, exist_ok=True)
            tmp.write_text(json.dumps(self.info()))
            os.replace(tmp, self.path)
        return self.info()

    def info(self) -> dict:
        return {"skin": self.skin, "accent": self.accent}
