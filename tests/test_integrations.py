"""What can be added to an agent's own setup: found, shown, and changed only when named."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

DAEMON = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DAEMON))
import harness  # noqa: E402
import integrations  # noqa: E402
import ttycatd  # noqa: E402
from integrations import BROKEN, INSTALLED, MISSING, UNREADABLE, Addition, Agent  # noqa: E402


class Switch:
    """An addition that's a flag in memory."""

    def __init__(self, on=False):
        self.on, self.fails = on, False

    def addition(self, name="hook"):
        def install():
            if self.fails:
                raise OSError("the settings can't be written")
            self.on = True
        return Addition(name, "Answer its prompts", "More.", "One hook.", lambda: INSTALLED if self.on else MISSING,
                        install, lambda: setattr(self, "on", False))


class Finding(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.agent = Agent("pi", "Pi", ("pi-not-a-real-program",), (".pi",))

    def test_an_agent_that_has_been_run_has_its_folder(self):
        self.assertFalse(integrations.on_this_mac(self.agent, self.home, which=lambda p: None))
        (self.home / ".pi").mkdir()
        self.assertTrue(integrations.on_this_mac(self.agent, self.home, which=lambda p: None))

    def test_one_just_installed_has_only_its_program(self):
        self.assertTrue(integrations.on_this_mac(self.agent, self.home, which=lambda p: "/usr/local/bin/" + p))


class Listing(unittest.TestCase):
    def test_each_agent_says_what_it_shows_and_what_can_be_added(self):
        agents = [Agent("claude", "Claude Code", (), (), [Switch().addition()]), Agent("pi", "Pi", (), ())]
        kinds = {"claude": harness.Claude(), "pi": harness.InATerminal()}
        got = integrations.listing(agents, kinds, found=lambda a: a.id == "claude")["agents"]
        self.assertEqual(got[0], {"id": "claude", "name": "Claude Code", "found": True,
                                  "can": {"state": "exact", "sub": "exact", "sh": "estimated"},
                                  "shows": ["What it's doing", "Subagents", "Open shells, as an estimate"],
                                  "adds": [{"id": "hook", "title": "Answer its prompts", "detail": "More.", "writes": "One hook.",
                                            "state": MISSING}]})
        self.assertEqual((got[1]["found"], got[1]["shows"], got[1]["adds"]), (False, ["What it's doing", "Open shells, as an estimate"], []))
        self.assertEqual(got[1]["can"], {"state": "exact", "sh": "estimated"})
        self.assertEqual(integrations.can(None), {"state": "exact"})

    def test_settings_that_cannot_be_read_are_a_state(self):
        def state():
            raise ValueError("not JSON")
        broken = Addition("hook", "t", "d", "w", state, lambda: None, lambda: None)
        got = integrations.listing([Agent("claude", "Claude Code", (), (), [broken])], {}, found=lambda a: True)
        self.assertEqual(got["agents"][0]["adds"][0]["state"], UNREADABLE)

    def test_every_supported_agent_is_listed_even_with_nothing_to_add(self):
        self.assertEqual([a.id for a in integrations.supported({})], ["claude", "codex", "opencode", "pi"])
        self.assertEqual(set(harness.known()), {a.id for a in integrations.supported({})})


class Changing(unittest.TestCase):
    def test_only_the_one_named_changes(self):
        one, other = Switch(), Switch()
        agents = [Agent("claude", "Claude Code", (), (), [one.addition("a"), other.addition("b")])]
        self.assertEqual(integrations.change(agents, "install", "a"), {"ok": True, "state": INSTALLED})
        self.assertEqual((one.on, other.on), (True, False))
        self.assertEqual(integrations.change(agents, "remove", "a"), {"ok": True, "state": MISSING})

    def test_a_name_it_does_not_know_changes_nothing(self):
        self.assertFalse(integrations.change([], "install", "claude-hook")["ok"])

    def test_what_goes_wrong_is_said_and_the_state_is_as_it_is(self):
        switch = Switch()
        switch.fails = True
        got = integrations.change([Agent("claude", "Claude Code", (), (), [switch.addition()])], "install", "hook")
        self.assertEqual(got, {"ok": False, "msg": "the settings can't be written", "state": MISSING})


class ClaudeHook(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "settings.json"
        self.path.write_text(json.dumps({"model": "opus", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]}}))

    def run_setup(self, action, addition=None):
        return ttycatd.setup_command(action, addition, ttycatd.agent_setups(self.path))

    def state(self):
        claude = next(a for a in self.run_setup("list")["agents"] if a["id"] == "claude")
        return claude["adds"][0]["state"]

    def test_listing_changes_nothing(self):
        before = self.path.read_text()
        self.assertEqual(self.state(), MISSING)
        self.assertEqual(self.path.read_text(), before)

    def test_installed_and_removed_by_name_leaving_the_rest(self):
        self.assertEqual(self.run_setup("install", "claude-hook"), {"ok": True, "state": INSTALLED})
        settings = json.loads(self.path.read_text())
        self.assertEqual((settings["model"], settings["hooks"]["Stop"][0]["hooks"][0]["command"]), ("opus", "say done"))
        self.assertEqual(self.run_setup("remove", "claude-hook"), {"ok": True, "state": MISSING})
        self.assertEqual(json.loads(self.path.read_text()), {"model": "opus", "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "say done"}]}]}})

    def test_one_whose_program_has_gone_is_broken(self):
        self.path.write_text(json.dumps({"hooks": {"PermissionRequest": [{"matcher": "*", "hooks": [
            {"type": "command", "command": "exec /gone/python3 -I /gone/ttycat.app/Contents/Resources/daemon/hook.py"}]}]}}))
        self.assertEqual(self.state(), BROKEN)

    def test_settings_that_are_not_json_are_never_written_over(self):
        self.path.write_text("{ not json")
        self.assertEqual(self.state(), UNREADABLE)
        self.assertFalse(self.run_setup("install", "claude-hook")["ok"])
        self.assertEqual(self.path.read_text(), "{ not json")

    def test_install_needs_a_name(self):
        self.assertFalse(self.run_setup("install")["ok"])


if __name__ == "__main__":
    unittest.main()
