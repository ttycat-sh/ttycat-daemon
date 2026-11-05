"""Which device on the USB port may approve: only ttycat's own, unless the person says otherwise."""
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ttycatd
from devices import NOT_ALLOWED, Devices, clean_name, is_custom


class Recording:
    def __init__(self):
        self.calls = []

    def act(self, action, sid, rev, picks=None, via="board"):
        self.calls.append((action, sid, rev, picks))
        return True, ""


class Base(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "devices.json"
        self.devices = Devices(self.path)


class Names(unittest.TestCase):
    def test_only_a_plain_version_is_ours(self):
        for ours in ("0.9.1", "1", "0.10.0", None):
            self.assertFalse(is_custom(ours), ours)
        for theirs in ("my-lamp-1.0", "dev", "0.9.1-mine", "1..2", "+1.2", "٣.٤"):
            self.assertTrue(is_custom(theirs), theirs)

    def test_a_name_is_made_safe_to_keep_and_show(self):
        self.assertEqual(clean_name("my-lamp\n\x1b-1.0"), "my-lamp-1.0")
        self.assertEqual(len(clean_name("x" * 500)), 48)
        for nothing in (None, 7, "", "  ", ["0.9.1"], {"a": 1}):
            self.assertIsNone(clean_name(nothing), nothing)


class WhoMayApprove(Base):
    def test_our_firmware_may_approve_without_being_asked_about(self):
        self.assertTrue(self.devices.permits("allow", "0.9.1"))
        self.assertTrue(self.devices.permits("answer", "0.9.1"))

    def test_a_device_that_hasnt_said_what_it_is_may_not(self):
        self.assertFalse(self.devices.permits("allow", None))
        self.assertFalse(self.devices.permits("answer", None))

    def test_custom_firmware_may_not_until_the_person_says_so(self):
        self.assertFalse(self.devices.permits("allow", "my-lamp-1.0"))
        self.assertFalse(self.devices.permits("answer", "my-lamp-1.0"))
        self.devices.set_approve("my-lamp-1.0", True)
        self.assertTrue(self.devices.permits("allow", "my-lamp-1.0"))
        self.devices.set_approve("my-lamp-1.0", False)
        self.assertFalse(self.devices.permits("allow", "my-lamp-1.0"))

    def test_saying_so_covers_that_name_only(self):
        self.devices.set_approve("my-lamp-1.0", True)
        self.assertFalse(self.devices.permits("allow", "my-lamp-1.1"))

    def test_anything_can_still_stop_an_agent(self):
        for fw in (None, "my-lamp-1.0", "0.9.1"):
            for action in ("deny", "open", "seen", "pet-pat"):
                self.assertTrue(self.devices.permits(action, fw), (action, fw))

    def test_a_damaged_file_means_nobody_has_been_allowed(self):
        for text in ("not json", "[]", '{"approve": "my-lamp-1.0"}', '{"approve": [1, null]}', ""):
            self.path.write_text(text)
            self.assertFalse(self.devices.may_approve("my-lamp-1.0"), text)
        self.devices.set_approve("my-lamp-1.0", True)
        self.assertTrue(self.devices.may_approve("my-lamp-1.0"))

    def test_the_choice_is_private_and_survives_a_restart(self):
        self.devices.set_approve("my-lamp-1.0", True)
        self.devices.set_approve("my-lamp-1.0", True)
        self.assertEqual(json.loads(self.path.read_text()), {"approve": ["my-lamp-1.0"]})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertTrue(Devices(self.path).may_approve("my-lamp-1.0"))


class WhatTheDaemonDoes(Base):
    ALLOW = {"t": "act", "a": "allow", "id": "p1", "rev": "r1"}

    def test_a_refused_approval_never_reaches_the_agent(self):
        source = Recording()
        for fw in ("my-lamp-1.0", None):
            self.assertEqual(ttycatd.device_action(source, fw, self.devices, self.ALLOW), (False, NOT_ALLOWED))
            self.assertEqual(ttycatd.device_action(source, fw, self.devices, {**self.ALLOW, "a": "answer"}), (False, NOT_ALLOWED))
        self.assertEqual(source.calls, [])

    def test_a_refused_message_fits_on_the_boards_toast(self):
        self.assertLess(len(NOT_ALLOWED), 48)  # ui_event_t.text

    def test_our_firmware_and_a_deny_go_through(self):
        source = Recording()
        self.assertEqual(ttycatd.device_action(source, "0.9.1", self.devices, self.ALLOW), (True, ""))
        self.assertEqual(ttycatd.device_action(source, "my-lamp-1.0", self.devices, {**self.ALLOW, "a": "deny"}), (True, ""))
        self.assertEqual([c[0] for c in source.calls], ["allow", "deny"])

    def board(self, fw="my-lamp-1.0", connected=True):
        board = ttycatd.Board(None)
        board.fw, board.fd = fw, 99 if connected else None
        return board

    def test_setup_can_only_change_the_custom_device_plugged_in_now(self):
        self.assertTrue(ttycatd.device_setting(self.board(), self.devices, {"fw": "my-lamp-1.0", "approve": True})["ok"])
        self.assertTrue(self.devices.may_approve("my-lamp-1.0"))
        for board, name in ((self.board(), "other"), (self.board(connected=False), "my-lamp-1.0"), (self.board("0.9.1"), "0.9.1"), (self.board(None), None)):
            self.assertFalse(ttycatd.device_setting(board, self.devices, {"fw": name, "approve": True})["ok"])
        self.assertEqual(self.devices._approving(), ["my-lamp-1.0"])

    def test_only_a_real_true_turns_it_on(self):
        for not_yes in ("true", 1, None):
            ttycatd.device_setting(self.board(), self.devices, {"fw": "my-lamp-1.0", "approve": not_yes})
            self.assertFalse(self.devices.may_approve("my-lamp-1.0"))


if __name__ == "__main__":
    unittest.main()
