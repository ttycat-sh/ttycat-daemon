"""The pet's life: what it needs, and what's been done for it. The board only draws this.

It's a companion, not a chore. It gets sad, but it's never ill and it never dies, and it
never beeps: the chime means an agent needs you. Food comes from turns ending, however
they end. Allow and Deny earn the same, so the pet is never a reason to answer either way.

It levels up with time and use, and neither can be raced: a day is worth so much however
many tokens went by. A day off earns nothing, and costs nothing. It's just glad to see you.
"""
from __future__ import annotations

import json
import math
import threading
import time
from datetime import date
from pathlib import Path

PET_FILE = Path.home() / ".ttycat" / "pet.json"

# Points per hour, on 0-100.
AWAKE_RATES = {"food": -6.0, "fun": -5.0, "energy": -5.0}
ASLEEP_RATES = {"food": -2.0, "fun": -1.0, "energy": 15.0}
POOP_EVERY_H = 5.0        # awake hours between messes
MAX_POOPS = 3
MAX_CATCHUP_S = 12 * 3600 # time the daemon was off barely counts
AWAY_AFTER_S = 12 * 3600  # no sign of anyone this long: its needs wait for you to come back
STEP_S = 600
SNACK_FOOD, RICE_FOOD = 30, 10
FULL_AT = 95
NAP_MAX_S = 3 * 3600
STAY_UP_S = 1800
NIGHT = (22, 7)
PAT_COOLDOWN_S = 60
SAVE_EVERY_S = 60
STARTING_SNACKS = 3
GROW_PER_DAY = 1.0        # towards its level, for each day that goes by
TOKEN_BONUS = 2.0         # the most a day's tokens can add to that
TOKEN_SCALE = 200_000     # each 140k or so tokens halves what's left of the bonus
TOKEN_DAYS_PER_WEEK = 5   # only the biggest days count, so working through the weekend gains nothing
BACK_FOOD, BACK_FUN = 70.0, 70.0  # the least it comes back from a day off with
LONG_AWAY_DAYS = 3        # days off in a row that earn the bigger hello
AROUND_KEPT = 30
# What each kind of care looks like: the board and the Mac app both show it, whichever gave it.
REACTIONS = {"feed": "eat", "clean": "sparkle", "pat": "heart", "play": "heart"}


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
        self.food = float(d.get("food", 70))
        self.fun = float(d.get("fun", 70))
        self.energy = float(d.get("energy", 80))
        self.poops = int(d.get("poops", 0))
        self.poop_in = float(d.get("poop_in", POOP_EVERY_H))
        self.snacks = int(d.get("snacks", STARTING_SNACKS))
        self.best = int(d.get("best", 0))
        self.nap_until = float(d.get("nap_until", 0))
        self.awake_until = float(d.get("awake_until", 0))
        self.last = float(d.get("last", now))
        today = time.strftime("%Y-%m-%d", local(now))
        # A pet from before growth worked this way keeps what its level came from, and counts on from today.
        self.counted_from = str(d.get("counted_from", today))
        self.head_start = int(d.get("head_start", self.total))
        self.tokens = d["tokens"] if isinstance(d.get("tokens"), dict) else {}  # {where: {day: tokens}}
        self.around = [str(x) for x in d["around"]] if isinstance(d.get("around"), list) else []  # days someone was
        self.seen_at = float(d.get("seen_at", now))
        self.greeted = str(d.get("greeted", ""))
        self.pat_at = 0.0
        self.ev, self.evk = 0, ""  # the latest care, numbered so each is shown once; not kept across restarts
        self.saved_at = now
        self._roll()

    def _load(self) -> dict:
        if not self.path:
            return {}
        try:
            d = json.loads(self.path.read_text())
            float(d.get("total", 0)), float(d.get("food", 0)), float(d.get("last", 0))
            return d
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def save(self):
        self.saved_at = self.clock()
        if not self.path:
            return
        keys = ("name", "born", "total", "today", "day", "food", "fun", "energy", "poops", "poop_in",
                "snacks", "best", "nap_until", "awake_until", "last",
                "counted_from", "head_start", "tokens", "around", "seen_at", "greeted")
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
        if t < self.awake_until:
            return False
        if t < self.nap_until:
            return True
        hour = self.local(t).tm_hour
        return hour >= NIGHT[0] or hour < NIGHT[1]

    def _advance(self, hours: float, t: float):
        asleep = self.asleep_at(t)
        rates = ASLEEP_RATES if asleep else AWAKE_RATES
        for key, rate in rates.items():
            setattr(self, key, max(0.0, min(100.0, getattr(self, key) + rate * hours)))
        if not asleep:
            self.poop_in -= hours
            if self.poop_in <= 0:
                self.poop_in = POOP_EVERY_H
                self.poops = min(MAX_POOPS, self.poops + 1)
        if self.nap_until and self.energy >= 100:
            self.nap_until = 0

    def tick(self):
        with self.lock:
            now = self.clock()
            dt = min(now - self.last, MAX_CATCHUP_S)
            self.last = now
            t = now - dt
            while dt > 0:
                step = min(dt, STEP_S)
                t += step
                if t - self.seen_at < AWAY_AFTER_S:
                    self._advance(step / 3600, t)
                dt -= step
            self._roll()
            if now - self.saved_at >= SAVE_EVERY_S:
                self.save()

    def need(self) -> str:
        """The one thing it would ask for, if it could. Most urgent first."""
        if self.clock() - self.seen_at >= AWAY_AFTER_S:
            return ""  # it isn't waiting for you with a long face
        if self.food < 25:
            return "food"
        if self.poops:
            return "mess"
        if self.energy < 20:
            return "tired"
        if self.fun < 25:
            return "bored"
        return ""

    def finished(self):
        """A turn ended, whether it ran to the end or was denied: one snack in the pantry, and it grows."""
        with self.lock:
            self._roll()
            self.total += 1
            self.today += 1
            self.snacks = min(99, self.snacks + 1)
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
        """Someone touched the board or opened the panel. After a day off, it's rested and glad to see them."""
        with self.lock:
            self.tick()
            off, today = self._days_off(), self._date().isoformat()
            self._around()
            birthday = self.birthday()
            if (off > 0 or birthday) and self.greeted != today:
                self.greeted = today
                if off > 0:
                    self.food, self.fun = max(self.food, BACK_FOOD), max(self.fun, BACK_FUN)
                    self.energy, self.poops, self.poop_in = 100.0, 0, POOP_EVERY_H
                self.ev += 1
                self.evk = "birthday" if birthday else "welcome" if off >= LONG_AWAY_DAYS else "rested"
                self.save()

    def message(self) -> dict:
        with self.lock:
            self.tick()
            now = self.clock()
            age = int((now - self.born) // 86400)
            return {"total": self.total, "grown": self.grown(), "today": self.today, "food": round(self.food), "fun": round(self.fun),
                    "energy": round(self.energy), "poops": self.poops, "birthday": self.birthday(),
                    "asleep": self.asleep_at(now), "snacks": self.snacks, "best": self.best,
                    "age": age, "name": self.name, "need": self.need(),
                    "ev": self.ev, "evk": self.evk}

    def act(self, action: str, arg: str = "") -> tuple[bool, str]:
        if action == "here":
            self.here()
            return True, ""
        with self.lock:
            self.here()
            ok, msg = self._act(action, arg)
            if ok:
                self.ev += 1
                self.evk = REACTIONS.get(action, "")
                self.save()
            return ok, msg

    def _act(self, action: str, arg: str) -> tuple[bool, str]:
        now = self.clock()
        asleep = self.asleep_at(now)
        if action == "sleep":
            if asleep:
                self.nap_until, self.awake_until = 0, now + STAY_UP_S
            else:
                self.awake_until, self.nap_until = 0, now + NAP_MAX_S
            return True, ""
        if action == "pat":
            if now - self.pat_at >= PAT_COOLDOWN_S:
                self.pat_at = now
                self.fun = min(100.0, self.fun + 2)
            return True, ""
        if asleep:
            return False, f"{self.name} is asleep"
        if action == "feed":
            if self.food >= FULL_AT:
                return False, f"{self.name} is full"
            if self.snacks > 0:
                self.snacks -= 1
                self.food = min(100.0, self.food + SNACK_FOOD)
            else:
                self.food = min(100.0, self.food + RICE_FOOD)
            return True, ""
        if action == "clean":
            if not self.poops:
                return False, "already clean"
            self.poops = 0
            return True, ""
        if action == "play":
            score = max(0, min(999, int(arg))) if arg.isdigit() else 0
            self.best = max(self.best, score)
            self.fun = min(100.0, self.fun + min(50, 10 + score * 4))
            self.energy = max(0.0, self.energy - 8)
            self.food = max(0.0, self.food - 4)
            return True, ""
        return False, "unknown action"

    def rename(self, name: str):
        with self.lock:
            cleaned = "".join(c for c in name if 32 <= ord(c) < 127).strip()[:11]
            if cleaned:
                self.name = cleaned
                self.save()
