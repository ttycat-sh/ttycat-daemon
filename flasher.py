"""Puts a firmware image on the board, for the Mac app.

The daemon owns the serial port, so it's the one to let go of it, run esptool, and take it
back. An update is finished only when the board itself says it's running the new version.

An image is a folder with manifest.json next to the files it names (tools/firmware_bundle.py
makes one). Each file's hash is checked first: nothing is written from a damaged image.

ttycat's own firmware, with a plain version, is only written from a signed image: one with a
release.json whose signature is one of ours (release.py), wherever the image came from. Then
release.json is what's trusted, and manifest.json is only for showing. Someone's own build,
such as custom-0.10.1-896b3059, needs no signature: it came from their own folder.

It can also read what a board of someone's own runs back off it, without writing anything, so
there's a copy to put back if an update of it fails (boards.py, Setup's "Choose its folder").
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path

import release
from devices import is_custom

BAUD = "460800"
FLASH_TIMEOUT_S = 180
COMES_BACK_S = 30
MESSAGE_LEN = 120
PROGRESS = re.compile(r"Writing at 0x([0-9a-fA-F]+)\D*?\((\d+) ?%\)")
READ_PROGRESS = re.compile(r"^\d+ \((\d+) ?%\)")  # esptool's read_flash: 65536 (4 %)
READ_TIMEOUT_S = 300  # each read: a big app over a slow serial link takes a while
IMAGE_MAGIC = 0xE9  # the first byte of a bootloader or app image
ENTRY = struct.Struct("<2sBBII16sI")  # a partition table entry
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


def make_image(build: Path, out: Path, secret: bytes | None = None) -> str:
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
    (out / "release.json").unlink(missing_ok=True)
    if secret:
        if is_custom(version):
            raise ValueError(f"{version} is someone's own build: only ttycat's releases are signed")
        (out / "release.json").write_text(json.dumps(release.seal_firmware(manifest, secret), indent=1) + "\n")
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


def read_image(folder: str, keys: dict[str, bytes] | None = None) -> dict:
    """The manifest, once everything in it is known to be sound. Raises ValueError otherwise.
    `keys` are the ones a signed image may be signed by: release_keys.json's, unless given."""
    root = Path(folder)
    signed = (root / "release.json").exists()
    try:
        manifest = release.open_firmware(root / "release.json", keys) if signed else \
            json.loads((root / "manifest.json").read_text())
        version, files, flash = manifest["version"], manifest["files"], manifest["flash"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise ValueError(f"no usable firmware image: {e}") from e
    if not isinstance(version, str) or (not is_custom(version) and not signed):
        raise ValueError(f"{version} says it's ttycat's own firmware, but the image isn't signed")
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
            "signed": signed,
            "from": source if isinstance(source, dict) else None}


def app_partition(table: bytes) -> tuple[int, int]:
    """Where the app the board starts is, from its partition table: offset and size. Raises ValueError."""
    apps = []
    for at in range(0, len(table) - ENTRY.size + 1, ENTRY.size):
        magic, kind, _, offset, size, _, _ = ENTRY.unpack_from(table, at)
        if magic != b"\xaa\x50":
            break
        if kind == 0:
            apps.append((offset, size))
    if len(apps) != 1:
        raise ValueError("its partition table has no app" if not apps else
                         "it has more than one app, so which one it starts can't be told")
    return apps[0]


class Flasher:
    """idle -> flashing -> restarting -> done, or failed from either. Never blocks the daemon.
    Reading what a board runs: idle -> reading -> restarting -> done, writing nothing."""

    def __init__(self, log=print, tool=find_esptool, keys: dict[str, bytes] | None = None):
        self.log = log
        self.tool = tool
        self.keys = keys  # who ttycat's own images may be signed by: release_keys.json's, unless given
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
        self.job = "write"  # or "read"
        self.reading: dict = {}  # a read's plan: where things are, and which step it's on

    @property
    def busy(self) -> bool:
        return self.state in ("flashing", "reading", "restarting")

    @property
    def holds_port(self) -> bool:
        return self.state in ("flashing", "reading")

    def info(self) -> dict:
        return {"state": self.state, "pct": self.pct, "msg": self.msg, "to": self.to, "tool": self.tool() is not None,
                "job": self.job}

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
            image = read_image(folder, self.keys)
        except ValueError as e:
            return False, str(e)
        if board and image["board"] and board != image["board"]:
            return False, f"this firmware is for {image['board']}, and this board is {board}"
        command = [*tool, "--chip", image["chip"], "-p", port, "-b", BAUD, "--before", "default_reset", "--after", "hard_reset",
                   "write_flash", *image["flash"]]
        for f in image["files"]:
            command += [f["offset"], f["path"]]
        self.files, self.written, self.partial, self.last_lines = image["files"], {}, b"", []
        self.to, self.pct, self.msg, self.job = image["version"], 0, "", "write"
        self.image, self.source = folder, image["from"]  # what's being written, and where it was built from
        ok, msg = self._spawn(command)
        if not ok:
            return False, msg
        self.state = "flashing"
        self.deadline = time.monotonic() + FLASH_TIMEOUT_S
        self.log("firmware: writing", image["version"], "to", port, f"(board had {fw})")
        return True, ""

    def start_read(self, port: str | None, fw: str | None, board: str | None, where: dict, out: Path,
                   source: dict) -> tuple[bool, str]:
        """Reads what a board of someone's own runs into an image in `out`, the same as one made from its
        build, with `source` as where it came from. Writes nothing to the board. `where` is boards.layout's:
        the chip, and the bootloader's and partition table's offsets."""
        if self.busy:
            return False, "already updating"
        if not port or not fw or not board:
            return False, "no board connected"
        if not is_custom(fw):
            return False, f"{fw} is ttycat's own firmware: there's nothing of yours to keep a copy of"
        tool = self.tool()
        if not tool:
            return False, "esptool not found"
        out = Path(out)
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True)
        self.job, self.to, self.pct, self.msg = "read", fw, 0, ""
        self.image, self.source, self.partial, self.last_lines = str(out), source, b"", []
        self.reading = {**where, "tool": tool, "port": port, "board": board, "out": out, "step": "head"}
        start, size = where["bootloader"], where["table"] + 0x1000 - where["bootloader"]  # the bootloader, then the table
        ok, msg = self._read(start, size, out / "head.bin", last=False)
        if not ok:
            return False, msg
        self.state = "reading"
        self.log("firmware: reading what", port, "runs:", fw)
        return True, ""

    def _read(self, offset: int, size: int, path: Path, last: bool) -> tuple[bool, str]:
        r = self.reading
        command = [*r["tool"], "--chip", r["chip"], "-p", r["port"], "-b", BAUD, "--before", "default_reset",
                   "--after", "hard_reset" if last else "no_reset", "read_flash", hex(offset), hex(size), str(path)]
        self.deadline = time.monotonic() + READ_TIMEOUT_S
        return self._spawn(command)

    def _spawn(self, command: list[str]) -> tuple[bool, str]:
        try:
            self.process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env={**os.environ, "PYTHONUNBUFFERED": "1"}, start_new_session=True)
        except OSError as e:
            self.process = None
            return False, f"couldn't start esptool: {e}"
        self.fd = self.process.stdout.fileno()
        os.set_blocking(self.fd, False)
        return True, ""

    def _read_next(self):
        """A read finished: the next one, or the image they make."""
        r = self.reading
        out = r["out"]
        try:
            if r["step"] == "head":
                head = (out / "head.bin").read_bytes()
                split = r["table"] - r["bootloader"]
                r["bootloader_bin"], r["table_bin"] = head[:split], head[split:]
                if not r["bootloader_bin"] or r["bootloader_bin"][0] != IMAGE_MAGIC:
                    raise ValueError(f"there's no bootloader at {hex(r['bootloader'])}")
                r["app"], size = app_partition(r["table_bin"])
                r["step"] = "app"
                ok, msg = self._read(r["app"], size, out / "app.read", last=True)
                if not ok:
                    raise ValueError(msg)
                return
            app = (out / "app.read").read_bytes()
            if not app or app[0] != IMAGE_MAGIC:
                raise ValueError(f"there's no app at {hex(r['app'])}")
        except (OSError, ValueError) as e:
            return self._fail(f"Couldn't read the board's firmware: {e}")
        files = []
        for name, offset, data in (("bootloader.bin", r["bootloader"], r["bootloader_bin"]),
                                   ("partition-table.bin", r["table"], r["table_bin"]), ("app.bin", r["app"], app)):
            data = data.rstrip(b"\xff")  # what's left of the area: erased flash reads 0xff, and writing erases first
            (out / name).write_bytes(data)
            files.append({"offset": hex(offset), "path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        for scratch in ("head.bin", "app.read"):
            (out / scratch).unlink(missing_ok=True)
        # No flash settings: esptool then keeps the ones in the bootloader it writes, this one.
        manifest = {"version": self.to, "chip": r["chip"], "board": r["board"], "flash": [], "files": files,
                    "from": self.source}
        (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
        self.state, self.pct = "restarting", 100
        self.deadline = time.monotonic() + COMES_BACK_S
        self.log("firmware: read", sum(f["size"] for f in files), "bytes, waiting for the board")

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
            *lines, self.partial = re.split(rb"[\r\n\b]", self.partial + data)  # read_flash moves back with \b
            for line in lines:
                self._line(line.decode(errors="replace").strip())
            return
        self._line(self.partial.decode(errors="replace").strip())
        self.partial = b""
        self._ended(self.process.wait())

    def _line(self, line: str):
        if not line:
            return
        read = READ_PROGRESS.match(line) if self.job == "read" else None
        if read:  # the head is small: the app is most of it
            pct = int(read.group(1))
            self.pct = min(99, pct * 5 // 100 if self.reading.get("step") == "head" else 5 + pct * 94 // 100)
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
        if status == 0 and self.job == "read":
            self._read_next()
        elif status == 0:
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
            self.log("firmware: board now runs" if self.job == "write" else "firmware: board back, running", fw)
            return True
        self._fail(f"the board came back running {fw}, not {self.to}")
        return False

    def tick(self):
        if self.state in ("flashing", "reading") and time.monotonic() >= self.deadline:
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
