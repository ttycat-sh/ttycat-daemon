"""How many tokens agents used each day on this Mac. It's all Bit's growth needs from their logs.

Counts what went in and came out. Cache reads don't count: they mostly measure context being
read again, and would dwarf the rest. Read-only towards the agents. Their logs are internal
formats that can change, so this reads a few fields and skips whatever it doesn't recognise: the
worst case is that Bit grows on time alone.

All it keeps is ~/.ttycat/token-days.json: a number per day, and where it got to in each log.
"""
from __future__ import annotations

import glob
import json
import os
import sqlite3
import threading
import time
from datetime import datetime

CACHE_VERSION = 1
LONGEST_LINE = 8_000_000  # bigger ones are pasted images, not usage
KEEP_REPLIES_DAYS = 14    # long enough to notice a resumed session copying old replies
SCAN_EVERY_S = 60


def _when(text) -> float:
    if isinstance(text, (int, float)):
        return float(text) / (1000.0 if text > 1e11 else 1.0)
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _int(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


class TokenDays:
    """`since` is the first day that matters, so logs older than that are never read."""

    def __init__(self, since: str, home: str | None = None, path: str | None = None):
        self.home = home or os.path.expanduser("~")
        self.path = path if path is not None else os.path.join(self.home, ".ttycat", "token-days.json")
        self.since = since
        self.since_ts = time.mktime(time.strptime(since, "%Y-%m-%d"))
        self.c = {"v": CACHE_VERSION, "files": {}, "replies": {}, "days": {}}
        try:
            with open(self.path) as f:
                saved = json.load(f)
            if saved.get("v") == CACHE_VERSION:
                self.c.update(saved)
        except (OSError, ValueError, AttributeError):
            pass

    def scan(self, now: float | None = None) -> dict:
        """{day: tokens}. A day's count never falls, so deleting old logs can't shrink it."""
        now = time.time() if now is None else now
        for read in (self._claude, self._codex, self._opencode, self._gemini, self._pi):
            try:
                read()
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                continue  # one agent's surprise is no reason to lose the others
        counted: dict[str, int] = {}
        for day, tokens in self.c["replies"].values():
            counted[day] = counted.get(day, 0) + tokens
        days = self.c["days"]
        for day, tokens in counted.items():
            days[day] = max(days.get(day, 0), tokens)
        oldest = _day(now - KEEP_REPLIES_DAYS * 86400)
        self.c["replies"] = {k: v for k, v in self.c["replies"].items() if v[0] >= oldest}
        self._save()
        return dict(days)

    def _save(self):
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.c, f, separators=(",", ":"))
        os.replace(tmp, self.path)

    def _reply(self, key: str, ts: float, tokens: int):
        day = _day(ts)
        if ts and day >= self.since:
            self.c["replies"][key] = [day, tokens]

    def _paths(self, pattern: str) -> list:
        found = []
        for path in glob.glob(os.path.join(self.home, pattern), recursive=True):
            try:
                if os.path.getmtime(path) >= self.since_ts:
                    found.append(path)
            except OSError:
                continue
        return sorted(found)

    def _lines(self, path: str, want: tuple):
        """Complete new lines since the last scan, holding any of `want`."""
        state = self.c["files"].setdefault(path, {"at": 0})
        size = os.path.getsize(path)
        if size < state["at"]:  # rewritten: count it again, ids keep that from doubling
            state.clear()
            state["at"] = 0
        if size == state["at"]:
            return
        with open(path, "rb") as f:
            f.seek(state["at"])
            for line in f:
                if not line.endswith(b"\n"):
                    break  # still being written
                state["at"] += len(line)
                if len(line) > LONGEST_LINE or not any(w in line for w in want):
                    continue
                try:
                    yield state, json.loads(line)
                except ValueError:
                    continue

    def _claude(self):
        for path in self._paths(os.path.join(".claude", "projects", "**", "*.jsonl")):
            for _, o in self._lines(path, (b'"usage"',)):
                m = o.get("message")
                if o.get("type") != "assistant" or not isinstance(m, dict) or not isinstance(m.get("usage"), dict):
                    continue
                if m.get("model") == "<synthetic>":
                    continue
                # One reply is written several times as it streams, and resuming copies it to another
                # file. The same id always means the same reply; the last has the final count.
                u = m["usage"]
                self._reply(f"c:{m.get('id')}:{o.get('requestId', '')}", _when(o.get("timestamp")),
                            _int(u.get("input_tokens")) + _int(u.get("output_tokens")))

    def _codex(self):
        for path in self._paths(os.path.join(".codex", "sessions", "**", "rollout-*.jsonl")):
            for state, o in self._lines(path, (b'"token_count"', b'"session_meta"')):
                p = o.get("payload")
                if not isinstance(p, dict):
                    continue
                if o.get("type") == "session_meta":
                    state["sid"] = p.get("id") or p.get("session_id") or state.get("sid")
                elif p.get("type") == "token_count":
                    self._codex_tokens(state, p.get("info"), _when(o.get("timestamp")), path)

    def _codex_tokens(self, state: dict, info, ts: float, path: str):
        total = (info or {}).get("total_token_usage")
        if not isinstance(total, dict) or not ts:
            return
        now = [_int(total.get(k)) for k in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens")]
        before = state.get("seen") or [0, 0, 0, 0]
        state["seen"] = now
        # It's a running total for the thread, and repeats itself. A smaller one is a new count.
        diff = [a - b if a >= b else a for a, b in zip(now, before)]
        day = _day(ts)
        if not any(diff) or day < self.since:
            return
        record = self.c["replies"].setdefault(f"x:{state.get('sid') or path}:{day}", [day, 0])
        record[1] += max(diff[0] - diff[1] - diff[2], 0) + diff[3]  # its input includes the cached part

    def _opencode(self):
        path = os.path.join(self.home, ".local", "share", "opencode", "opencode.db")
        if not os.path.exists(path) or os.path.getmtime(path) < self.since_ts:
            return
        cursor = self.c.get("opencode", 0)
        try:
            db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1)
            try:
                rows = db.execute("select id, time_updated, data from message where time_updated >= ?", (cursor,)).fetchall()
            finally:
                db.close()
        except sqlite3.Error:
            return  # busy, or a new format: next time
        for mid, updated, data in rows:
            cursor = max(cursor, updated)
            try:
                d = json.loads(data)
            except ValueError:
                continue
            t = d.get("tokens")
            if d.get("role") == "assistant" and isinstance(t, dict):
                self._reply(f"o:{mid}", _when((d.get("time") or {}).get("created") or updated),
                            _int(t.get("input")) + _int(t.get("output")) + _int(t.get("reasoning")))
        self.c["opencode"] = cursor

    def _gemini(self):
        for path in self._paths(os.path.join(".gemini", "tmp", "*", "chats", "*.json")):
            state = self.c["files"].setdefault(path, {})
            mtime = os.path.getmtime(path)
            if state.get("mtime") == mtime:
                continue
            state["mtime"] = mtime
            with open(path) as f:
                chat = json.load(f)
            for m in chat.get("messages") or []:
                t = m.get("tokens") if isinstance(m, dict) else None
                if isinstance(t, dict):
                    self._reply(f"g:{chat.get('sessionId')}:{m.get('id')}", _when(m.get("timestamp")),
                                max(_int(t.get("input")) - _int(t.get("cached")), 0) + _int(t.get("output")) + _int(t.get("thoughts")))

    def _pi(self):
        for path in self._paths(os.path.join(".pi", "agent", "sessions", "*", "*.jsonl")):
            for _, o in self._lines(path, (b'"usage"',)):
                m = o.get("message")
                if o.get("type") != "message" or not isinstance(m, dict) or m.get("role") != "assistant":
                    continue
                u = m.get("usage")
                if isinstance(u, dict):
                    self._reply(f"p:{path}:{o.get('id')}", _when(o.get("timestamp") or m.get("timestamp")),
                                _int(u.get("input")) + _int(u.get("output")))


def watch(pet, log=print):
    """Tells the pet what's been used, for as long as the daemon runs."""
    days = TokenDays(pet.born_day().isoformat())

    def loop():
        while True:
            try:
                pet.note_tokens(days.scan())
            except Exception as e:  # noqa: BLE001  the next scan may do better; this thread must not end
                log("token scan:", repr(e))
            time.sleep(SCAN_EVERY_S)

    threading.Thread(target=loop, daemon=True, name="token-days").start()
