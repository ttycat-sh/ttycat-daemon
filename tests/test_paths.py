"""Where the daemon keeps its state, and which Claude settings it changes: paths.py decides, and nothing else does."""
import ast
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))

import paths  # noqa: E402
from patience import PATIENCE  # noqa: E402


class TheStateFolder(unittest.TestCase):
    def test_is_ttycat_in_home_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TTYCAT_HOME", None)
            self.assertEqual(paths.state_dir(), Path.home() / ".ttycat")
            self.assertEqual(paths.state_dir("/Users/someone"), Path("/Users/someone/.ttycat"))

    def test_is_moved_by_ttycat_home(self):
        with mock.patch.dict(os.environ, {"TTYCAT_HOME": "/tmp/second"}):
            self.assertEqual(paths.state_dir(), Path("/tmp/second"))
            self.assertEqual(paths.state_dir("/Users/someone"), Path("/tmp/second"))

    def test_only_by_a_whole_path(self):
        for value in ("", "second", "./second", "~/second"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"TTYCAT_HOME": value}):
                self.assertEqual(paths.state_dir(), Path.home() / ".ttycat")


class ClaudesSettings(unittest.TestCase):
    def test_are_claudes_own_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TTYCAT_CLAUDE_SETTINGS", None)
            self.assertEqual(paths.claude_settings(), Path.home() / ".claude" / "settings.json")

    def test_can_be_another_file(self):
        with mock.patch.dict(os.environ, {"TTYCAT_CLAUDE_SETTINGS": "/tmp/second/claude-settings.json"}):
            self.assertEqual(paths.claude_settings(), Path("/tmp/second/claude-settings.json"))

    def test_only_by_a_whole_path(self):
        with mock.patch.dict(os.environ, {"TTYCAT_CLAUDE_SETTINGS": "settings.json"}):
            self.assertEqual(paths.claude_settings(), Path.home() / ".claude" / "settings.json")


class ASecondDaemon(unittest.TestCase):
    """Run as the person would beside the daemon they use: its own folder, no board. With HOME moved too, anything
    that still went to ~/.ttycat would show up there."""

    def setUp(self):
        # Short, under /tmp: a socket's path has to fit in 104 bytes.
        self.home = Path(tempfile.mkdtemp(prefix="tc-home-", dir="/tmp"))
        self.state = Path(tempfile.mkdtemp(prefix="tc-state-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.addCleanup(shutil.rmtree, self.state, True)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("TTYCAT_")}
        self.env.update(HOME=str(self.home), TTYCAT_HOME=str(self.state),
                        TTYCAT_CLAUDE_SETTINGS=str(self.state / "claude-settings.json"))

    def daemon(self, *args) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "ttycatd.py", *args], cwd=HERE, env=self.env,
                              capture_output=True, text=True, timeout=PATIENCE)

    def test_keeps_everything_in_its_folder(self):
        running = subprocess.Popen([sys.executable, "ttycatd.py", "--no-board", "demo"], cwd=HERE, env=self.env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + PATIENCE
            while not (self.state / "ctl.sock").exists() and time.monotonic() < deadline and running.poll() is None:
                time.sleep(0.05)
            self.assertTrue((self.state / "ctl.sock").exists(), "the daemon never listened in its folder")
            told = self.daemon("status")
            self.assertEqual(told.returncode, 0, told.stderr)
            board = json.loads(told.stdout)["board"]
            self.assertEqual((board["connected"], board["port"], board["silent"]), (False, None, []))
        finally:
            running.send_signal(signal.SIGTERM)
            running.wait(timeout=PATIENCE)
        self.assertTrue((self.state / "daemon.lock").exists())
        self.assertFalse((self.home / ".ttycat").exists())

    def test_its_hook_goes_in_the_settings_it_was_given(self):
        self.assertEqual(self.daemon("hook", "install").returncode, 0)
        given = json.loads((self.state / "claude-settings.json").read_text())
        self.assertIn("PermissionRequest", given["hooks"])
        self.assertEqual(self.daemon("hook", "remove").returncode, 0)
        self.assertNotIn("hooks", json.loads((self.state / "claude-settings.json").read_text()))
        self.assertFalse((self.home / ".claude").exists())
        self.assertFalse((self.home / ".ttycat").exists())

    def test_the_hook_finds_its_socket(self):
        def sock(**env):
            run = subprocess.run([sys.executable, "-c", "import hook; print(hook.SOCK)"], cwd=HERE,
                                 env={**self.env, **env}, capture_output=True, text=True, timeout=PATIENCE)
            return run.stdout.strip()
        self.assertEqual(sock(), str(self.state / "hook.sock"))
        self.assertEqual(sock(TTYCAT_HOOK_SOCK="/tmp/elsewhere.sock"), "/tmp/elsewhere.sock")
        self.assertEqual(sock(TTYCAT_HOME="relative"), str(self.home / ".ttycat" / "hook.sock"))


class InOnePlace(unittest.TestCase):
    """The state folder is named in paths.py only, so a new file can't quietly keep something in ~/.ttycat. hook.py
    runs alone, on other machines too, and says the same thing itself. Comments and docstrings may name it."""

    FOLDER = re.compile(r"(?<![\w-])\.ttycat(?![\w-])")
    ALONE = {"paths.py", "hook.py"}

    def strings(self, path: Path):
        tree = ast.parse(path.read_text())
        docs = {id(node.body[0].value) for node in ast.walk(tree)
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
                yield node.lineno, node.value

    def test_no_other_file_names_the_folder(self):
        found = [f"{path.name}:{line}: {text!r}"
                 for path in sorted(HERE.glob("*.py")) if path.name not in self.ALONE
                 for line, text in self.strings(path) if self.FOLDER.search(text)]
        self.assertEqual(found, [])

    def test_this_test_would_see_one(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "new.py"
            path.write_text('"""Kept in ~/.ttycat."""\nFILE = Path.home() / ".ttycat" / "x.json"\n')
            self.assertEqual([text for _, text in self.strings(path) if self.FOLDER.search(text)], [".ttycat"])


if __name__ == "__main__":
    unittest.main()
