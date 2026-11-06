"""Signed releases: ttycat's firmware source, so the Mac can build it for boards of people's own,
and each official board's firmware. Both are signed the same way, and checked the same way.

A release is two files side by side:
  ttycat-core-0.11.0.tar.gz   the firmware folder, without builds
  ttycat-core-0.11.0.json     {"body": "...", "key": "...", "sig": "..."}

`body` is the release's description as JSON text, and `sig` is an Ed25519 signature of exactly
those bytes, by the key named in `key`. Only keys in release_keys.json are trusted, so wherever a
release comes from, the app or a server, nobody but whoever holds that key can make one it builds.
The body names the archive's size and SHA-256, so the archive is covered by the signature too.
tools/release_core.py makes them.

An official board's firmware is an image folder (flasher.py) with a release.json in the same
envelope, whose body is the image's manifest with "kind": "ttycat-firmware". tools/firmware_bundle.py
--sign makes one. The manifest names each file's size and SHA-256, so the files are covered too.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import tarfile
from pathlib import Path

import ed25519

KIND = "ttycat-core"
FIRMWARE_KIND = "ttycat-firmware"
KEYS_FILE = Path(__file__).with_name("release_keys.json")
DEV_KEY_ENV = "TTYCAT_TRUST_KEY"  # a key to trust while developing, never in a release
VERSION = re.compile(r"[0-9]+(\.[0-9]+){1,3}")
IDF = re.compile(r"[0-9]+\.[0-9]+")
FILE = re.compile(r"ttycat-core-[0-9.]+\.tar\.gz")
MAX_SOURCE = 32 * 1024 * 1024


def key_id(public: bytes) -> str:
    return hashlib.sha256(public).hexdigest()[:16]


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(n) for n in version.split("."))


def newer(a: str, b: str) -> bool:
    return version_key(a) > version_key(b)


def trusted_keys(path: Path = KEYS_FILE) -> dict[str, bytes]:
    """By key id. Anything unreadable trusts nothing."""
    keys = {}
    try:
        listed = json.loads(path.read_text()).get("keys", [])
    except (OSError, ValueError, AttributeError):
        listed = []
    for entry in listed if isinstance(listed, list) else []:
        try:
            public = base64.b64decode(entry["key"], validate=True)
        except (KeyError, TypeError, ValueError):
            continue
        if len(public) == 32:
            keys[key_id(public)] = public
    dev = os.environ.get(DEV_KEY_ENV)
    if dev:
        try:
            public = base64.b64decode(dev, validate=True)
            if len(public) == 32:
                keys[key_id(public)] = public
        except ValueError:
            pass
    return keys


def seal(body: dict, secret: bytes) -> dict:
    """Signs a release's body. For tools/release_core.py."""
    text = json.dumps(body, sort_keys=True, indent=1)
    public = ed25519.public_key(secret)
    signature = ed25519.sign(secret, text.encode())
    return {"body": text, "key": key_id(public), "sig": base64.b64encode(signature).decode()}


def seal_firmware(manifest: dict, secret: bytes) -> dict:
    """Signs an official board's image: its manifest, as flasher.make_image writes it."""
    return seal({**manifest, "kind": FIRMWARE_KIND}, secret)


def open_firmware(path: Path, keys: dict[str, bytes] | None = None) -> dict:
    """The manifest an image's release.json signs, once its signature checks out. The image's files
    are checked against it by flasher.read_image. Raises ValueError."""
    body = _signed_body(Path(path), keys)
    if not isinstance(body, dict) or body.get("kind") != FIRMWARE_KIND:
        raise ValueError(f"{Path(path).name} isn't a firmware release")
    return {k: v for k, v in body.items() if k != "kind"}


def _signed_body(path: Path, keys: dict[str, bytes] | None):
    """What `path` signs, parsed, once the signature is one of ours. Raises ValueError."""
    try:
        envelope = json.loads(path.read_text())
        text, kid, sig = envelope["body"], envelope["key"], envelope["sig"]
        signature = base64.b64decode(sig, validate=True)
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise ValueError(f"{path.name} isn't a release: {e}") from e
    if not isinstance(text, str):
        raise ValueError(f"{path.name} isn't a release")
    public = (trusted_keys() if keys is None else keys).get(kid)
    if public is None:
        raise ValueError(f"{path.name} is signed by a key this app doesn't trust")
    if not ed25519.verify(public, text.encode(), signature):
        raise ValueError(f"{path.name} has a signature that doesn't match it")
    try:
        return json.loads(text)
    except ValueError as e:
        raise ValueError(f"{path.name} isn't a release: {e}") from e


def open_release(path: Path, keys: dict[str, bytes] | None = None) -> dict:
    """The release described in `path`, once its signature and its archive have been checked.
    Raises ValueError, with a reason someone could act on, for anything else."""
    path = Path(path)
    body = _signed_body(path, keys)
    # Signed by us, so from here on a problem is a mistake, not an attack. Still checked.
    try:
        kind, version, api, idf = body["kind"], body["version"], body["board_api"], body["idf"]
        source = body["source"]
        name, size, digest = source["file"], source["size"], source["sha256"]
    except (KeyError, TypeError) as e:
        raise ValueError(f"{path.name} is missing something: {e}") from e
    if kind != KIND or not isinstance(version, str) or not VERSION.fullmatch(version):
        raise ValueError(f"{path.name} isn't a core release")
    if not isinstance(api, int) or isinstance(api, bool) or api < 1:
        raise ValueError(f"{path.name} has no board interface")
    if not isinstance(idf, str) or not IDF.fullmatch(idf):
        raise ValueError(f"{path.name} doesn't say which ESP-IDF")
    if not isinstance(name, str) or not FILE.fullmatch(name) or not isinstance(size, int) or not isinstance(digest, str):
        raise ValueError(f"{path.name} names its archive wrongly")
    archive = path.with_name(name)
    if size > MAX_SOURCE:
        raise ValueError(f"{name} is too big")
    h = hashlib.sha256()
    try:
        with open(archive, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        actual = archive.stat().st_size
    except OSError as e:
        raise ValueError(f"{name} is missing") from e
    if actual != size or h.hexdigest() != digest:
        raise ValueError(f"{name} doesn't match its release")
    return {"version": version, "board_api": api, "idf": idf, "date": body.get("date"),
            "archive": str(archive), "sha256": digest, "path": str(path)}


class Releases:
    """Every release in some folders that checks out, read again only when a file changes."""

    def __init__(self, folders: list[Path], keys: dict[str, bytes] | None = None):
        self.folders = folders
        self.keys = keys
        self.seen: dict[str, tuple[tuple, dict | None, str]] = {}

    def _check(self, path: Path) -> tuple[dict | None, str]:
        try:
            st, archive = path.stat(), None
            try:
                archive = path.with_name(json.loads(json.loads(path.read_text())["body"])["source"]["file"]).stat()
            except (OSError, ValueError, KeyError, TypeError):
                pass
            stamp = (st.st_mtime_ns, st.st_size, archive and (archive.st_mtime_ns, archive.st_size))
        except OSError:
            return None, "gone"
        cached = self.seen.get(str(path))
        if cached and cached[0] == stamp:
            return cached[1], cached[2]
        try:
            found, why = open_release(path, self.keys), ""
        except ValueError as e:
            found, why = None, str(e)
        self.seen[str(path)] = (stamp, found, why)
        return found, why

    def all(self) -> tuple[list[dict], list[str]]:
        """The good ones, oldest first, and why each other one was left out."""
        good, bad = [], []
        for folder in self.folders:
            for path in sorted(Path(folder).glob("ttycat-core-*.json")):
                found, why = self._check(path)
                if found:
                    good.append(found)
                else:
                    bad.append(why)
        good.sort(key=lambda r: version_key(r["version"]))
        return good, bad

    def best(self, board_api: int | None = None) -> dict | None:
        """The newest, or the newest a folder made for `board_api` can build."""
        good = [r for r in self.all()[0] if board_api is None or r["board_api"] == board_api]
        return good[-1] if good else None


def unpack(release: dict, into: Path, keys: dict[str, bytes] | None = None) -> Path:
    """The release's source, unpacked once into `into`/VERSION. Checked again first: it's what gets built."""
    again = open_release(Path(release["path"]), keys)
    target = Path(into) / again["version"]
    stamp = target / ".sha256"
    if stamp.exists() and stamp.read_text() == again["sha256"]:
        return target / "firmware"
    shutil.rmtree(target, ignore_errors=True)
    temp = Path(into) / f".{again['version']}.unpacking"
    shutil.rmtree(temp, ignore_errors=True)
    temp.mkdir(parents=True)
    with tarfile.open(again["archive"], "r:gz") as tar:
        for member in tar.getmembers():
            parts = Path(member.name).parts
            if not parts or parts[0] != "firmware" or ".." in parts or member.name.startswith("/"):
                raise ValueError(f"{member.name} is outside the release's firmware folder")
            if not (member.isfile() or member.isdir()):
                raise ValueError(f"{member.name} isn't a plain file")
        tar.extractall(temp, filter="data")
    (temp / ".sha256").write_text(again["sha256"])
    temp.rename(target)
    return target / "firmware"
