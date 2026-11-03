"""The pet's life: what it needs, and what's been done for it. The board only draws this.

It's a companion, not a chore. It gets sad and can get sick, but it never dies, and it
never beeps: the chime means an agent needs you. Food comes from turns ending, however
they end. Allow and Deny earn the same, so the pet is never a reason to answer either way.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

PET_FILE = Path.home() / ".ttycat" / "pet.json"

# Points per hour, on 0-100.
AWAKE_RATES = {"food": -6.0, "fun": -5.0, "energy": -5.0}
ASLEEP_RATES = {"food": -2.0, "fun": -1.0, "energy": 15.0}
POOP_EVERY_H = 5.0        # awake hours between messes
MAX_POOPS = 3
SICK_AFTER_H = 6.0        # empty or filthy this long, awake or asleep
MAX_CATCHUP_S = 12 * 3600 # time the daemon was off barely counts: a holiday isn't neglect
STEP_S = 600
SNACK_FOOD, RICE_FOOD = 30, 10
FULL_AT = 95
NAP_MAX_S = 3 * 3600
STAY_UP_S = 1800
NIGHT = (22, 7)
PAT_COOLDOWN_S = 60
SAVE_EVERY_S = 60
STARTING_SNACKS = 3
# What each kind of care looks like: the board and the Mac app both show it, whichever gave it.
REACTIONS = {"feed": "eat", "clean": "sparkle", "heal": "heal", "pat": "heart", "play": "heart"}


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
        self.neglect = float(d.get("neglect", 0))
        self.sick = bool(d.get("sick", False))
        self.snacks = int(d.get("snacks", STARTING_SNACKS))
        self.best = int(d.get("best", 0))
        self.nap_until = float(d.get("nap_until", 0))
        self.awake_until = float(d.get("awake_until", 0))
        self.last = float(d.get("last", now))
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
        keys = ("name", "born", "total", "today", "day", "food", "fun", "energy", "poops", "poop_in", "neglect",
                "sick", "snacks", "best", "nap_until", "awake_until", "last")
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
        if self.sick:
            self.fun = max(0.0, self.fun - 5.0 * hours)
        if not asleep:
            self.poop_in -= hours
            if self.poop_in <= 0:
                self.poop_in = POOP_EVERY_H
                self.poops = min(MAX_POOPS, self.poops + 1)
        if self.nap_until and self.energy >= 100:
            self.nap_until = 0
        if self.food <= 0 or self.poops >= MAX_POOPS:
            self.neglect += hours
            if self.neglect >= SICK_AFTER_H:
                self.sick = True
        else:
            self.neglect = 0

    def tick(self):
        with self.lock:
            now = self.clock()
            dt = min(now - self.last, MAX_CATCHUP_S)
            self.last = now
            t = now - dt
            while dt > 0:
                step = min(dt, STEP_S)
                t += step
                self._advance(step / 3600, t)
                dt -= step
            self._roll()
            if now - self.saved_at >= SAVE_EVERY_S:
                self.save()

    def need(self) -> str:
        """The one thing it would ask for, if it could. Most urgent first."""
        if self.sick:
            return "sick"
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
            self.save()

    def message(self) -> dict:
        with self.lock:
            self.tick()
            now = self.clock()
            return {"total": self.total, "today": self.today, "food": round(self.food), "fun": round(self.fun),
                    "energy": round(self.energy), "poops": self.poops, "sick": self.sick,
                    "asleep": self.asleep_at(now), "snacks": self.snacks, "best": self.best,
                    "age": int((now - self.born) // 86400), "name": self.name, "need": self.need(),
                    "ev": self.ev, "evk": self.evk}

    def act(self, action: str, arg: str = "") -> tuple[bool, str]:
        with self.lock:
            self.tick()
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
        if action == "heal":
            if not self.sick:
                return False, f"{self.name} feels fine"
            self.sick, self.neglect = False, 0
            return True, ""
        if action == "play":
            if self.sick:
                return False, f"{self.name} is too ill"
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
