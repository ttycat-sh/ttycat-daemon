"""Boards of people's own, registered so the app can build them new releases of the core.

A board's folder is the person's own code, and never comes into the core. Registering it says
where it is, where the ESP-IDF that builds it is, and which build of it the board runs now, kept
as an image so it can always go back on. That image is the build that was flashed through the
daemon, or else what the board runs, read back off it (Setup's "Choose its folder"). Each board
has a folder in ~/.ttycat/boards:
  NAME/board.json    {"folder", "idf", "fw", "hash"}
  NAME/current/      the image the board runs: fw
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path

from flasher import make_image, read_image

BOARDS_DIR = Path.home() / ".ttycat" / "boards"
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
API = re.compile(r"^set\(BOARD_API ([0-9]+)\)", re.M)
CUSTOM = re.compile(r"(.+)-([0-9]+(?:\.[0-9]+)*)-([0-9a-f]{8})")
TARGET = re.compile(r'^CONFIG_IDF_TARGET="(esp32[a-z0-9]*)"', re.M)
TABLE_AT = re.compile(r"^CONFIG_PARTITION_TABLE_OFFSET=(0x[0-9a-fA-F]+)", re.M)
# Where each chip's ROM looks for the bootloader, as ESP-IDF builds it. Every other chip: 0x0.
BOOTLOADER_AT = {"esp32": 0x1000, "esp32s2": 0x1000, "esp32c5": 0x2000, "esp32p4": 0x2000}


def core_of(fw: str | None) -> str | None:
    """The core release a custom build was made from: custom-0.10.1-896b3059 is 0.10.1."""
    found = CUSTOM.fullmatch(fw or "")
    return found.group(2) if found else None


def board_api(folder: Path) -> int | None:
    """The board interface a folder says it was made for."""
    try:
        found = API.search((Path(folder) / "CMakeLists.txt").read_text())
    except OSError:
        return None
    return int(found.group(1)) if found else None


def folder_hash(folder: Path) -> str:
    """Every file in it, so any change at all counts. Hidden files, such as .git, don't."""
    h = hashlib.sha256()
    root = Path(folder)
    for here, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(names):
            if name.startswith("."):
                continue
            path = Path(here) / name
            h.update(path.relative_to(root).as_posix().encode() + b"\0")
            h.update(hashlib.sha256(path.read_bytes()).digest())
    return h.hexdigest()


def idf_version(idf: Path) -> str | None:
    """5.5.6, from the ESP-IDF folder itself."""
    try:
        text = (Path(idf) / "tools" / "cmake" / "version.cmake").read_text()
    except OSError:
        return None
    parts = [re.search(rf"IDF_VERSION_{p} ([0-9]+)", text) for p in ("MAJOR", "MINOR", "PATCH")]
    return ".".join(m.group(1) for m in parts) if all(parts) else None


def find_idf() -> Path | None:
    """The newest ESP-IDF in ~/esp, where Espressif's installer and the skills put it."""
    found = sorted(Path.home().glob("esp/esp-idf*/export.sh"), reverse=True)
    return found[0].parent if found else None


def check_folder(folder: Path, idf: Path | None = None) -> tuple[Path, Path]:
    """A board's folder that ttycat can build, and the ESP-IDF to build it with. Raises ValueError."""
    folder = Path(folder).expanduser().resolve()
    if not NAME.fullmatch(folder.name):
        raise ValueError(f"a board's folder is named in lowercase letters, numbers and dashes, not {folder.name}")
    if not (folder / "CMakeLists.txt").exists() or not (folder / "board.c").exists():
        raise ValueError(f"{folder} isn't a board's folder: it has no CMakeLists.txt and board.c")
    if board_api(folder) is None:
        raise ValueError(f"{folder}/CMakeLists.txt doesn't say which board interface it was made for: "
                         "add set(BOARD_API 1), as the template has")
    idf = Path(idf).expanduser().resolve() if idf else find_idf()
    if not idf or not (idf / "export.sh").exists() or not idf_version(idf):
        raise ValueError("no ESP-IDF found: install it in ~/esp, as the new-board skill does")
    return folder, idf


def built_with(build: Path, board: str) -> Path | None:
    """A build folder, which people pick easily since it's named after the board too: the board folder
    ESP-IDF recorded building it with. None if it isn't a build of that board."""
    try:
        about = json.loads((Path(build) / "project_description.json").read_text())
        folder = about["build_component_info"][board]["dir"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return Path(folder) if isinstance(folder, str) and Path(folder).is_dir() else None


def layout(folder: Path) -> dict:
    """Where a build of this folder sits in flash: its chip, and the bootloader's and partition
    table's offsets. From its sdkconfig.defaults, which the build requires to name the chip."""
    try:
        text = (Path(folder) / "sdkconfig.defaults").read_text()
    except OSError:
        text = ""
    chip = TARGET.search(text)
    if not chip:
        raise ValueError(f"{folder}/sdkconfig.defaults doesn't say which chip it's for (CONFIG_IDF_TARGET)")
    table = TABLE_AT.search(text)
    return {"chip": chip.group(1), "bootloader": BOOTLOADER_AT.get(chip.group(1), 0),
            "table": int(table.group(1), 16) if table else 0x8000}


class Boards:
    def __init__(self, root: Path = BOARDS_DIR):
        self.root = Path(root)

    def get(self, name: str | None) -> dict | None:
        """The registration, or nil if there's none or it can't be read."""
        if not name or not NAME.fullmatch(name):
            return None
        try:
            reg = json.loads((self.root / name / "board.json").read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(reg, dict) or not all(isinstance(reg.get(k), str) for k in ("folder", "idf", "fw", "hash")):
            return None
        return {**reg, "name": name, "image": str(self.root / name / "current")}

    def register(self, folder: Path, build: Path, idf: Path | None = None) -> dict:
        """The board built from `folder` into `build`, which is what it runs now. Raises ValueError."""
        folder, idf = check_folder(folder, idf)
        name = folder.name
        mine = self.root / name
        mine.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = mine / "current.new"
        shutil.rmtree(temp, ignore_errors=True)
        fw = make_image(Path(build), temp)
        image = read_image(str(temp))
        if image["board"] != name:
            shutil.rmtree(temp)
            raise ValueError(f"that build is for {image['board']}, not {name}")
        shutil.rmtree(mine / "current", ignore_errors=True)
        temp.rename(mine / "current")
        self._save(name, {"folder": str(folder), "idf": str(idf), "fw": fw, "hash": folder_hash(folder)})
        return self.get(name)

    def flashed(self, image: Path) -> dict:
        """Someone's own build is on the board, and said hello: it's the one to update from, and to go
        back to. Its image says where it was built from (flasher.built_from), or, read back off the
        board, which folder the person chose for it. Raises ValueError."""
        found = read_image(str(image))
        source, name, fw = found["from"], found["board"], found["version"]
        if not source or not core_of(fw):
            raise ValueError("it doesn't say where it was built from")
        folder, idf = Path(source["folder"]), Path(source["idf"])
        if not NAME.fullmatch(name or "") or folder.name != name:
            raise ValueError(f"its folder, {folder}, isn't named after the board, {name}")
        if board_api(folder) is None:
            raise ValueError(f"{folder}/CMakeLists.txt doesn't say which board interface it was made for")
        if not idf_version(idf):
            raise ValueError(f"no ESP-IDF at {idf}")
        mine = self.root / name
        mine.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = mine / "current.new"
        shutil.rmtree(temp, ignore_errors=True)
        shutil.copytree(image, temp)
        shutil.rmtree(mine / "current", ignore_errors=True)
        temp.rename(mine / "current")
        self._save(name, {"folder": str(folder), "idf": str(idf), "fw": fw, "hash": source["hash"]})
        return self.get(name)

    def built(self, name: str, image: Path, fw: str, hash_: str):
        """A new build is on the board: it's the one to go back to from now on."""
        mine = self.root / name
        shutil.rmtree(mine / "previous", ignore_errors=True)
        if (mine / "current").exists():
            (mine / "current").rename(mine / "previous")
        shutil.copytree(image, mine / "current")
        shutil.rmtree(mine / "previous", ignore_errors=True)
        reg = self.get(name) or {}
        self._save(name, {"folder": reg.get("folder", ""), "idf": reg.get("idf", ""), "fw": fw, "hash": hash_})

    def _save(self, name: str, reg: dict):
        path = self.root / name / "board.json"
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(reg, indent=1))
        os.replace(temp, path)
