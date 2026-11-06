"""Boards of people's own, registered so the app can build them new releases of the core.

A board's folder is the person's own code, and never comes into the core. Registering it says
where it is, where the ESP-IDF that builds it is, and which build of it the board runs now, kept
as an image so it can always go back on. Each board has a folder in ~/.ttycat/boards:
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
        folder = Path(folder).expanduser().resolve()
        name = folder.name
        if not NAME.fullmatch(name):
            raise ValueError(f"a board's folder is named in lowercase letters, numbers and dashes, not {name}")
        if not (folder / "CMakeLists.txt").exists() or not (folder / "board.c").exists():
            raise ValueError(f"{folder} isn't a board's folder: it has no CMakeLists.txt and board.c")
        if board_api(folder) is None:
            raise ValueError(f"{folder}/CMakeLists.txt doesn't say which board interface it was made for: "
                             "add set(BOARD_API 1), as the template has")
        idf = Path(idf).expanduser().resolve() if idf else find_idf()
        if not idf or not (idf / "export.sh").exists() or not idf_version(idf):
            raise ValueError("no ESP-IDF found: pass --idf with its folder")
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
        back to. Its image says where it was built from (flasher.built_from). Raises ValueError."""
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
