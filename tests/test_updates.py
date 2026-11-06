"""Asking the update server, downloading, and keeping only what checks out. Against a server in the test."""
import hashlib
import io
import json
import sys
import tarfile
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ed25519
import release
import updates
from boards import Boards

SECRET = bytes(range(32))
KEYS = {release.key_id(ed25519.public_key(SECRET)): ed25519.public_key(SECRET)}
OTHER = bytes(range(1, 33))
C5 = "waveshare-esp32-c5-touch-lcd-2-8"


def firmware(version: str, secret=SECRET, board=C5) -> tuple[dict, dict[str, bytes]]:
    files = {"bootloader.bin": b"boot" + version.encode(), "ttycat.bin": b"app" + version.encode()}
    manifest = {"version": version, "chip": "esp32c5", "board": board, "flash": ["--flash_mode", "dio"],
                "files": [{"offset": o, "path": n, "size": len(files[n]), "sha256": hashlib.sha256(files[n]).hexdigest()}
                          for o, n in (("0x2000", "bootloader.bin"), ("0x10000", "ttycat.bin"))]}
    return release.seal_firmware(manifest, secret), files


def core(version: str, secret=SECRET) -> tuple[dict, bytes]:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        info = tarfile.TarInfo("firmware/main/model.h")
        text = f'#define FW_VERSION "{version}"\n'.encode()
        info.size = len(text)
        tar.addfile(info, io.BytesIO(text))
    data = raw.getvalue()
    name = f"ttycat-core-{version}.tar.gz"
    body = {"kind": "ttycat-core", "version": version, "board_api": 1, "idf": "5.5",
            "source": {"file": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}}
    return release.seal(body, secret), data


class Server:
    """Answers /v1/updates with whatever `answer` is, and serves `files` by path."""

    def __init__(self):
        self.answer: dict = {}
        self.files: dict[str, bytes] = {}
        self.asked: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                url = urllib.parse.urlsplit(self.path)
                if url.path == "/v1/updates":
                    outer.asked.append(dict(urllib.parse.parse_qsl(url.query)))
                    data = json.dumps(outer.answer).encode()
                elif url.path in outer.files:
                    data = outer.files[url.path]
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.http.server_address[1]}"
        threading.Thread(target=self.http.serve_forever, daemon=True).start()

    def offer_firmware(self, envelope, files, board=C5, version=None):
        version = version or json.loads(envelope["body"])["version"]
        self.answer = {"firmware": {"release": envelope, "files": {}}}
        for name, data in files.items():
            path = f"/releases/firmware/{board}/{version}/{name}"
            self.files[path] = data
            self.answer["firmware"]["files"][name] = self.base + path

    def offer_core(self, envelope, data):
        name = json.loads(envelope["body"])["source"]["file"]
        self.files[f"/releases/core/{name}"] = data
        self.answer = {"core": {"release": envelope, "archive": f"{self.base}/releases/core/{name}"}}

    def close(self):
        self.http.shutdown()
        self.http.server_close()


class Updates(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.server = Server()
        self.addCleanup(self.server.close)
        self.boards = Boards(self.home / "boards")
        self.logged = []
        self.checker = updates.Checker(self.boards, url=self.server.base + "/v1/updates", home=self.home, keys=KEYS,
                                       log=lambda *a: self.logged.append(" ".join(map(str, a))))

    def check(self, name=C5, fw="0.10.1", key="a1b2c3d4e5f6", now=0.0):
        self.checker.tick(name, fw, key, now=now)
        self.checker.wait()

    def register(self, name="my-board"):
        folder = self.home / "mine" / name
        folder.mkdir(parents=True)
        (folder / "CMakeLists.txt").write_text("set(BOARD_API 1)\n")
        (folder / "board.c").write_text("")
        idf = self.home / "esp-idf"
        (idf / "tools" / "cmake").mkdir(parents=True, exist_ok=True)
        (idf / "tools" / "cmake" / "version.cmake").write_text(
            "set(IDF_VERSION_MAJOR 5)\nset(IDF_VERSION_MINOR 5)\nset(IDF_VERSION_PATCH 6)\n")
        mine = self.boards.root / name
        mine.mkdir(parents=True)
        (mine / "board.json").write_text(json.dumps({"folder": str(folder), "idf": str(idf), "fw": "custom-0.10.3-790f644e", "hash": "x"}))

    def test_an_official_board_gets_its_firmware_downloaded_and_checked(self):
        self.server.offer_firmware(*firmware("0.11.0"))
        self.check()
        self.assertEqual(self.checker.info()["error"], "")
        got = self.checker.firmware_for(C5, "0.10.1")
        self.assertEqual(got["version"], "0.11.0")
        self.assertTrue((Path(got["dir"]) / "ttycat.bin").exists())
        self.assertIsNone(self.checker.firmware_for(C5, "0.11.0"))  # not newer than what it runs
        self.assertIsNone(self.checker.firmware_for(C5, "custom-0.10.1-896b3059"))  # someone's own: never

    def test_what_is_sent_is_only_about_the_board(self):
        self.check()
        asked = self.server.asked[0]
        self.assertEqual(set(asked), {"board", "fw", "install"})
        self.assertEqual((asked["board"], asked["fw"]), (C5, "0.10.1"))
        self.assertRegex(asked["install"], r"^[0-9a-f]{32}$")
        self.check(now=EVERY + 1)
        self.assertEqual(self.server.asked[1]["install"], asked["install"])  # the same Mac, the same ID

    def test_a_board_of_ones_own_gets_the_core(self):
        self.register()
        self.server.offer_core(*core("0.11.0"))
        self.check(name="my-board", fw="custom-0.10.3-790f644e")
        asked = self.server.asked[0]
        self.assertEqual((asked["board_api"], asked["idf"]), ("1", "5.5.6"))
        found = release.Releases([self.home / "core" / "releases"], KEYS).best()
        self.assertEqual(found["version"], "0.11.0")

    def test_an_unregistered_board_of_ones_own_isnt_asked_about(self):
        self.check(name="my-board", fw="custom-0.10.3-790f644e")
        self.assertEqual(self.server.asked, [])

    def test_a_release_signed_by_someone_else_is_deleted(self):
        self.server.offer_firmware(*firmware("0.11.0", secret=OTHER))
        self.check()
        self.assertIn("doesn't trust", self.checker.info()["error"])
        self.assertIsNone(self.checker.firmware_for(C5, "0.10.1"))
        self.assertEqual([p for p in (self.home / "firmware").rglob("*") if p.is_file()], [])

    def test_a_file_that_doesnt_match_its_release_is_deleted(self):
        envelope, files = firmware("0.11.0")
        files["ttycat.bin"] = b"something else"
        self.server.offer_firmware(envelope, files)
        self.check()
        self.assertIn("damaged", self.checker.info()["error"])
        self.assertIsNone(self.checker.firmware_for(C5, "0.10.1"))

    def test_a_core_archive_that_doesnt_match_is_deleted(self):
        self.register()
        envelope, _ = core("0.11.0")
        self.server.offer_core(envelope, b"not the archive")
        self.check(name="my-board", fw="custom-0.10.3-790f644e")
        self.assertIn("doesn't match", self.checker.info()["error"])
        self.assertEqual(list((self.home / "core" / "releases").glob("ttycat-core-*")), [])

    def test_odd_file_names_are_refused(self):
        envelope, files = firmware("0.11.0")
        self.server.offer_firmware(envelope, {"../evil.bin": b"x", **files})
        self.check()
        self.assertIn("isn't a file name", self.checker.info()["error"])
        self.assertFalse((self.home / "evil.bin").exists())

    def test_downloads_must_be_https_from_the_real_server(self):
        real = updates.Checker(self.boards, home=self.home, keys=KEYS, log=lambda *a: None)
        self.assertFalse(real.plain_http)
        with self.assertRaisesRegex(ValueError, "isn't https"):
            real._url("http://example.com/ttycat.bin")
        self.assertEqual(real._url("https://dl.ttycat.sh/x"), "https://dl.ttycat.sh/x")

    def test_asked_when_a_board_is_first_seen_then_every_hour(self):
        self.check(now=100)
        self.check(now=200)
        self.assertEqual(len(self.server.asked), 1)
        self.check(name="another-board", key="0000000000ff", now=300)  # plugged in later: asked at once
        self.assertEqual(len(self.server.asked), 2)
        self.check(now=100 + EVERY)
        self.assertEqual(len(self.server.asked), 3)

    def test_turned_off_it_never_asks(self):
        self.checker.set_enabled(False)
        self.check()
        self.assertEqual(self.server.asked, [])
        self.assertFalse(self.checker.info()["check"])
        self.checker.set_enabled(True)
        self.check()
        self.assertEqual(len(self.server.asked), 1)

    def test_a_server_that_isnt_there_is_only_noted(self):
        gone = updates.Checker(self.boards, url="http://127.0.0.1:9/v1/updates", home=self.home, keys=KEYS, log=lambda *a: None)
        gone.tick(C5, "0.10.1", None, now=0)
        gone.wait()
        self.assertNotEqual(gone.info()["error"], "")
        self.assertIsNone(gone.info()["checked"])

    def test_only_the_newest_firmware_is_kept(self):
        self.server.offer_firmware(*firmware("0.11.0"))
        self.check(now=0)
        self.server.offer_firmware(*firmware("0.12.0"))
        self.check(now=EVERY)
        self.assertEqual(sorted(p.name for p in (self.home / "firmware" / C5).iterdir()), ["0.12.0"])


EVERY = updates.EVERY_S

if __name__ == "__main__":
    unittest.main()
