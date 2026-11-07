"""A release ignores the developer overrides, and knows its environment. tools/app_build.py writes what says so."""
import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

DAEMON = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DAEMON))
sys.path.insert(0, str(DAEMON.parent / "tools"))
import app_build  # noqa: E402
import build_info  # noqa: E402
import ed25519  # noqa: E402
import release  # noqa: E402
import updates  # noqa: E402

DEV_KEY = base64.b64encode(ed25519.public_key(bytes(range(32)))).decode()
RELEASE = {"release": True, "env": "prod", "updates": "https://api.ttycat.sh/v1/updates"}


class BuildJson(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def test_no_file_is_a_developers_build(self):
        self.assertEqual(build_info.read(self.dir / "build.json"), {})
        self.assertFalse(build_info.is_release({}))

    def test_a_release_says_so(self):
        (self.dir / "build.json").write_text(json.dumps(RELEASE))
        self.assertTrue(build_info.is_release(build_info.read(self.dir / "build.json")))

    def test_a_damaged_file_never_turns_the_overrides_on(self):
        for text in ("not json", "[]", '{"release": false}', '{"release": "yes"}'):
            (self.dir / "build.json").write_text(text)
            self.assertTrue(build_info.is_release(build_info.read(self.dir / "build.json")), text)


class Overrides(unittest.TestCase):
    def setUp(self):
        self.keys = Path(tempfile.mkdtemp()) / "release_keys.json"
        self.keys.write_text('{"keys": []}')

    def test_a_developers_build_trusts_the_key_it_is_told_to(self):
        with mock.patch.dict(os.environ, {release.DEV_KEY_ENV: DEV_KEY}):
            self.assertEqual(len(release.trusted_keys(self.keys, build={})), 1)

    def test_a_release_trusts_only_what_it_was_built_with(self):
        with mock.patch.dict(os.environ, {release.DEV_KEY_ENV: DEV_KEY}):
            self.assertEqual(release.trusted_keys(self.keys, build=RELEASE), {})

    def test_a_release_asks_its_own_server_whatever_the_environment_says(self):
        with mock.patch.dict(os.environ, {updates.URL_ENV: "http://localhost:8080/v1/updates"}):
            dev = updates.Checker(build={**RELEASE, "updates": "https://api.dev.ttycat.sh/v1/updates"}, log=lambda *a: None)
            self.assertEqual((dev.url, dev.plain_http), ("https://api.dev.ttycat.sh/v1/updates", False))
            mine = updates.Checker(build={}, log=lambda *a: None)
            self.assertEqual((mine.url, mine.plain_http), ("http://localhost:8080/v1/updates", True))
        self.assertEqual(updates.Checker(build={}, log=lambda *a: None).url, updates.UPDATES_URL)


class AppBuild(unittest.TestCase):
    def test_versions_and_build_numbers(self):
        self.assertEqual(app_build.build_number("0.2.0-dev.1"), ("0.2.0", 2000001, "dev"))
        self.assertEqual(app_build.build_number("0.2.0"), ("0.2.0", 2000999, "prod"))
        self.assertEqual(app_build.build_number("1.0.0"), ("1.0.0", 1000000999, "prod"))
        order = ["0.1.9", "0.2.0-dev.1", "0.2.0-dev.2", "0.2.0", "0.2.1-dev.1", "0.10.0"]
        numbers = [app_build.build_number(v)[1] for v in order]
        self.assertEqual(numbers, sorted(numbers))  # what Sparkle compares always grows
        for bad in ("0.2", "v0.2.0", "0.2.0-beta.1", "0.2.0-dev.0", "0.2.0-dev.999", "1000.0.0"):
            with self.assertRaises(ValueError, msg=bad):
                app_build.build_number(bad)

    def run_tool(self, *version) -> tuple[str, Path]:
        out = Path(tempfile.mkdtemp())
        said = subprocess.run([sys.executable, str(DAEMON.parent / "tools" / "app_build.py"), str(out), *version],
                              capture_output=True, text=True, check=True).stdout.strip()
        return said, out

    def key_ids(self, out: Path) -> set[str]:
        return {k["id"] for k in json.loads((out / "release_keys.json").read_text())["keys"]}

    def test_a_dev_release_trusts_dev_too_and_asks_dev(self):
        said, out = self.run_tool("0.2.0-dev.3")
        self.assertEqual(said, "0.2.0 2000003 " + app_build.SPARKLE_KEYS["dev"])
        info = json.loads((out / "build.json").read_text())
        self.assertEqual((info["env"], info["updates"], info["version"]), ("dev", app_build.UPDATES["dev"], "0.2.0-dev.3"))
        self.assertEqual(info["appcast"], "https://api.dev.ttycat.sh/v1/appcast")
        self.assertIn("2d95d7c3a1680322", self.key_ids(out))

    def test_a_prod_release_trusts_prod_only(self):
        said, out = self.run_tool("0.2.0")
        self.assertEqual(said, "0.2.0 2000999 " + app_build.SPARKLE_KEYS["prod"])
        self.assertEqual(json.loads((out / "build.json").read_text())["env"], "prod")
        self.assertNotIn("2d95d7c3a1680322", self.key_ids(out))

    def test_without_a_version_its_a_developers_build(self):
        said, out = self.run_tool()
        self.assertEqual(said, "0.0.0 0 -")  # no key: a developer's build never updates itself
        self.assertFalse((out / "build.json").exists())
        self.assertTrue((out / "release_keys.json").exists())  # the app trusts prod's keys, as a release would


if __name__ == "__main__":
    unittest.main()
