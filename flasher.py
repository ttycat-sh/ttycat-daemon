"""Puts a firmware image on the board, for the Mac app.

The daemon owns the serial port, so it's the one to let go of it, run esptool, and take it
back. An update is finished only when the board itself says it's running the new version.

An image is a folder with manifest.json next to the files it names (tools/firmware_bundle.py
makes one). Each file's hash is checked first: nothing is written from a damaged image.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from devices import is_custom

BAUD = "460800"
FLASH_TIMEOUT_S = 180
COMES_BACK_S = 30
MESSAGE_LEN = 120
PROGRESS = re.compile(r"Writing at 0x([0-9a-fA-F]+)\D*?\((\d+) ?%\)")
ESPTOOLS = "~/.espressif/python_env/*/lib/python*/site-packages/esptool/__init__.py"
CANT_CONNECT = "Couldn't reach the board. Hold BOOT while you plug it in, then try again."


def find_esptool() -> list[str] | None:
    """The app's own copy, or ESP-IDF's with the Python it's installed for. Only looks at files: this runs often."""
    own = Path(sys.prefix) / "lib" / f"python{sys.version_info[0]}.{sys.version_info[1]}" / "site-packages" / "esptool" / "__init__.py"
    if own.exists():
        return [sys.executable, "-E", "-s", "-B", "-m", "esptool"]
    for path in sorted(glob.glob(os.path.expanduser(ESPTOOLS)), reverse=True):
        env = Path(path).parents[4]
        python = env / "bin" / "python"
        if python.exists():
            return [str(python), "-m", "esptool"]
    return None


def make_image(build: Path, out: Path) -> str:
    """Packs a firmware build into an image, and gives the version it says it is. Raises ValueError.

    The version and board come out of the binary itself, the hello the board sends when it starts,
    so an image can't claim to be anything it isn't."""
    build, out = Path(build), Path(out)
    try:
        args = json.loads((build / "flasher_args.json").read_text())
        found = re.search(rb'\{"t":"hello","fw":"([^"\\]+)","b":"([^"\\]+)","p":\d+[,}]', (build / "ttycat.bin").read_bytes())
    except (OSError, ValueError) as e:
        raise ValueError(f"no usable build in {build}: {e}") from e
    if not found:
        raise ValueError(f"no version in {build / 'ttycat.bin'}: it may be from an older source, so build it again")
    out.mkdir(parents=True, exist_ok=True)
    files = []
    for offset, name in sorted(args["flash_files"].items(), key=lambda kv: int(kv[0], 16)):
        data = (build / name).read_bytes()
        (out / Path(name).name).write_bytes(data)
        files.append({"offset": offset, "path": Path(name).name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
    version, board = found.group(1).decode(), found.group(2).decode()
    manifest = {"version": version, "chip": args["extra_esptool_args"]["chip"], "board": board,
                "flash": args["write_flash_args"], "files": files}
    if is_custom(version):
        source = built_from(build, board)
        if source:
            manifest["from"] = source
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return version


def built_from(build: Path, board: str) -> dict | None:
    """For someone's own build: the board folder and ESP-IDF it came from, as ESP-IDF recorded them, and
    the folder's hash now, so a board flashed with it can be registered (boards.py). Never in a release:
    it's only for this Mac."""
    from boards import folder_hash  # boards.py imports this module
    try:
        about = json.loads((Path(build) / "project_description.json").read_text())
        folder, idf = about["build_component_info"][board]["dir"], about["idf_path"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(folder, str) or not isinstance(idf, str) or not Path(folder).is_dir():
        return None
    return {"folder": folder, "idf": idf, "hash": folder_hash(Path(folder))}


def read_image(folder: str) -> dict:
    """The manifest, once everything in it is known to be sound. Raises ValueError otherwise."""
    root = Path(folder)
    try:
        manifest = json.loads((root / "manifest.json").read_text())
        version, files, flash = manifest["version"], manifest["files"], manifest["flash"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise ValueError(f"no usable firmware image: {e}") from e
    chip = manifest.get("chip")  # esptool refuses to write if the board turns out to be another chip
    if not isinstance(chip, str) or not re.fullmatch(r"esp32[a-z0-9]*", chip):
        raise ValueError(f"no usable firmware image: chip {chip}")
    if not isinstance(version, str) or not version or not isinstance(files, list) or not files:
        raise ValueError("no usable firmware image: nothing in it")
    if not isinstance(flash, list) or not all(isinstance(a, str) and re.fullmatch(r"[\w-]+", a) for a in flash):
        raise ValueError("no usable firmware image: bad flash settings")
    checked = []
    for f in files:
        try:
            offset, name, size, digest = f["offset"], f["path"], f["size"], f["sha256"]
        except (KeyError, TypeError) as e:
            raise ValueError("no usable firmware image: bad file entry") from e
        if not isinstance(offset, str) or not re.fullmatch(r"0x[0-9a-fA-F]+", offset):
            raise ValueError(f"bad offset for {name}")
        path = (root / str(name)).resolve()
        if root.resolve() not in path.parents:
            raise ValueError(f"{name} is outside the image")
        try:
            data = path.read_bytes()
        except OSError as e:
            raise ValueError(f"missing from the image: {name}") from e
        if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
            raise ValueError(f"damaged image: {name}")
        checked.append({"offset": offset, "path": str(path), "size": size})
    source = manifest.get("from")
    return {"version": version, "chip": chip, "board": manifest.get("board"), "flash": flash, "files": checked,
            "from": source if isinstance(source, dict) else None}


class Flasher:
    """idle -> flashing -> restarting -> done, or failed from either. Never blocks the daemon."""

    def __init__(self, log=print, tool=find_esptool):
        self.log = log
        self.tool = tool
        self.state = "idle"
        self.pct = 0
        self.msg = ""
        self.to: str | None = None
        self.process: subprocess.Popen | None = None
        self.fd: int | None = None
        self.deadline = 0.0
        self.files: list[dict] = []
        self.written: dict[int, int] = {}
        self.partial = b""
        self.last_lines: list[str] = []
        self.image: str | None = None
        self.source: dict | None = None

    @property
    def busy(self) -> bool:
        return self.state in ("flashing", "restarting")

    @property
    def holds_port(self) -> bool:
        return self.state == "flashing"

    def info(self) -> dict:
        return {"state": self.state, "pct": self.pct, "msg": self.msg, "to": self.to, "tool": self.tool() is not None}

    def start(self, folder: str, port: str | None, fw: str | None, unknown_ok: bool, board: str | None = None) -> tuple[bool, str]:
        """`fw` is what the board on `port` said it runs. A port that hasn't said anything could
        be any other device, and one running its own firmware would lose it, so both need
        `unknown_ok`: the person was told what would be overwritten. `board` is which board it
        says it is: firmware for another would leave it with a dark screen, even on the same chip."""
        if self.busy:
            return False, "already updating"
        if not port:
            return False, "no board connected"
        if fw is None and not unknown_ok:
            return False, "the board hasn't said what it is"
        if is_custom(fw) and not unknown_ok:
            return False, f"it runs {fw}, which this would erase"
        tool = self.tool()
        if not tool:
            return False, "esptool not found"
        try:
            image = read_image(folder)
        except ValueError as e:
            return False, str(e)
        if board and image["board"] and board != image["board"]:
            return False, f"this firmware is for {image['board']}, and this board is {board}"
        command = [*tool, "--chip", image["chip"], "-p", port, "-b", BAUD, "--before", "default_reset", "--after", "hard_reset",
                   "write_flash", *image["flash"]]
        for f in image["files"]:
            command += [f["offset"], f["path"]]
        self.files, self.written, self.partial, self.last_lines = image["files"], {}, b"", []
        self.to, self.pct, self.msg = image["version"], 0, ""
        self.image, self.source = folder, image["from"]  # what's being written, and where it was built from
        try:
            self.process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env={**os.environ, "PYTHONUNBUFFERED": "1"}, start_new_session=True)
        except OSError as e:
            self.process = None
            return False, f"couldn't start esptool: {e}"
        self.fd = self.process.stdout.fileno()
        os.set_blocking(self.fd, False)
        self.state = "flashing"
        self.deadline = time.monotonic() + FLASH_TIMEOUT_S
        self.log("firmware: writing", image["version"], "to", port, f"(board had {fw})")
        return True, ""

    def on_output(self):
        """The select loop says esptool has written something, or has ended."""
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
        self._ended(self.process.wait())

    def _line(self, line: str):
        if not line:
            return
        hit = PROGRESS.search(line)
        if hit:
            self._progress(int(hit.group(1), 16), int(hit.group(2)))
            return
        self.last_lines = (self.last_lines + [line])[-6:]
        if line.startswith("Hash of data verified"):  # esptool reads each file back
            self.log("firmware:", line)

    def _progress(self, address: int, percent: int):
        for i, f in enumerate(self.files):
            start = int(f["offset"], 16)
            if start <= address < start + f["size"]:
                self.written[i] = f["size"] * percent // 100
        total = sum(f["size"] for f in self.files)
        self.pct = min(99, sum(self.written.values()) * 100 // total) if total else 0

    def _ended(self, status: int):
        if self.fd is not None:
            self.process.stdout.close()
        self.fd, self.process = None, None
        if status == 0:
            self.state, self.pct = "restarting", 100
            self.deadline = time.monotonic() + COMES_BACK_S
            self.log("firmware: written, waiting for the board")
        else:
            self._fail(self._reason(status))

    def _reason(self, status: int) -> str:
        if status < 0:
            return "The update was interrupted. Try again."
        said = " ".join(self.last_lines)
        if "Failed to connect" in said or "No serial data received" in said:
            return CANT_CONNECT
        for line in reversed(self.last_lines):
            if "error" in line.lower():
                return line[:MESSAGE_LEN]
        return f"esptool stopped with status {status}"

    def _fail(self, reason: str):
        self.state, self.msg = "failed", reason
        self.log("firmware: failed:", reason, "|", " / ".join(self.last_lines)[:300])

    def board_said(self, fw: str | None) -> bool:
        """The board said hello: this is the only proof that an update worked. True if it just did."""
        if self.state != "restarting":
            return False
        if fw == self.to:
            self.state, self.msg = "done", ""
            self.log("firmware: board now runs", fw)
            return True
        self._fail(f"the board came back running {fw}, not {self.to}")
        return False

    def tick(self):
        if self.state == "flashing" and time.monotonic() >= self.deadline:
            self._stop()
            self._fail("esptool took too long")
        elif self.state == "restarting" and time.monotonic() >= self.deadline:
            self._fail("the board didn't come back after writing. Unplug it and plug it back in.")

    def _stop(self):
        if self.process:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except OSError:
                pass
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process.stdout.close()
        self.fd, self.process = None, None

    def close(self):
        """The daemon is stopping. The ROM on the chip can always be written again."""
        self._stop()
