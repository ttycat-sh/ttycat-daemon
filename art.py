"""Art for Bit's stage on the board: docs/PROTOCOL.md, "Art". The board checks everything again and draws
only what it can, so this only keeps out what no board would take."""
from __future__ import annotations

import json
import re

LINE_MAX = 8192  # ART_LINE_MAX in firmware/main/art.h
ID = re.compile(r"[A-Za-z0-9-]{1,11}")


def check(msg) -> str:
    """Why this can't be sent to a board, or "" if it can."""
    if not isinstance(msg, dict) or msg.get("t") != "art":
        return 'not an art message: it needs "t": "art"'
    if not isinstance(msg.get("id"), str) or not ID.fullmatch(msg["id"]):
        return "its id has to be 1 to 11 letters, digits or dashes"
    f = msg.get("f")
    if not isinstance(f, int) or isinstance(f, bool) or f < 1:
        return 'it needs "f", the art format it uses'
    if len(encode(msg)) > LINE_MAX:
        return f"it's over the {LINE_MAX // 1024} KB a board takes"
    return ""


def encode(msg: dict) -> bytes:
    return json.dumps(msg, separators=(",", ":"), ensure_ascii=True).encode()


def drawable(msg: dict | None, board_format) -> bool:
    """Whether a board that said it draws `board_format` can draw this. One that didn't say draws none."""
    return bool(msg) and isinstance(board_format, int) and not isinstance(board_format, bool) and board_format >= msg["f"]
