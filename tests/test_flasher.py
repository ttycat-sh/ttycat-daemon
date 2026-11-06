"""The firmware updater, with a stand-in for esptool. Nothing here touches a real board."""
import hashlib
import json
import os
import select
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

DAEMON = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DAEMON))
import ttycatd  # noqa: E402
import ed25519  # noqa: E402
import flasher  # noqa: E402
import release  # noqa: E402

SECRET = bytes(range(32))  # a test key: ttycat's own images are only written signed
KEYS = {release.key_id(ed25519.public_key(SECRET)): ed25519.public_key(SECRET)}

GOOD = b"""
import sys
print("Connecting....")
print("Writing at 0x00002000... (100 %)", flush=True)
print("Writing at 0x00010000... (50 %)", end="\\r", flush=True)
print("Writing at 0x00010000... (100 %)", flush=True)
print("Hash of data verified.")
sys.exit(0)
"""
NO_CONNECT = b"""
import sys
print("A fatal error occurred: Failed to connect to ESP32-C5: No serial data received.")
sys.exit(2)
"""
WRONG_CHIP = b"""
import sys
print("A fatal error occurred: This chip is ESP32-S3, not ESP32-C5. Wrong chip argument?")
sys.exit(2)
"""
HANGS = b"""
import time
print("Connecting....", flush=True)
time.sleep(60)
"""


class Image:
    def __init__(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name)
        self.manifest = {"version": "0.7.0", "chip": "esp32c5", "flash": ["--flash_mode", "dio"], "files": []}
        for offset, name, size in (("0x2000", "boot.bin", 100), ("0x10000", "app.bin", 900)):
            data = b"x" * size
            (self.path / name).write_bytes(data)
            self.manifest["files"].append(
                {"offset": offset, "path": name, "size": size, "sha256": hashlib.sha256(data).hexdigest()})
        self.save()

    def save(self, secret: bytes | None = SECRET):
        (self.path / "manifest.json").write_text(json.dumps(self.manifest))
        (self.path / "release.json").unlink(missing_ok=True)
        if secret:
            (self.path / "release.json").write_text(json.dumps(release.seal_firmware(self.manifest, secret)))


class ReadImage(unittest.TestCase):
    def setUp(self):
        self.image = Image()
        self.addCleanup(self.image.dir.cleanup)

    def test_a_sound_image(self):
        read = flasher.read_image(str(self.image.path), KEYS)
        self.assertEqual(read["version"], "0.7.0")
        self.assertEqual([f["offset"] for f in read["files"]], ["0x2000", "0x10000"])

    def test_a_changed_file_is_refused(self):
        (self.image.path / "app.bin").write_bytes(b"y" * 900)
        with self.assertRaisesRegex(ValueError, "damaged"):
            flasher.read_image(str(self.image.path), KEYS)

    def test_a_shortened_file_is_refused(self):
        (self.image.path / "app.bin").write_bytes(b"x" * 899)
        with self.assertRaisesRegex(ValueError, "damaged"):
            flasher.read_image(str(self.image.path), KEYS)

    def test_a_nonsense_chip_is_refused(self):
        self.image.manifest["chip"] = "--flash-erase"
        self.image.save()
        with self.assertRaisesRegex(ValueError, "chip"):
            flasher.read_image(str(self.image.path), KEYS)

    def test_files_outside_the_image_are_refused(self):
        self.image.manifest["files"][0]["path"] = "../boot.bin"
        self.image.save()
        with self.assertRaisesRegex(ValueError, "outside"):
            flasher.read_image(str(self.image.path), KEYS)

    def test_an_offset_that_could_be_an_option_is_refused(self):
        self.image.manifest["files"][0]["offset"] = "--erase-all"
        self.image.save()
        with self.assertRaisesRegex(ValueError, "offset"):
            flasher.read_image(str(self.image.path), KEYS)

    def test_flash_settings_can_only_be_plain_words(self):
        self.image.manifest["flash"] = ["--flash_mode", "dio; rm -rf /"]
        self.image.save()
        with self.assertRaisesRegex(ValueError, "flash settings"):
            flasher.read_image(str(self.image.path), KEYS)

    def test_ttycats_own_firmware_has_to_be_signed(self):
        self.image.save(secret=None)
        with self.assertRaisesRegex(ValueError, "isn't signed"):
            flasher.read_image(str(self.image.path), KEYS)

    def test_signed_by_a_key_nobody_trusts_is_refused(self):
        self.image.save(secret=bytes(range(1, 33)))
        with self.assertRaisesRegex(ValueError, "doesn't trust"):
            flasher.read_image(str(self.image.path), KEYS)

    def test_the_signature_is_what_counts_not_the_manifest_beside_it(self):
        # Someone swaps the files and writes a matching manifest.json: release.json still names the old ones.
        (self.image.path / "app.bin").write_bytes(b"evil" * 225)
        self.image.manifest["files"][1]["sha256"] = hashlib.sha256(b"evil" * 225).hexdigest()
        (self.image.path / "manifest.json").write_text(json.dumps(self.image.manifest))
        with self.assertRaisesRegex(ValueError, "damaged"):
            flasher.read_image(str(self.image.path), KEYS)

    def test_a_changed_signed_manifest_is_refused(self):
        envelope = json.loads((self.image.path / "release.json").read_text())
        envelope["body"] = envelope["body"].replace("0.7.0", "0.9.0")
        (self.image.path / "release.json").write_text(json.dumps(envelope))
        with self.assertRaisesRegex(ValueError, "signature"):
            flasher.read_image(str(self.image.path), KEYS)

    def test_a_signed_core_release_isnt_firmware(self):
        envelope = release.seal({"kind": "ttycat-core", "version": "0.7.0"}, SECRET)
        (self.image.path / "release.json").write_text(json.dumps(envelope))
        with self.assertRaisesRegex(ValueError, "isn't a firmware release"):
            flasher.read_image(str(self.image.path), KEYS)

    def test_someones_own_build_needs_no_signature(self):
        self.image.manifest["version"] = "custom-0.7.0-a1b2c3d4"
        self.image.save(secret=None)
        self.assertFalse(flasher.read_image(str(self.image.path), KEYS)["signed"])

    def test_no_manifest(self):
        with self.assertRaisesRegex(ValueError, "no usable"):
            flasher.read_image(str(self.image.path / "nothing"), KEYS)


class Flashing(unittest.TestCase):
    def setUp(self):
        self.image = Image()
        self.addCleanup(self.image.dir.cleanup)

    def flasher(self, script: bytes) -> flasher.Flasher:
        path = self.image.path / "fake_esptool.py"
        path.write_bytes(script)
        made = flasher.Flasher(log=lambda *a: None, tool=lambda: [sys.executable, str(path)], keys=KEYS)
        self.addCleanup(made.close)
        return made

    def run_to_end(self, f, seconds=10):
        end = time.monotonic() + seconds
        while f.fd is not None and time.monotonic() < end:
            if select.select([f.fd], [], [], 0.1)[0]:
                f.on_output()
            f.tick()

    def start(self, f, fw="0.6.1", unknown=False):
        return f.start(str(self.image.path), "/dev/cu.test", fw, unknown)

    def test_written_is_not_finished_until_the_board_says_so(self):
        f = self.flasher(GOOD)
        self.assertEqual(self.start(f), (True, ""))
        self.assertTrue(f.holds_port)
        self.run_to_end(f)
        self.assertEqual(f.state, "restarting")
        self.assertFalse(f.holds_port)
        self.assertTrue(f.busy)
        f.board_said("0.7.0")
        self.assertEqual(f.state, "done")

    def test_the_old_version_coming_back_is_a_failure(self):
        f = self.flasher(GOOD)
        self.start(f)
        self.run_to_end(f)
        f.board_said("0.6.1")
        self.assertEqual(f.state, "failed")
        self.assertIn("0.6.1", f.msg)

    def test_a_board_that_never_comes_back_is_a_failure(self):
        f = self.flasher(GOOD)
        self.start(f)
        self.run_to_end(f)
        f.deadline = 0
        f.tick()
        self.assertEqual(f.state, "failed")
        self.assertIn("didn't come back", f.msg)

    def test_a_port_that_never_said_what_it_is_needs_agreement(self):
        f = self.flasher(GOOD)
        ok, msg = self.start(f, fw=None)
        self.assertFalse(ok)
        self.assertIn("hasn't said", msg)
        self.assertEqual(f.state, "idle")
        self.assertTrue(self.start(f, fw=None, unknown=True)[0])

    def test_custom_firmware_is_never_overwritten_without_agreement(self):
        f = self.flasher(GOOD)
        ok, msg = self.start(f, fw="my-lamp-1.0")
        self.assertFalse(ok)
        self.assertIn("my-lamp-1.0", msg)
        self.assertEqual(f.state, "idle")
        self.assertTrue(self.start(f, fw="my-lamp-1.0", unknown=True)[0])

    def test_firmware_for_another_board_is_refused_even_with_agreement(self):
        self.image.manifest["board"] = "another-board"
        self.image.save()
        f = self.flasher(GOOD)
        ok, msg = f.start(str(self.image.path), "/dev/cu.test", "my-lamp-1.0", True, "this-board")
        self.assertFalse(ok)
        self.assertIn("another-board", msg)
        self.assertEqual(f.state, "idle")

    def test_no_board_no_flash(self):
        f = self.flasher(GOOD)
        self.assertFalse(f.start(str(self.image.path), None, "0.6.1", False)[0])

    def test_one_at_a_time(self):
        f = self.flasher(HANGS)
        self.assertTrue(self.start(f)[0])
        self.assertEqual(self.start(f), (False, "already updating"))

    def test_a_damaged_image_never_starts_esptool(self):
        f = self.flasher(GOOD)
        (self.image.path / "app.bin").write_bytes(b"z" * 900)
        ok, msg = self.start(f)
        self.assertFalse(ok)
        self.assertIn("damaged", msg)
        self.assertIsNone(f.process)
        self.assertEqual(f.state, "idle")

    def test_no_esptool(self):
        f = flasher.Flasher(log=lambda *a: None, tool=lambda: None)
        ok, msg = f.start(str(self.image.path), "/dev/cu.test", "0.6.1", False)
        self.assertFalse(ok)
        self.assertIn("esptool", msg)
        self.assertFalse(f.info()["tool"])

    def test_cant_connect_says_what_to_do(self):
        f = self.flasher(NO_CONNECT)
        self.start(f)
        self.run_to_end(f)
        self.assertEqual((f.state, f.msg), ("failed", flasher.CANT_CONNECT))
        self.assertFalse(f.holds_port)

    def test_being_killed_is_called_an_interruption(self):
        f = self.flasher(HANGS)
        self.start(f)
        f.process.terminate()
        self.run_to_end(f)
        self.assertEqual((f.state, f.msg), ("failed", "The update was interrupted. Try again."))

    def test_the_wrong_chip_is_reported(self):
        f = self.flasher(WRONG_CHIP)
        self.start(f)
        self.run_to_end(f)
        self.assertEqual(f.state, "failed")
        self.assertIn("ESP32-S3", f.msg)

    def test_esptool_stuck_is_stopped(self):
        f = self.flasher(HANGS)
        self.start(f)
        process = f.process
        f.deadline = 0
        f.tick()
        self.assertEqual(f.state, "failed")
        self.assertIsNotNone(process.poll())
        self.assertIsNone(f.fd)

    def test_progress_counts_bytes_across_files(self):
        f = self.flasher(HANGS)
        self.start(f)
        f._line("Writing at 0x00002000... (100 %)")
        self.assertEqual(f.pct, 10)  # 100 of 1000 bytes
        f._line("Writing at 0x00010000... (50 %)")
        self.assertEqual(f.pct, 55)
        f._line("Writing at 0x00010000... (100 %)")
        self.assertEqual(f.pct, 99)  # 100 only once esptool has finished checking

    def test_stopping_the_daemon_stops_esptool(self):
        f = self.flasher(HANGS)
        self.start(f)
        process = f.process
        f.close()
        self.assertIsNotNone(process.poll())


class OwnEsptool(unittest.TestCase):
    def test_the_apps_own_comes_first(self):
        with tempfile.TemporaryDirectory() as prefix:
            package = Path(prefix) / "lib" / f"python{sys.version_info[0]}.{sys.version_info[1]}" / "site-packages" / "esptool"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("")
            with mock.patch.object(flasher.sys, "prefix", prefix):
                self.assertEqual(flasher.find_esptool(), [sys.executable, "-E", "-s", "-B", "-m", "esptool"])

class Esptool(unittest.TestCase):
    def test_found_only_with_its_python(self):
        with tempfile.TemporaryDirectory() as home:
            env = Path(home) / ".espressif/python_env/idf5.5_py3.14_env"
            package = env / "lib/python3.14/site-packages/esptool"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("")
            old = os.environ["HOME"]
            os.environ["HOME"] = home
            self.addCleanup(os.environ.__setitem__, "HOME", old)
            self.assertIsNone(flasher.find_esptool())
            (env / "bin").mkdir()
            (env / "bin/python").write_text("")
            self.assertEqual(flasher.find_esptool(), [str(env / "bin/python"), "-m", "esptool"])


if __name__ == "__main__":
    unittest.main()
