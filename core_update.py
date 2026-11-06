"""A new core release for a board of the person's own: built here, with their folder, then put on.

idle -> building -> writing -> done
                           -> putting-back -> failed, with the old build back on
          any step -> failed

Only a release whose signature checks out is built (release.py), only for a registered board
(boards.py), and only while the board runs the build that was registered, because that's the one
that goes back on if the new one doesn't start. Being allowed to approve carries over to the new
build when the folder hasn't changed since the build that was allowed: nothing of the person's
changed, and the rest is ttycat's signed release.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

import release
from boards import Boards, board_api, core_of, folder_hash, idf_version
from flasher import make_image, read_image

WORK_DIR = Path.home() / ".ttycat" / "core"
BUILD_TIMEOUT_S = 30 * 60  # the first build also fetches the board's drivers
NINJA = re.compile(r"^\[(\d+)/(\d+)\]")  # [12/80], when ESP-IDF builds with Ninja
MAKE = re.compile(r"^\[\s*(\d+)%\]")  # [ 37%], with Make
PUT_BACK_TRIES = 5
PUT_BACK_WAIT_S = 3
MESSAGE_LEN = 160
# The shell ESP-IDF sets itself up in. Exit 97: it couldn't.
BUILD = '. "$0/export.sh" >/dev/null 2>&1; command -v idf.py >/dev/null || exit 97; exec idf.py -C "$1" -B "$2" -D BOARD_DIR="$3" build'


def idf_python(idf: Path, espressif: Path = Path.home() / ".espressif") -> list[str]:
    """The folder of the Python ESP-IDF was installed with. export.sh takes the first python3 it finds,
    and gives up if that isn't the one: the Mac's own is older than most installs use."""
    version = ".".join((idf_version(idf) or "").split(".")[:2])
    for cfg in sorted((espressif / "python_env").glob(f"idf{version}_py*_env/pyvenv.cfg"), reverse=True):
        try:
            home = re.search(r"^home = (.+)$", cfg.read_text(), re.M)
        except OSError:
            continue
        if home and (Path(home.group(1).strip()) / "python3").exists():
            return [home.group(1).strip()]
    return []


class Updater:
    def __init__(self, boards: Boards, releases: release.Releases, devices, log=print, work: Path = WORK_DIR,
                 keys: dict | None = None):
        self.boards, self.releases, self.devices, self.log = boards, releases, devices, log
        self.work, self.keys = Path(work), keys
        self.state, self.pct, self.msg = "idle", 0, ""
        self.to: str | None = None  # the release
        self.fw: str | None = None  # the build of it, once it's built
        self.reg: dict | None = None
        self.process: subprocess.Popen | None = None
        self.fd: int | None = None
        self.partial, self.last_lines = b"", []
        self.deadline = 0.0
        self.port: str | None = None
        self.write = None
        self.carry = False
        self.hash = ""
        self.why = ""
        self.tries = 0
        self.next_try = 0.0

    @property
    def busy(self) -> bool:
        return self.state in ("building", "writing", "putting-back")

    def offer(self, name: str | None, fw: str | None) -> dict:
        """What Setup shows for the board that's plugged in. Reading it changes nothing."""
        reg = self.boards.get(name)
        core = core_of(fw)
        best = self.releases.best()
        info = {"registered": reg is not None, "core": core, "available": None, "blocked": "",
                "state": "idle", "pct": 0, "msg": "", "to": None}
        if self.busy or (self.reg and self.reg["name"] == name):  # the last update's outcome is that board's only
            info.update(state=self.state, pct=self.pct, msg=self.msg, to=self.to)
        if best and core and release.newer(best["version"], core):
            info["available"] = best["version"]
            info["blocked"] = self.blocked(reg, best, fw) if reg else ""
        return info

    def blocked(self, reg: dict, rel: dict, fw: str | None) -> str:
        """Why this release can't be built for this board, or "" if it can."""
        folder = Path(reg["folder"])
        if not folder.is_dir():
            return f"Its folder isn't at {folder} any more."
        if board_api(folder) != rel["board_api"]:
            return "This update needs the board's folder changed. Ask Claude to update it."
        have = idf_version(Path(reg["idf"])) or ""
        if not (have + ".").startswith(rel["idf"] + "."):
            return f"This update needs ESP-IDF {rel['idf']}" + (f", and this Mac has {have}." if have else ".")
        if fw != reg["fw"]:
            return "It runs a build the app didn't make. Ask Claude to register the board again."
        return ""

    def start(self, name: str | None, fw: str | None, port: str | None, write) -> tuple[bool, str]:
        """`write(image_folder, port)` puts an image on the board, as the flasher does for Setup."""
        if self.busy:
            return False, "already updating"
        if not port or not fw:
            return False, "no board connected"
        reg = self.boards.get(name)
        if not reg:
            return False, "this board isn't registered"
        best = self.releases.best()
        if not best or not core_of(fw) or not release.newer(best["version"], core_of(fw)):
            return False, "there's no newer release"
        why = self.blocked(reg, best, fw)
        if why:
            return False, why
        try:
            source = release.unpack(best, self.work / "src", self.keys)
        except (ValueError, OSError) as e:
            return False, f"couldn't unpack the release: {e}"

        self.reg, self.to, self.fw, self.port, self.write = reg, best["version"], None, port, write
        self.hash = folder_hash(Path(reg["folder"]))
        # Allowed before, and nothing of theirs changed since the build that was allowed.
        self.carry = self.devices.may_approve(fw) and self.hash == reg["hash"]
        build = self.boards.root / reg["name"] / f"build-{best['version']}"
        env = {k: v for k, v in os.environ.items() if not k.startswith(("IDF_", "ESP_", "PYTHON", "VIRTUAL_ENV"))}
        env["PATH"] = ":".join([*idf_python(Path(reg["idf"])), "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin"])
        try:
            self.process = subprocess.Popen(
                ["/bin/bash", "-c", BUILD, reg["idf"], str(source), str(build), reg["folder"]],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                start_new_session=True)
        except OSError as e:
            self.process = None
            return False, f"couldn't start the build: {e}"
        self.fd = self.process.stdout.fileno()
        os.set_blocking(self.fd, False)
        self.partial, self.last_lines = b"", []
        self.state, self.pct, self.msg = "building", 0, ""
        self.deadline = time.monotonic() + BUILD_TIMEOUT_S
        self.log("core: building", best["version"], "for", reg["name"], "from", reg["folder"])
        return True, ""

    # ------------------------------------------------------------ building

    def on_output(self):
        if self.fd is None:
            return
        try:
            data = os.read(self.fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if data:
            *lines, self.partial = re.split(rb"[\r\n]", self.partial + data)
            for line in lines:
                self._line(line.decode(errors="replace").strip())
            return
        self._line(self.partial.decode(errors="replace").strip())
        self.partial = b""
        status = self.process.wait()
        self.process.stdout.close()
        self.fd, self.process = None, None
        self._built(status)

    def _line(self, line: str):
        if not line:
            return
        hit, made = NINJA.match(line), MAKE.match(line)
        if hit and int(hit.group(2)):
            self.pct = min(99, int(hit.group(1)) * 100 // int(hit.group(2)))
            return
        if made:
            self.pct = min(99, int(made.group(1)))
            return
        self.last_lines = (self.last_lines + [line])[-12:]

    def _built(self, status: int):
        if status == 97:
            return self._fail("ESP-IDF couldn't be set up. Run its install.sh again.")
        if status != 0:
            said = [line for line in self.last_lines if "error" in line.lower()] or self.last_lines[-1:]
            self.log("core: build failed |", " / ".join(self.last_lines)[-600:])
            return self._fail("The build failed: " + (said[0][:MESSAGE_LEN] if said else f"status {status}"))
        name = self.reg["name"]
        image = self.boards.root / name / "next"
        shutil.rmtree(image, ignore_errors=True)
        try:
            self.fw = make_image(self.boards.root / name / f"build-{self.to}", image)
            board = read_image(str(image))["board"]
        except (ValueError, OSError) as e:
            return self._fail(f"The build can't be used: {e}")
        if core_of(self.fw) != self.to or board != name:
            return self._fail(f"The build says it's {self.fw} for {board}, not {self.to} for {name}")
        ok, msg = self.write(str(image), self.port)
        if not ok:
            return self._fail(f"Couldn't write it: {msg}")  # nothing was written: the old build is still on
        self.state, self.pct = "writing", 100
        self.log("core: built", self.fw)

    # ------------------------------------------------------------ writing, and going back

    def tick(self, flasher, port: str | None):
        """Each turn of the daemon's loop, with the flasher's state and the board's port if it's there."""
        if port:
            self.port = port
        if self.state == "building" and time.monotonic() >= self.deadline:
            self._stop()
            self._fail("The build took too long.")
        elif self.state == "writing" and flasher.state == "done" and flasher.to == self.fw:
            self._done()
        elif self.state == "writing" and flasher.state == "failed":
            self.why = flasher.msg.split(". ")[0].rstrip(".")  # not its advice: the old build goes back by itself
            self.state, self.tries, self.next_try = "putting-back", 0, time.monotonic()
            self.log("core: the new build failed:", flasher.msg, "| putting", self.reg["fw"], "back")
        if self.state != "putting-back":
            return
        if flasher.state == "done" and flasher.to == self.reg["fw"] and self.tries:
            self._fail(f"The new build didn't work, so {self.reg['fw']} went back on. ({self.why})")
        elif not flasher.busy and time.monotonic() >= self.next_try:
            if self.tries >= PUT_BACK_TRIES:
                self._fail(f"The new build didn't work, and {self.reg['fw']} couldn't go back on: {flasher.msg}. "
                           "Hold BOOT while you plug the board in, then try again.")
                return
            self.tries += 1
            self.next_try = time.monotonic() + PUT_BACK_WAIT_S
            ok, msg = self.write(self.reg["image"], self.port)
            if not ok:
                self.log("core: putting back, try", self.tries, msg)

    def _done(self):
        name = self.reg["name"]
        self.boards.built(name, self.boards.root / name / "next", self.fw, self.hash)
        shutil.rmtree(self.boards.root / name / "next", ignore_errors=True)
        for old in self.boards.root.glob(f"{name}/build-*"):
            if old.name != f"build-{self.to}":
                shutil.rmtree(old, ignore_errors=True)
        if self.carry:
            self.devices.set_approve(self.fw, True)
        self.state, self.msg = "done", ""
        self.log("core: board now runs", self.fw, "| can approve" if self.carry else "")

    def _fail(self, reason: str):
        self.state, self.msg = "failed", reason[:300]
        self.log("core: failed:", reason)

    def _stop(self):
        if self.process:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except OSError:
                pass
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process.stdout.close()
        self.fd, self.process = None, None

    def close(self):
        self._stop()
