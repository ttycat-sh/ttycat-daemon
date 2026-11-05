"""Bit: how far it's grown, and what it has to say. The board only draws this.

There's nothing to look after. It's never hungry or ill, it never dies and it never beeps: the
chime means an agent needs you. Allow and Deny count the same, so it's never a reason to answer
either way.

It levels up with time and use, and neither can be raced: a day is worth so much however many
tokens went by. A day off earns nothing, and costs nothing. It's just glad to see you.
"""
from __future__ import annotations

import json
import math
import threading
import time
from datetime import date
from pathlib import Path

PET_FILE = Path.home() / ".ttycat" / "pet.json"

NIGHT = (22, 7)
SAVE_EVERY_S = 60
GROW_PER_DAY = 1.0        # towards its level, for each day that goes by
TOKEN_BONUS = 2.0         # the most a day's tokens can add to that
TOKEN_SCALE = 200_000     # each 140k or so tokens halves what's left of the bonus
TOKEN_DAYS_PER_WEEK = 5   # only the biggest days count, so working through the weekend gains nothing
LONG_AWAY_DAYS = 3        # days off in a row that earn the bigger hello
AROUND_KEPT = 30


def token_bonus(tokens: float) -> float:
    """What a day's tokens add to its growth. Each one is worth less than the last, and it resets daily."""
    return TOKEN_BONUS * (1 - math.exp(-max(0, tokens) / TOKEN_SCALE))


class Pet:
    def __init__(self, path: Path | None = PET_FILE, clock=time.time, local=time.localtime):
        self.path, self.clock, self.local = path, clock, local
        self.lock = threading.RLock()  # remote machines finish turns on their own threads
        now = clock()
        d = self._load()
        self.name = str(d.get("name", "Bit"))[:11]
        self.born = float(d.get("born", now))
        self.total = int(d.get("total", 0))
        self.today = int(d.get("today", 0))
        self.day = str(d.get("day", ""))
        today = time.strftime("%Y-%m-%d", local(now))
        # A pet from before growth worked this way keeps what its level came from, and counts on from today.
        self.counted_from = str(d.get("counted_from", today))
        self.head_start = int(d.get("head_start", self.total))
        self.tokens = d["tokens"] if isinstance(d.get("tokens"), dict) else {}  # {where: {day: tokens}}
        self.around = [str(x) for x in d["around"]] if isinstance(d.get("around"), list) else []  # days someone was
        self.seen_at = float(d.get("seen_at", now))
        self.greeted = str(d.get("greeted", ""))
        self.ev, self.evk = 0, ""  # the latest reaction, numbered so each is shown once; not kept across restarts
        self.saved_at = now
        self._roll()

    def _load(self) -> dict:
        if not self.path:
            return {}
        try:
            d = json.loads(self.path.read_text())
            float(d.get("total", 0)), float(d.get("born", 0))
            return d
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def save(self):
        self.saved_at = self.clock()
        if not self.path:
            return
        keys = ("name", "born", "total", "today", "day", "counted_from", "head_start", "tokens", "around", "seen_at", "greeted")
        try:
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps({k: getattr(self, k) for k in keys}))
            tmp.replace(self.path)
        except OSError:
            pass

    def _roll(self):
        day = time.strftime("%Y-%m-%d", self.local(self.clock()))
        if day != self.day:
            self.day, self.today = day, 0

    def asleep_at(self, t: float) -> bool:
        hour = self.local(t).tm_hour
        return hour >= NIGHT[0] or hour < NIGHT[1]

    def tick(self):
        with self.lock:
            self._roll()
            if self.clock() - self.saved_at >= SAVE_EVERY_S:
                self.save()

    def finished(self):
        """A turn ended, whether it ran to the end or was denied. It's counted, and nothing more."""
        with self.lock:
            self._roll()
            self.total += 1
            self.today += 1
            self._around()
            self.save()

    def _date(self) -> date:
        return date(*self.local(self.clock())[:3])

    def _around(self):
        self.seen_at = self.clock()
        today = self._date().isoformat()
        if not self.around or self.around[-1] != today:
            self.around = [*self.around, today][-AROUND_KEPT:]

    def note_tokens(self, days: dict, where: str = "here"):
        """{day: tokens} used so far, from one machine. A day's count only ever goes up."""
        with self.lock:
            kept = self.tokens.setdefault(where, {})
            for day, tokens in days.items():
                if isinstance(tokens, int) and tokens > kept.get(day, 0):
                    kept[day] = tokens

    def _tokens_by_day(self) -> dict:
        total: dict[str, int] = {}
        for days in self.tokens.values():
            for day, tokens in days.items():
                total[day] = total.get(day, 0) + tokens
        return total

    def grown(self) -> int:
        """What its level comes from. Goes up with each day, and with each day's tokens."""
        with self.lock:
            try:
                start = date.fromisoformat(self.counted_from)
            except ValueError:
                return self.head_start
            weeks: dict[tuple, list] = {}
            for day, tokens in self._tokens_by_day().items():
                try:
                    when = date.fromisoformat(day)
                except ValueError:
                    continue
                if when >= start:
                    weeks.setdefault(tuple(when.isocalendar())[:2], []).append(token_bonus(tokens))
            earned = sum(sum(sorted(week, reverse=True)[:TOKEN_DAYS_PER_WEEK]) for week in weeks.values())
            return self.head_start + int(max(0, (self._date() - start).days) * GROW_PER_DAY + earned)

    def _days_off(self) -> int:
        """Whole days since agents last ran or anyone was here, up to today."""
        today = self._date()
        used = [d for d, tokens in self._tokens_by_day().items() if tokens > 0]
        before = [d for d in [*self.around, *used] if d < today.isoformat()]
        if not before:
            return 0  # it's new: there's nothing to come back to
        try:
            return (today - date.fromisoformat(max(before))).days - 1
        except ValueError:
            return 0

    def birthday(self) -> bool:
        """The first of every month, once it's at least a day old. Nothing is lost by not being there."""
        return self._date().day == 1 and self.clock() - self.born >= 86400

    def here(self):
        """Someone touched the board or opened the panel. After a day off, it's glad to see them."""
        with self.lock:
            self.tick()
            off, today = self._days_off(), self._date().isoformat()
            self._around()
            birthday = self.birthday()
            if (off > 0 or birthday) and self.greeted != today:
                self.greeted = today
                self.ev += 1
                self.evk = "birthday" if birthday else "welcome" if off >= LONG_AWAY_DAYS else "rested"
                self.save()

    def message(self) -> dict:
        with self.lock:
            self.tick()
            now = self.clock()
            age = int((now - self.born) // 86400)
            return {"total": self.total, "grown": self.grown(), "today": self.today, "birthday": self.birthday(),
                    "asleep": self.asleep_at(now), "age": age, "name": self.name, "ev": self.ev, "evk": self.evk}

    def act(self, action: str, arg: str = "") -> tuple[bool, str]:
        with self.lock:
            if action == "here":
                self.here()
                return True, ""
            if action == "pat":  # a heart, and that's all
                self.here()
                self.ev += 1
                self.evk = "heart"
                return True, ""
            return False, "unknown action"

    def rename(self, name: str):
        with self.lock:
            cleaned = "".join(c for c in name if 32 <= ord(c) < 127).strip()[:11]
            if cleaned:
                self.name = cleaned
                self.save()
