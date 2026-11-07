"""Asking ttycat's update server what's new for the board plugged in: when the app starts, then every hour.

The server is ttycat-server (GET /v1/updates). What's sent: the board's model (its folder's name), the
firmware it runs, and for a board of someone's own, the board interface its folder was made for and the
ESP-IDF on this Mac. Also a random ID made once for this Mac, so a release can go to some Macs first.
Nothing about sessions, prompts or the person.

What comes back is downloaded into ~/.ttycat, and checked as everything else is: a release whose signature
isn't one of ours (release.py), or whose files don't match it, is deleted at once. Nothing is written to a
board here. Setup offers what's been downloaded, and only a click puts it on.

Every check runs in a thread of its own, so the board never waits on the network.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

import build_info
import release
from boards import Boards, board_api, idf_version
from devices import is_custom
from flasher import read_image

UPDATES_URL = "https://api.ttycat.sh/v1/updates"
URL_ENV = "TTYCAT_UPDATES_URL"  # a developer's build's server, such as ttycat-server's compose.yaml: http allowed
HOME = Path.home() / ".ttycat"  # updates.json (the setting), install-id, firmware/, core/releases/
EVERY_S = 3600
TIMEOUT_S = 30
MAX_ANSWER = 256 * 1024
MAX_FILE = 32 * 1024 * 1024
FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
BOARD = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


def fetch(url: str, limit: int) -> bytes:
    """One GET, up to `limit` bytes. Raises OSError or ValueError."""
    request = urllib.request.Request(url, headers={"User-Agent": "ttycat-daemon"})
    with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310 (scheme checked by the caller)
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{url} is bigger than {limit} bytes")
    return data


class Checker:
    def __init__(self, boards: Boards | None = None, url: str | None = None, home: Path = HOME,
                 keys: dict[str, bytes] | None = None, log=print, get=fetch, build: dict | None = None):
        info = build_info.read() if build is None else build
        # A release asks the server it was built for (build.json), and never one its environment names.
        env = None if build_info.is_release(info) else os.environ.get(URL_ENV)
        self.url = url or env or info.get("updates") or UPDATES_URL
        self.plain_http = bool(url or env)  # only ever for a server someone chose, such as one on this Mac
        self.boards = boards or Boards()
        self.home = Path(home)
        self.firmware_dir = self.home / "firmware"
        self.releases_dir = self.home / "core" / "releases"
        self.keys, self.log, self.get = keys, log, get
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.last: dict[str, float] = {}  # each board asked about since the app started: time.monotonic()
        self.checked: float | None = None  # time.time() of the last one that got an answer
        self.error = ""

    # ------------------------------------------------------------ the setting, and what's been downloaded

    @property
    def enabled(self) -> bool:
        try:
            return json.loads((self.home / "updates.json").read_text()).get("check", True) is not False
        except (OSError, ValueError, AttributeError):
            return True

    def set_enabled(self, on: bool):
        self.home.mkdir(mode=0o700, exist_ok=True)
        temp = self.home / "updates.json.tmp"
        temp.write_text(json.dumps({"check": bool(on)}))
        os.replace(temp, self.home / "updates.json")

    def install_id(self) -> str:
        """Random, made once for this Mac, and only for rollouts: it says nothing about anyone."""
        path = self.home / "install-id"
        try:
            found = path.read_text().strip()
            if re.fullmatch(r"[0-9a-f]{32}", found):
                return found
        except OSError:
            pass
        made = secrets.token_hex(16)
        self.home.mkdir(mode=0o700, exist_ok=True)
        path.write_text(made)
        return made

    def info(self) -> dict:
        with self.lock:
            return {"check": self.enabled, "checked": self.checked, "error": self.error,
                    "checking": bool(self.thread and self.thread.is_alive())}

    def firmware_for(self, board: str | None, fw: str | None) -> dict | None:
        """The newest downloaded firmware for ttycat's own board, if it's newer than what it runs."""
        if not board or not fw or is_custom(fw) or not BOARD.fullmatch(board):
            return None
        best = None
        for folder in (self.firmware_dir / board).glob("*"):
            try:
                image = read_image(str(folder), self.keys)
            except ValueError:
                continue
            if image["board"] == board and release.newer(image["version"], fw) and \
                    (best is None or release.newer(image["version"], best["version"])):
                best = {"version": image["version"], "dir": str(folder)}
        return best

    # ------------------------------------------------------------ when to ask

    def tick(self, name: str | None, fw: str | None, key: str | None, now: float | None = None):
        """Each turn of the daemon's loop, with the board that's connected and speaks, if any.
        Asks the first time a board is seen after the app starts, and then every hour."""
        if not name or not fw or not self.enabled:
            return
        now = time.monotonic() if now is None else now
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            board = key or name
            if board in self.last and now - self.last[board] < EVERY_S:
                return
            query = self.query(name, fw)
            if query is None:
                return
            self.last[board] = now
            self.thread = threading.Thread(target=self._check, args=(query,), daemon=True)
            self.thread.start()

    def query(self, name: str, fw: str) -> dict | None:
        """What to ask about this board, or nothing if no answer could be used: a board of someone's
        own that isn't registered has no folder to build a release with."""
        q = {"board": name, "fw": fw, "install": self.install_id()}
        if is_custom(fw):
            reg = self.boards.get(name)
            if not reg:
                return None
            api, idf = board_api(Path(reg["folder"])), idf_version(Path(reg["idf"]))
            if not api or not idf:
                return None
            q.update(board_api=str(api), idf=idf)
        return q

    def wait(self, timeout: float = 30):
        """For tests: the check that's running, done."""
        if self.thread:
            self.thread.join(timeout)

    # ------------------------------------------------------------ asking, downloading, checking

    def _check(self, query: dict):
        try:
            answer = json.loads(self.get(self.url + "?" + urllib.parse.urlencode(query), MAX_ANSWER))
            if not isinstance(answer, dict):
                raise ValueError("the server's answer isn't an object")
            if "firmware" in answer:
                self._firmware(query["board"], answer["firmware"])
            if "core" in answer:
                self._core(answer["core"])
            with self.lock:
                self.checked, self.error = time.time(), ""
        except (OSError, ValueError, KeyError, TypeError) as e:
            with self.lock:
                self.error = str(e)[:200]
            self.log("updates:", e)

    def _url(self, url) -> str:
        if not isinstance(url, str):
            raise ValueError("a download has no address")
        scheme = urllib.parse.urlsplit(url).scheme
        if scheme != "https" and not (self.plain_http and scheme == "http"):
            raise ValueError(f"{url} isn't https")
        return url

    def _firmware(self, board: str, offer: dict):
        envelope, files = offer["release"], offer["files"]
        body = json.loads(envelope["body"])  # read only for where to put it: checked below
        version = body["version"]
        if not isinstance(version, str) or not release.VERSION.fullmatch(version) or body.get("board") != board:
            raise ValueError("the firmware offered isn't for this board")
        target = self.firmware_dir / board / version
        if target.exists():
            return
        temp = self._temp(self.firmware_dir)
        try:
            (temp / "release.json").write_text(json.dumps(envelope))
            for name, url in files.items():
                if name == "release.json":
                    continue  # the one in the answer is what's checked
                if not isinstance(name, str) or not FILE.fullmatch(name):
                    raise ValueError(f"{name!r} isn't a file name")
                (temp / name).write_bytes(self.get(self._url(url), MAX_FILE))
            image = read_image(str(temp), self.keys)  # the signature, and every file's hash
            if image["board"] != board or image["version"] != version:
                raise ValueError("the firmware isn't what it said")
            target.parent.mkdir(parents=True, exist_ok=True)
            temp.rename(target)
        finally:
            shutil.rmtree(temp, ignore_errors=True)
        for old in (self.firmware_dir / board).glob("*"):  # keep only the newest
            if old != target and old.is_dir():
                shutil.rmtree(old, ignore_errors=True)
        self.log("updates: firmware", version, "for", board, "downloaded")

    def _core(self, offer: dict):
        envelope = offer["release"]
        body = json.loads(envelope["body"])  # read only for names: checked below
        version, archive = body["version"], body["source"]["file"]
        if not isinstance(version, str) or not release.VERSION.fullmatch(version) or \
                not isinstance(archive, str) or not release.FILE.fullmatch(archive):
            raise ValueError("the core release offered has odd names")
        name = f"ttycat-core-{version}.json"
        if (self.releases_dir / name).exists():
            return
        temp = self._temp(self.releases_dir)
        try:
            (temp / name).write_text(json.dumps(envelope))
            (temp / archive).write_bytes(self.get(self._url(offer["archive"]), release.MAX_SOURCE))
            release.open_release(temp / name, self.keys)  # the signature, and the archive's hash
            for path in (temp / archive, temp / name):  # the archive first: the release names it
                os.replace(path, self.releases_dir / path.name)
        finally:
            shutil.rmtree(temp, ignore_errors=True)
        self.log("updates: core", version, "downloaded")

    @staticmethod
    def _temp(parent: Path) -> Path:
        parent.mkdir(parents=True, exist_ok=True)
        temp = parent / f".downloading-{secrets.token_hex(4)}"
        temp.mkdir()
        return temp
