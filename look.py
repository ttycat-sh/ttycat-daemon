"""How Bit and the board look: a number for each, into the tables below. 0 is the default for both.
The firmware (main/gfx.c in ttycat-firmware) and the app (Sources/TTYCat/Look.swift in ttycat-app) have
the same tables, in the same order: each one's CI checks itself against these."""
from __future__ import annotations

import json
import os
from pathlib import Path

LOOK_FILE = Path.home() / ".ttycat" / "look.json"
# (name, body, shade)
SKIN_COLOURS = [("Ember", 0xFF6A13, 0xB84A0C), ("Mint", 0x3FD38A, 0x1F8F58), ("Ice", 0x5AB8FF, 0x2E78B8),
                ("Grape", 0xB07CFF, 0x7443C4), ("Rose", 0xFF6FA8, 0xB83E72), ("Mono", 0xD8D8D8, 0x8A8A8A)]
# (name, value)
ACCENT_COLOURS = [("Orange", 0xFF6A13), ("Teal", 0x2DD4BF), ("Blue", 0x5AB8FF), ("Violet", 0xB07CFF),
                  ("Yellow", 0xFFC83D)]
SKINS = len(SKIN_COLOURS)
ACCENTS = len(ACCENT_COLOURS)


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
