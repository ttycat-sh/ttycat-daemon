"""What AI models cost, from ttycat's server, for an extension that shows an estimate of what the
tokens it counted would cost. Nothing in the daemon itself asks for this: on a Mac with no such
extension, it's never fetched.

The server is ttycat's (GET /v1/model-insights/prices): public list prices, in US dollars per
million tokens, and the same table for everyone. What's sent: nothing. No install ID,
and nothing about which models this Mac uses, because it's always the whole table, and whoever
wants a sum does it here. It's asked for at most once a day, only while Setup's update check is on
(updates.py has the setting), and only while something wants it.

What comes back is kept in ~/.ttycat/prices.json. It isn't signed, as a release is: all it can be
is a list of names and prices (read_table), and all it can change is a number someone is shown as
an estimate.

The asking runs in a thread of its own, so nothing waits on the network: table() answers with what
there is.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

import build_info

PRICES_URL = "https://api.ttycat.sh/v1/model-insights/prices"
PATH = "/v1/model-insights/prices"
URL_ENV = "TTYCAT_PRICES_URL"  # a developer's build's server, such as one on this Mac: http allowed
HOME = Path.home() / ".ttycat"
EVERY_S = 24 * 3600
AGAIN_S = 3600  # after an answer that couldn't be used
TIMEOUT_S = 30
MAX_ANSWER = 1024 * 1024  # the table is about 32 KB
MAX_MODELS = 5000
MAX_NAME = 128
HIGHEST = 100_000  # dollars per million tokens: more is a mistake, not a price


def fetch(url: str, etag: str, limit: int) -> tuple[bytes | None, str]:
    """One GET, up to `limit` bytes: the answer and its ETag, or None if it's still what `etag` names.
    Raises OSError or ValueError."""
    headers = {"User-Agent": "ttycat-daemon"}
    if etag:
        headers["If-None-Match"] = etag
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310 (scheme checked by the caller)
            data = response.read(limit + 1)
            new = response.headers.get("ETag") or ""
    except urllib.error.HTTPError as e:
        e.close()
        if e.code == 304:
            return None, etag
        raise
    if len(data) > limit:
        raise ValueError(f"{url} is bigger than {limit} bytes")
    return data, new


def _price(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= HIGHEST


def _name(value) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_NAME and value.isprintable()


def read_table(answer) -> dict:
    """The table in the server's answer, with only what a table can hold: when it was fetched, and
    for each model its provider, its name, and prices that are prices. A model that isn't that is
    left out. Raises ValueError for an answer that isn't a table of dollars per million tokens."""
    if not isinstance(answer, dict) or answer.get("currency") != "USD" or answer.get("per") != 1_000_000:
        raise ValueError("the answer isn't a table of US dollars per million tokens")
    models = answer.get("models")
    if not isinstance(models, list) or len(models) > MAX_MODELS:
        raise ValueError("the answer has no list of models, or one too long")
    out = []
    for m in models:
        if not isinstance(m, dict) or not _name(m.get("provider")) or not _name(m.get("model")) or \
                not _price(m.get("input")) or not _price(m.get("output")):
            continue
        row = {"provider": m["provider"], "model": m["model"], "input": m["input"], "output": m["output"]}
        for cache in ("cache_read", "cache_write"):
            if _price(m.get(cache)):
                row[cache] = m[cache]
        out.append(row)
    if not out:
        raise ValueError("the answer has no model with a price")
    fetched = answer.get("fetched_at")
    return {"fetched_at": fetched if _name(fetched) else "", "models": out}


class Prices:
    """`enabled`: whether Setup's update check is on, asked each time."""

    def __init__(self, enabled: Callable[[], bool], url: str | None = None, home: Path = HOME, log=print,
                 get=fetch, build: dict | None = None, clock=time.time):
        info = build_info.read() if build is None else build
        # A release asks the server it was built for (build.json), and never one its environment names.
        env = None if build_info.is_release(info) else os.environ.get(URL_ENV)
        beside = urllib.parse.urljoin(info["updates"], PATH) if isinstance(info.get("updates"), str) else None
        self.url = url or env or info.get("prices") or beside or PRICES_URL
        self.plain_http = bool(url or env)  # only ever for a server someone chose, such as one on this Mac
        self.enabled, self.log, self.get, self.clock = enabled, log, get, clock
        self.file = Path(home) / "prices.json"
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.kept: dict | None = None  # {"etag", "asked", "table"}, as in the file
        self.tried = 0.0               # when the last answer couldn't be used
        self.error = ""

    def _kept(self) -> dict:
        if self.kept is None:
            try:
                kept = json.loads(self.file.read_text())
                self.kept = {"etag": str(kept.get("etag") or ""), "asked": float(kept.get("asked") or 0),
                             "table": read_table({**kept["table"], "currency": "USD", "per": 1_000_000})}
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                self.kept = {"etag": "", "asked": 0.0, "table": None}
        return self.kept

    def table(self) -> dict | None:
        """The table as it was last fetched, {"fetched_at": ..., "models": [...]}, or None if it never
        has been. Calling this is what says the table is wanted: if the one there is more than a day
        old, and the update check is on, it's asked for again, for next time."""
        with self.lock:
            kept, now = self._kept(), self.clock()
            due = now - kept["asked"] >= EVERY_S and now - self.tried >= AGAIN_S
            if due and not (self.thread and self.thread.is_alive()) and self.enabled():
                self.thread = threading.Thread(target=self._ask, args=(kept["etag"] if kept["table"] else "",), daemon=True)
                self.thread.start()
            return kept["table"]

    def info(self) -> dict:
        with self.lock:
            kept = self._kept()
            return {"asked": kept["asked"] or None, "fetched_at": (kept["table"] or {}).get("fetched_at") or None, "error": self.error}

    def wait(self, timeout: float = 30):
        """For tests: the asking that's running, done."""
        if self.thread:
            self.thread.join(timeout)

    def _ask(self, etag: str):
        try:
            scheme = urllib.parse.urlsplit(self.url).scheme
            if scheme != "https" and not (self.plain_http and scheme == "http"):
                raise ValueError(f"{self.url} isn't https")
            answer, new = self.get(self.url, etag, MAX_ANSWER)
            with self.lock:
                kept = self._kept()
                if answer is not None:
                    kept["table"], kept["etag"] = read_table(json.loads(answer)), new
                kept["asked"] = self.clock()
                self.error = ""
                self._save(kept)
        except (OSError, ValueError, TypeError) as e:
            with self.lock:
                self.tried, self.error = self.clock(), str(e)[:200]
            self.log("prices:", e)

    def _save(self, kept: dict):
        self.file.parent.mkdir(mode=0o700, exist_ok=True)
        temp = self.file.with_name("prices.json.tmp")
        temp.write_text(json.dumps(kept, separators=(",", ":")))
        os.replace(temp, self.file)
