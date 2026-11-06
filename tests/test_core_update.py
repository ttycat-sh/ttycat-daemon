"""Core updates for boards of people's own: only signed releases, and the old build goes back on failure."""
import base64
import hashlib
import json
import io
import os
import select
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ed25519
import release
from boards import Boards, board_api, core_of, folder_hash
from core_update import Updater
from flasher import make_image, read_image

SECRET = bytes(range(32))
PUBLIC = ed25519.public_key(SECRET)
KEYS = {release.key_id(PUBLIC): PUBLIC}
OTHER = bytes(range(1, 33))

# A stand-in for ESP-IDF: its export.sh puts an idf.py first in PATH that "builds" in an instant.
FAKE_IDF = r'''
mkdir -p "$B"
version=$(sed -n 's/^#define FW_VERSION "\(.*\)"/\1/p' "$C/main/model.h")
board=$(basename "$D")
echo "[1/2] compiling"
echo "[ 37%] Building C object"
if [ -f "$D/broken" ]; then echo "board.c:1: error: it's broken"; exit 1; fi
printf '%s' '{"flash_files":{"0x0":"bootloader.bin","0x10000":"ttycat.bin"},"write_flash_args":["--flash_mode","dio"],"extra_esptool_args":{"chip":"esp32c5"}}' > "$B/flasher_args.json"
printf 'boot' > "$B/bootloader.bin"
printf '{"t":"hello","fw":"custom-%s-0000000%s","b":"%s","p":1,"art":1}' "$version" "${#version}" "$board" > "$B/ttycat.bin"
echo "[2/2] linking"
'''


def make_release(folder: Path, version: str, api=1, idf="5.5", secret=SECRET) -> Path:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for name, text in {"firmware/main/model.h": f'#define FW_VERSION "{version}"\n',
                           "firmware/CMakeLists.txt": "project(ttycat)\n"}.items():
            info = tarfile.TarInfo(name)
            info.size = len(text)
            tar.addfile(info, io.BytesIO(text.encode()))
    data = raw.getvalue()
    name = f"ttycat-core-{version}.tar.gz"
    (folder / name).write_bytes(data)
    body = {"kind": "ttycat-core", "version": version, "board_api": api, "idf": idf, "date": "2026-11-06",
            "source": {"file": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}}
    path = folder / f"ttycat-core-{version}.json"
    path.write_text(json.dumps(release.seal(body, secret)))
    return path


def make_build(folder: Path, fw: str, board: str):
    folder.mkdir(parents=True)
    (folder / "flasher_args.json").write_text(json.dumps({
        "flash_files": {"0x0": "bootloader.bin", "0x10000": "ttycat.bin"},
        "write_flash_args": ["--flash_mode", "dio"], "extra_esptool_args": {"chip": "esp32c5"}}))
    (folder / "bootloader.bin").write_bytes(b"boot")
    (folder / "ttycat.bin").write_text(json.dumps({"t": "hello", "fw": fw, "b": board, "p": 1}, separators=(",", ":")))


class Devices:
    def __init__(self, allowed=()):
        self.allowed = set(allowed)

    def may_approve(self, fw):
        return fw in self.allowed

    def set_approve(self, fw, on):
        (self.allowed.add if on else self.allowed.discard)(fw)


class Flasher:
    """What the updater reads of the real one."""
    def __init__(self):
        self.state, self.to, self.msg = "idle", None, ""

    @property
    def busy(self):
        return self.state in ("flashing", "restarting")


class Signed(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_rfc_8032_vectors(self):
        secret = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
        self.assertEqual(ed25519.public_key(secret).hex(), "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
        sig = bytes.fromhex("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
        self.assertEqual(ed25519.sign(secret, b""), sig)
        self.assertTrue(ed25519.verify(ed25519.public_key(secret), b"", sig))
        secret = bytes.fromhex("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb")
        sig = bytes.fromhex("92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00")
        self.assertEqual(ed25519.sign(secret, b"\x72"), sig)
        self.assertFalse(ed25519.verify(ed25519.public_key(secret), b"\x73", sig))

    def test_a_signature_only_holds_for_its_own_message_and_key(self):
        sig = ed25519.sign(SECRET, b"core 0.11.0")
        self.assertTrue(ed25519.verify(PUBLIC, b"core 0.11.0", sig))
        self.assertFalse(ed25519.verify(PUBLIC, b"core 0.11.1", sig))
        self.assertFalse(ed25519.verify(ed25519.public_key(OTHER), b"core 0.11.0", sig))
        bad = bytearray(sig)
        bad[40] ^= 1
        self.assertFalse(ed25519.verify(PUBLIC, b"core 0.11.0", bytes(bad)))
        # s at or above the group's order is the same signature written another way: never accepted
        s = int.from_bytes(sig[32:], "little") + ed25519.L
        self.assertFalse(ed25519.verify(PUBLIC, b"core 0.11.0", sig[:32] + s.to_bytes(32, "little")))
        self.assertFalse(ed25519.verify(PUBLIC, b"core 0.11.0", sig[:63]))

    def test_a_good_release_opens(self):
        found = release.open_release(make_release(self.dir, "0.11.0"), KEYS)
        self.assertEqual((found["version"], found["board_api"], found["idf"]), ("0.11.0", 1, "5.5"))

    def test_only_a_trusted_key_counts(self):
        path = make_release(self.dir, "0.11.0", secret=OTHER)
        with self.assertRaisesRegex(ValueError, "doesn't trust"):
            release.open_release(path, KEYS)
        with self.assertRaisesRegex(ValueError, "doesn't trust"):
            release.open_release(path, {})

    def test_a_changed_body_fails(self):
        path = make_release(self.dir, "0.11.0")
        envelope = json.loads(path.read_text())
        envelope["body"] = envelope["body"].replace("0.11.0", "0.12.0")
        path.write_text(json.dumps(envelope))
        with self.assertRaisesRegex(ValueError, "signature"):
            release.open_release(path, KEYS)

    def test_a_signature_from_a_trusted_key_over_another_body_fails(self):
        path = make_release(self.dir, "0.11.0")
        other = json.loads(make_release(self.dir, "0.12.0").read_text())
        envelope = json.loads(path.read_text())
        envelope["sig"] = other["sig"]
        path.write_text(json.dumps(envelope))
        with self.assertRaisesRegex(ValueError, "signature"):
            release.open_release(path, KEYS)

    def test_a_changed_archive_fails(self):
        path = make_release(self.dir, "0.11.0")
        archive = self.dir / "ttycat-core-0.11.0.tar.gz"
        archive.write_bytes(archive.read_bytes() + b"\0")
        with self.assertRaisesRegex(ValueError, "doesn't match"):
            release.open_release(path, KEYS)
        archive.unlink()
        with self.assertRaisesRegex(ValueError, "missing"):
            release.open_release(path, KEYS)

    def test_the_newest_good_one_is_offered(self):
        make_release(self.dir, "0.11.0")
        make_release(self.dir, "0.12.0", api=2)
        make_release(self.dir, "0.13.0", secret=OTHER)
        found = release.Releases([self.dir, self.dir / "nowhere"], KEYS)
        good, bad = found.all()
        self.assertEqual([r["version"] for r in good], ["0.11.0", "0.12.0"])
        self.assertEqual(len(bad), 1)
        self.assertEqual(found.best()["version"], "0.12.0")
        self.assertEqual(found.best(board_api=1)["version"], "0.11.0")

    def test_trusted_keys_come_from_the_file(self):
        keys = self.dir / "keys.json"
        keys.write_text(json.dumps({"keys": [{"key": base64.b64encode(PUBLIC).decode()}, {"key": "nonsense"}]}))
        self.assertEqual(release.trusted_keys(keys), KEYS)
        keys.write_text("not json")
        self.assertEqual(release.trusted_keys(keys), {})

    def test_unpacking_keeps_inside_its_folder(self):
        found = release.open_release(make_release(self.dir, "0.11.0"), KEYS)
        source = release.unpack(found, self.dir / "src", KEYS)
        self.assertIn("0.11.0", (source / "main" / "model.h").read_text())
        evil = io.BytesIO()
        with tarfile.open(fileobj=evil, mode="w:gz") as tar:
            info = tarfile.TarInfo("firmware/../../escaped")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        data = evil.getvalue()
        (self.dir / "ttycat-core-0.12.0.tar.gz").write_bytes(data)
        body = {"kind": "ttycat-core", "version": "0.12.0", "board_api": 1, "idf": "5.5",
                "source": {"file": "ttycat-core-0.12.0.tar.gz", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}}
        (self.dir / "ttycat-core-0.12.0.json").write_text(json.dumps(release.seal(body, SECRET)))
        found = release.open_release(self.dir / "ttycat-core-0.12.0.json", KEYS)
        with self.assertRaisesRegex(ValueError, "outside"):
            release.unpack(found, self.dir / "src", KEYS)
        self.assertFalse((self.dir / "escaped").exists())


class Registered(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.folder = self.dir / "mine" / "my-board"
        self.folder.mkdir(parents=True)
        (self.folder / "CMakeLists.txt").write_text("set(BOARD_API 1)\nidf_component_register()\n")
        (self.folder / "board.c").write_text("// mine\n")
        self.idf = self.dir / "esp-idf"
        (self.idf / "tools" / "cmake").mkdir(parents=True)
        (self.idf / "tools" / "cmake" / "version.cmake").write_text(
            "set(IDF_VERSION_MAJOR 5)\nset(IDF_VERSION_MINOR 5)\nset(IDF_VERSION_PATCH 6)\n")
        (self.idf / "bin").mkdir()
        (self.idf / "bin" / "idf.py").write_text(
            '#!/bin/sh\nwhile [ $# -gt 1 ]; do case "$1" in -C) C=$2; shift 2;; -B) B=$2; shift 2;; '
            '-D) D=${2#BOARD_DIR=}; shift 2;; *) shift;; esac; done\n' + FAKE_IDF)
        os.chmod(self.idf / "bin" / "idf.py", 0o755)
        (self.idf / "export.sh").write_text(f'export PATH="{self.idf}/bin:$PATH"\n')
        make_build(self.dir / "build", "custom-0.10.1-896b3059", "my-board")
        self.boards = Boards(self.dir / "boards")
        self.releases = self.dir / "releases"
        self.releases.mkdir()

    def register(self):
        return self.boards.register(self.folder, self.dir / "build", self.idf)

    def updater(self, devices=None):
        return Updater(self.boards, release.Releases([self.releases], KEYS), devices or Devices(),
                       log=lambda *a: None, work=self.dir / "work", keys=KEYS)

    def build(self, up: Updater):
        deadline = time.monotonic() + 20
        while up.fd is not None and time.monotonic() < deadline:
            select.select([up.fd], [], [], 1)
            up.on_output()

    def test_what_a_custom_build_was_made_from(self):
        self.assertEqual(core_of("custom-0.10.1-896b3059"), "0.10.1")
        self.assertEqual(core_of("mine-0.12.0-abcdef01"), "0.12.0")
        self.assertIsNone(core_of("0.10.1"))
        self.assertIsNone(core_of(None))

    def test_a_folder_says_its_interface_and_any_change_counts(self):
        self.assertEqual(board_api(self.folder), 1)
        before = folder_hash(self.folder)
        (self.folder / ".git").mkdir()
        (self.folder / ".git" / "HEAD").write_text("x")
        self.assertEqual(folder_hash(self.folder), before)
        (self.folder / "board.c").write_text("// changed\n")
        self.assertNotEqual(folder_hash(self.folder), before)

    def test_registering_keeps_the_build_it_runs(self):
        reg = self.register()
        self.assertEqual((reg["name"], reg["fw"]), ("my-board", "custom-0.10.1-896b3059"))
        self.assertTrue((Path(reg["image"]) / "manifest.json").exists())
        self.assertEqual(self.boards.get("my-board")["hash"], folder_hash(self.folder))
        self.assertIsNone(self.boards.get("someone-else"))
        self.assertIsNone(self.boards.get("../escape"))

    def test_registering_refuses_a_build_of_another_board(self):
        make_build(self.dir / "other", "custom-0.10.1-11111111", "other-board")
        with self.assertRaisesRegex(ValueError, "other-board"):
            self.boards.register(self.folder, self.dir / "other", self.idf)

    def test_registering_needs_the_interface_number(self):
        (self.folder / "CMakeLists.txt").write_text("idf_component_register()\n")
        with self.assertRaisesRegex(ValueError, "BOARD_API"):
            self.register()

    def test_offered_only_when_newer_and_it_can_be_built(self):
        self.register()
        up = self.updater()
        fw = "custom-0.10.1-896b3059"
        self.assertIsNone(up.offer("my-board", fw)["available"])
        make_release(self.releases, "0.10.1")
        self.assertIsNone(up.offer("my-board", fw)["available"])
        make_release(self.releases, "0.11.0")
        offer = up.offer("my-board", fw)
        self.assertEqual((offer["available"], offer["blocked"], offer["registered"]), ("0.11.0", "", True))
        self.assertIn("register", up.offer("my-board", "custom-0.10.1-00000000")["blocked"])
        self.assertFalse(up.offer("unknown-board", "custom-0.10.1-00000000")["registered"])
        self.assertIsNone(up.offer("my-board", "0.10.1")["available"])  # ttycat's own: the app's image updates it

    def test_a_release_for_another_interface_or_esp_idf_asks_for_help(self):
        self.register()
        make_release(self.releases, "0.11.0", api=2)
        self.assertIn("folder changed", self.updater().offer("my-board", "custom-0.10.1-896b3059")["blocked"])
        make_release(self.releases, "0.12.0", idf="6.0")
        self.assertIn("ESP-IDF 6.0", self.updater().offer("my-board", "custom-0.10.1-896b3059")["blocked"])

    def test_an_update_builds_writes_and_carries_approving_over(self):
        self.register()
        make_release(self.releases, "0.11.0")
        devices = Devices({"custom-0.10.1-896b3059"})
        up, flasher, written = self.updater(devices), Flasher(), []
        ok, msg = up.start("my-board", "custom-0.10.1-896b3059", "/dev/cu.test",
                           lambda image, port: (written.append((image, port)), (True, ""))[1])
        self.assertTrue(ok, msg)
        self.assertEqual(up.state, "building")
        self.build(up)
        self.assertEqual(up.state, "writing", up.msg)
        self.assertEqual(up.fw, "custom-0.11.0-00000006")
        self.assertEqual(written[0][1], "/dev/cu.test")
        flasher.state, flasher.to = "done", up.fw
        up.tick(flasher, None)
        self.assertEqual(up.state, "done")
        self.assertTrue(devices.may_approve("custom-0.11.0-00000006"))
        self.assertEqual(up.offer("my-board", up.fw)["state"], "done")
        self.assertEqual(up.offer("another-board", "custom-0.10.1-11111111")["state"], "idle")
        reg = self.boards.get("my-board")
        self.assertEqual(reg["fw"], "custom-0.11.0-00000006")
        self.assertEqual(json.loads((Path(reg["image"]) / "manifest.json").read_text())["version"], reg["fw"])

    def test_a_changed_folder_has_to_be_allowed_again(self):
        self.register()
        make_release(self.releases, "0.11.0")
        (self.folder / "board.c").write_text("// changed since\n")
        devices = Devices({"custom-0.10.1-896b3059"})
        up, flasher = self.updater(devices), Flasher()
        up.start("my-board", "custom-0.10.1-896b3059", "/dev/cu.test", lambda image, port: (True, ""))
        self.build(up)
        flasher.state, flasher.to = "done", up.fw
        up.tick(flasher, None)
        self.assertEqual(up.state, "done")
        self.assertFalse(devices.may_approve(up.fw))

    def test_a_build_that_fails_changes_nothing(self):
        self.register()
        make_release(self.releases, "0.11.0")
        (self.folder / "broken").write_text("")
        written = []
        up = self.updater()
        up.start("my-board", "custom-0.10.1-896b3059", "/dev/cu.test", lambda image, port: (written.append(image), (True, ""))[1])
        self.build(up)
        self.assertEqual(up.state, "failed")
        self.assertIn("it's broken", up.msg)
        self.assertEqual(written, [])
        self.assertEqual(self.boards.get("my-board")["fw"], "custom-0.10.1-896b3059")

    def test_a_build_that_doesnt_start_is_replaced_by_the_old_one(self):
        reg = self.register()
        make_release(self.releases, "0.11.0")
        devices = Devices({"custom-0.10.1-896b3059"})
        up, flasher, written = self.updater(devices), Flasher(), []
        up.start("my-board", "custom-0.10.1-896b3059", "/dev/cu.test",
                 lambda image, port: (written.append(image), (True, ""))[1])
        self.build(up)
        flasher.state, flasher.to, flasher.msg = "failed", up.fw, "the board didn't come back"
        up.tick(flasher, None)
        self.assertEqual(up.state, "putting-back")
        up.tick(flasher, None)
        self.assertEqual(written[-1], reg["image"])  # the build it ran before
        flasher.state, flasher.to = "done", "custom-0.10.1-896b3059"
        up.tick(flasher, None)
        self.assertEqual(up.state, "failed")
        self.assertIn("went back on", up.msg)
        self.assertEqual(self.boards.get("my-board")["fw"], "custom-0.10.1-896b3059")
        self.assertFalse(devices.may_approve("custom-0.11.0-00000006"))
        self.assertTrue(devices.may_approve("custom-0.10.1-896b3059"))

    def describe(self, build: Path, board="my-board", folder=None):
        (build / "project_description.json").write_text(json.dumps({
            "idf_path": str(self.idf), "build_component_info": {board: {"dir": str(folder or self.folder)}}}))

    def test_a_custom_build_says_where_it_came_from_and_a_release_never_does(self):
        self.describe(self.dir / "build")
        make_image(self.dir / "build", self.dir / "img")
        source = read_image(str(self.dir / "img"))["from"]
        self.assertEqual(source, {"folder": str(self.folder), "idf": str(self.idf), "hash": folder_hash(self.folder)})
        make_build(self.dir / "release", "0.10.1", "my-board")
        self.describe(self.dir / "release")
        make_image(self.dir / "release", self.dir / "release-img")
        self.assertIsNone(read_image(str(self.dir / "release-img"))["from"])  # a release carries no paths from this Mac

    def test_flashing_a_custom_build_registers_the_board(self):
        self.describe(self.dir / "build")
        make_image(self.dir / "build", self.dir / "img")
        reg = self.boards.flashed(self.dir / "img")
        self.assertEqual((reg["name"], reg["fw"], reg["folder"]), ("my-board", "custom-0.10.1-896b3059", str(self.folder)))
        self.assertEqual(read_image(reg["image"])["version"], "custom-0.10.1-896b3059")
        make_release(self.releases, "0.11.0")
        self.assertEqual(self.updater().offer("my-board", "custom-0.10.1-896b3059")["blocked"], "")

    def test_a_build_without_its_source_isnt_registered(self):
        make_image(self.dir / "build", self.dir / "img")  # no project_description.json: built some other way
        with self.assertRaisesRegex(ValueError, "where it was built"):
            self.boards.flashed(self.dir / "img")
        self.assertIsNone(self.boards.get("my-board"))

    def test_progress_from_ninja_or_make(self):
        up = self.updater()
        for line, pct in [("[12/80] Building C object", 15), ("[ 37%] Building C object", 37),
                          ("NOTICE: [1/2] idf (5.5.6)", 37), ("[100%] Built target app", 99)]:
            up._line(line)
            self.assertEqual(up.pct, pct, line)

    def test_only_a_registered_board_with_a_newer_release(self):
        up = self.updater()
        self.assertFalse(up.start("my-board", "custom-0.10.1-896b3059", "/dev/x", lambda *a: (True, ""))[0])
        self.register()
        ok, msg = up.start("my-board", "custom-0.10.1-896b3059", "/dev/x", lambda *a: (True, ""))
        self.assertEqual((ok, msg), (False, "there's no newer release"))


if __name__ == "__main__":
    unittest.main()
