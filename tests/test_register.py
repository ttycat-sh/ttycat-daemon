"""Registering a board of one's own from Setup: its folder chosen, what it runs read back off it, nothing
written. With a stand-in for esptool that reads from a file instead of a board."""
import json
import select
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

DAEMON = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DAEMON))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import flasher  # noqa: E402
import release  # noqa: E402
import ttycatd  # noqa: E402
from boards import Boards, layout  # noqa: E402
from core_update import Updater  # noqa: E402
from test_core_update import KEYS, Devices, make_release  # noqa: E402

FW = "custom-0.10.6-0ba51279"

# Reads from flash.bin beside it as if it were the board's flash, and notes every call in calls.log.
FAKE_ESPTOOL = rb"""
import json, sys
from pathlib import Path
here = Path(__file__).parent
args = sys.argv[1:]
with open(here / "calls.log", "a") as log:
    log.write(json.dumps(args) + "\n")
if "read_flash" not in args:
    print("A fatal error occurred: this stand-in only reads")
    sys.exit(2)
at = args.index("read_flash")
offset, size, path = int(args[at + 1], 16), int(args[at + 2], 16), args[at + 3]
print("Connecting....")
for done in (size // 2, size):
    sys.stdout.write(f"{done} ({done * 100 // size} %)" + ("\b" * 12 if done < size else "\n"))
    sys.stdout.flush()
Path(path).write_bytes((here / "flash.bin").read_bytes()[offset:offset + size])
print(f"Read {size} bytes at {hex(offset)} in 0.1 seconds")
"""


def entry(kind: int, subtype: int, offset: int, size: int, label: bytes) -> bytes:
    return flasher.ENTRY.pack(b"\xaa\x50", kind, subtype, offset, size, label.ljust(16, b"\0"), 0)


def flash_image(apps=((0x10000, 0x1000),), bootloader=b"\xe9" + b"B" * 99) -> bytes:
    """A C5's flash: the bootloader at 0x2000, the partition table at 0x8000, and its apps."""
    flash = bytearray(b"\xff" * 0x11000)
    flash[0x2000:0x2000 + len(bootloader)] = bootloader
    table = entry(1, 2, 0x9000, 0x6000, b"nvs") + entry(1, 1, 0xF000, 0x1000, b"phy_init")
    for n, (offset, size) in enumerate(apps):
        table += entry(0, 0 if n == 0 else 0x10 + n, offset, size, b"factory" if n == 0 else b"ota")
    table += b"\xeb\xeb" + b"\xff" * 14 + b"md5-of-the-table"
    flash[0x8000:0x8000 + len(table)] = table
    flash[0x10000:0x10000 + 200] = b"\xe9" + b"A" * 199
    flash[0x9000:0x9010] = b"my settings here"  # the board's own settings: never read, never written
    return bytes(flash)


class Reading(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp()).resolve()
        (self.dir / "tool").mkdir()
        (self.dir / "tool" / "esptool.py").write_bytes(FAKE_ESPTOOL)
        self.flash(flash_image())
        self.where = {"chip": "esp32c5", "bootloader": 0x2000, "table": 0x8000}
        self.source = {"folder": "/somewhere/my-board", "idf": "/somewhere/esp-idf", "hash": ""}

    def flash(self, data: bytes):
        (self.dir / "tool" / "flash.bin").write_bytes(data)

    def flasher(self) -> flasher.Flasher:
        made = flasher.Flasher(log=lambda *a: None, tool=lambda: [sys.executable, str(self.dir / "tool" / "esptool.py")])
        self.addCleanup(made.close)
        return made

    def calls(self) -> list[list[str]]:
        log = self.dir / "tool" / "calls.log"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def read(self, f, fw=FW) -> tuple[bool, str]:
        ok, msg = f.start_read("/dev/cu.test", fw, "my-board", self.where, self.dir / "out", self.source)
        end = time.monotonic() + 20
        while f.fd is not None and time.monotonic() < end:
            if select.select([f.fd], [], [], 0.1)[0]:
                f.on_output()
            f.tick()
        return ok, msg

    def test_what_it_runs_becomes_an_image_like_a_build(self):
        f = self.flasher()
        self.assertEqual(self.read(f), (True, ""))
        self.assertEqual(f.state, "restarting", f.msg)
        self.assertFalse(f.holds_port)
        image = flasher.read_image(str(self.dir / "out"))
        self.assertEqual((image["version"], image["chip"], image["board"], image["flash"]), (FW, "esp32c5", "my-board", []))
        self.assertEqual(image["from"], self.source)
        self.assertEqual([(x["offset"], x["size"]) for x in image["files"]],
                         [("0x2000", 100), ("0x8000", 3 * 32 + 32), ("0x10000", 200)])  # what's after them is erased flash
        self.assertEqual(sorted(p.name for p in (self.dir / "out").iterdir()),
                         ["app.bin", "bootloader.bin", "manifest.json", "partition-table.bin"])
        f.board_said(FW)
        self.assertEqual((f.state, f.info()["job"]), ("done", "read"))

    def test_nothing_is_written_and_the_settings_arent_read(self):
        f = self.flasher()
        self.read(f)
        calls = self.calls()
        self.assertEqual([c[c.index("read_flash") + 1:c.index("read_flash") + 3] for c in calls],
                         [["0x2000", "0x7000"], ["0x10000", "0x1000"]])
        self.assertTrue(all("write_flash" not in c for c in calls))
        self.assertEqual([c[c.index("--after") + 1] for c in calls], ["no_reset", "hard_reset"])  # it restarts once, at the end

    def test_progress_comes_through_backspaces(self):
        f = self.flasher()
        f.job, f.reading = "read", {"step": "head"}
        f._line("14336 (50 %)")
        self.assertEqual(f.pct, 2)
        f.reading["step"] = "app"
        f._line("2048 (50 %)")
        self.assertEqual(f.pct, 52)

    def test_two_apps_cant_be_told_apart(self):
        self.flash(flash_image(apps=((0x10000, 0x1000), (0x20000, 0x1000))))
        f = self.flasher()
        self.read(f)
        self.assertEqual(f.state, "failed")
        self.assertIn("more than one app", f.msg)

    def test_no_bootloader_where_the_chip_has_it_stops_it(self):
        self.flash(flash_image(bootloader=b"nonsense"))
        f = self.flasher()
        self.read(f)
        self.assertEqual(f.state, "failed")
        self.assertIn("no bootloader at 0x2000", f.msg)

    def test_the_board_coming_back_with_another_build_is_a_failure(self):
        f = self.flasher()
        self.read(f)
        f.board_said("custom-0.10.6-11111111")
        self.assertEqual(f.state, "failed")

    def test_ttycats_own_firmware_isnt_read(self):
        ok, msg = self.flasher().start_read("/dev/cu.test", "0.11.1", "my-board", self.where, self.dir / "out", self.source)
        self.assertFalse(ok)
        self.assertIn("ttycat's own", msg)
        self.assertEqual(self.calls(), [])


class Board:
    """What start_register reads of the daemon's board."""
    def __init__(self, fw=FW, name="my-board"):
        self.fd, self.fw, self.name, self.path, self.silent = 3, fw, name, "/dev/cu.test", {"/dev/cu.other"}
        self.closed = False

    def close(self):
        self.fd, self.closed = None, True


class FromSetup(Reading):
    def setUp(self):
        super().setUp()
        self.folder = self.dir / "mine" / "my-board"
        self.folder.mkdir(parents=True)
        (self.folder / "CMakeLists.txt").write_text("set(BOARD_API 1)\nidf_component_register()\n")
        (self.folder / "board.c").write_text("// mine\n")
        (self.folder / "sdkconfig.defaults").write_text('CONFIG_IDF_TARGET="esp32c5"\n')
        self.idf = self.dir / "esp-idf"
        (self.idf / "tools" / "cmake").mkdir(parents=True)
        (self.idf / "tools" / "cmake" / "version.cmake").write_text(
            "set(IDF_VERSION_MAJOR 5)\nset(IDF_VERSION_MINOR 5)\nset(IDF_VERSION_PATCH 6)\n")
        (self.idf / "export.sh").write_text("")
        self.boards = Boards(self.dir / "boards")
        patch = mock.patch("boards.find_idf", return_value=self.idf)
        patch.start()
        self.addCleanup(patch.stop)

    def register(self, f, board, folder=None) -> dict:
        return ttycatd.start_register(board, f, Updater(self.boards, release.Releases([], KEYS), Devices(), log=lambda *a: None),
                                      {"folder": str(folder or self.folder)}, self.boards)

    def test_a_folder_says_where_its_chip_keeps_things(self):
        self.assertEqual(layout(self.folder), {"chip": "esp32c5", "bootloader": 0x2000, "table": 0x8000})
        (self.folder / "sdkconfig.defaults").write_text('CONFIG_IDF_TARGET="esp32s3"\nCONFIG_PARTITION_TABLE_OFFSET=0x9000\n')
        self.assertEqual(layout(self.folder), {"chip": "esp32s3", "bootloader": 0, "table": 0x9000})
        (self.folder / "sdkconfig.defaults").unlink()
        with self.assertRaisesRegex(ValueError, "CONFIG_IDF_TARGET"):
            layout(self.folder)

    def test_choosing_its_folder_registers_it_without_writing_anything(self):
        f, board = self.flasher(), Board()
        self.assertEqual(self.register(f, board), {"ok": True, "msg": ""})
        self.assertTrue(board.closed)  # esptool gets the port
        self.assertEqual(board.silent, set())
        end = time.monotonic() + 20
        while f.fd is not None and time.monotonic() < end:
            if select.select([f.fd], [], [], 0.1)[0]:
                f.on_output()
        self.assertTrue(f.board_said(FW), f.msg)
        ttycatd.register_flashed(f.image, self.boards)
        reg = self.boards.get("my-board")
        self.assertEqual((reg["fw"], reg["folder"], reg["idf"], reg["hash"]), (FW, str(self.folder), str(self.idf), ""))
        self.assertEqual(flasher.read_image(reg["image"])["files"][2]["size"], 200)
        self.assertTrue(all("write_flash" not in c for c in self.calls()))

        # Registered: the newest core is offered. Approving doesn't carry over to it, because nothing
        # says the folder chosen is what this build came from.
        releases = self.dir / "releases"
        releases.mkdir()
        make_release(releases, "0.11.0")
        devices = Devices({FW})
        up = Updater(self.boards, release.Releases([releases], KEYS), devices, log=lambda *a: None,
                     work=self.dir / "work", keys=KEYS)
        offer = up.offer("my-board", FW)
        self.assertEqual((offer["registered"], offer["available"], offer["blocked"]), (True, "0.11.0", ""))
        with mock.patch("core_update.subprocess.Popen") as popen:
            popen.return_value.stdout.fileno.return_value = 0
            with mock.patch("core_update.os.set_blocking"):
                self.assertEqual(up.start("my-board", FW, "/dev/cu.test", lambda *a: (True, "")), (True, ""))
        self.assertFalse(up.carry)

    def test_its_build_folder_stands_for_the_folder_it_was_built_from(self):
        build = self.dir / "build-my-board"
        build.mkdir()
        (build / "project_description.json").write_text(json.dumps(
            {"build_component_info": {"my-board": {"dir": str(self.folder)}}}))
        f = self.flasher()
        self.assertEqual(self.register(f, Board(), build), {"ok": True, "msg": ""})
        self.assertEqual(f.source["folder"], str(self.folder))

    def test_a_folder_for_another_board_is_refused(self):
        other = self.dir / "mine" / "other-board"
        other.mkdir()
        for name in ("CMakeLists.txt", "board.c", "sdkconfig.defaults"):
            (other / name).write_text((self.folder / name).read_text())
        reply = self.register(self.flasher(), Board(), other)
        self.assertFalse(reply["ok"])
        self.assertIn("this board is my-board", reply["msg"])
        self.assertEqual(self.calls(), [])

    def test_a_folder_that_isnt_a_boards_is_refused(self):
        (self.folder / "board.c").unlink()
        reply = self.register(self.flasher(), Board())
        self.assertFalse(reply["ok"])
        self.assertIn("isn't a board's folder", reply["msg"])

    def test_only_a_board_running_its_own_build(self):
        reply = self.register(self.flasher(), Board(fw="0.11.1"))
        self.assertFalse(reply["ok"])
        self.assertIn("not a build of its own", reply["msg"])

    def test_no_board_nothing_to_read(self):
        board = Board()
        board.fd = None
        self.assertEqual(self.register(self.flasher(), board), {"ok": False, "msg": "no board connected"})


if __name__ == "__main__":
    unittest.main()
