"""Names people give their boards: kept on the board, with a copy on the Mac."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ttycatd
from devices import Devices, clean_label


class Board(ttycatd.Board):
    """One that records what it's sent instead of writing to a port."""
    def __init__(self, hello: dict):
        super().__init__(None)
        self.fd, self.sent = 1, []
        self.said_hello({"fw": "custom-0.10.3-790f644e", "b": "my-board", "p": ttycatd.PROTOCOL, **hello})

    def send(self, obj):
        self.sent.append(obj)


class Names(unittest.TestCase):
    def setUp(self):
        self.devices = Devices(Path(tempfile.mkdtemp()) / "devices.json")

    def test_a_name_is_kept_as_the_board_can_keep_it(self):
        self.assertEqual(clean_label("  desk cat  "), "desk cat")
        self.assertEqual(clean_label('say "hi"\\\n'), "say hi")
        self.assertEqual(clean_label("katze 🐈"), "katze 🐈")
        self.assertLessEqual(len(clean_label("é" * 40).encode()), 32)
        self.assertEqual(clean_label(None), "")

    def test_naming_keeps_who_may_approve(self):
        self.devices.set_approve("custom-0.10.3-790f644e", True)
        self.devices.set_label("a1b2c3d4e5f6", "desk cat")
        self.assertTrue(self.devices.may_approve("custom-0.10.3-790f644e"))
        self.assertEqual(self.devices.label("a1b2c3d4e5f6"), "desk cat")
        self.devices.set_approve("custom-0.10.3-790f644e", False)
        self.assertEqual(self.devices.label("a1b2c3d4e5f6"), "desk cat")

    def test_the_boards_own_name_wins_and_the_mac_keeps_a_copy(self):
        self.devices.set_label("a1b2c3d4e5f6", "old name")
        board = Board({"id": "a1b2c3d4e5f6", "nm": "desk cat"})
        ttycatd.sync_label(board, self.devices)
        self.assertEqual(self.devices.label("a1b2c3d4e5f6"), "desk cat")
        self.assertEqual(board.sent, [])

    def test_a_board_that_lost_its_name_gets_it_back(self):
        self.devices.set_label("a1b2c3d4e5f6", "desk cat")
        board = Board({"id": "a1b2c3d4e5f6"})
        ttycatd.sync_label(board, self.devices)
        self.assertEqual(board.sent, [{"t": "name", "name": "desk cat"}])
        self.assertEqual(board.label, "desk cat")

    def test_two_boards_of_one_model_keep_their_own_names(self):
        ttycatd.set_label(Board({"id": "a1b2c3d4e5f6"}), self.devices, {"name": "desk cat"})
        ttycatd.set_label(Board({"id": "0000000000ff"}), self.devices, {"name": "kitchen cat"})
        self.assertEqual(self.devices.label("a1b2c3d4e5f6"), "desk cat")
        self.assertEqual(self.devices.label("0000000000ff"), "kitchen cat")

    def test_a_board_that_cant_keep_a_name_is_named_on_the_mac(self):
        board = Board({})  # firmware from before names: no id
        reply = ttycatd.set_label(board, self.devices, {"name": "old lamp"})
        self.assertTrue(reply["ok"])
        self.assertEqual(board.sent, [])
        self.assertEqual(self.devices.label("model:my-board"), "old lamp")

    def test_naming_sends_it_to_the_board_and_empty_takes_it_away(self):
        board = Board({"id": "a1b2c3d4e5f6"})
        ttycatd.set_label(board, self.devices, {"name": " desk cat "})
        self.assertEqual(board.sent[-1], {"t": "name", "name": "desk cat"})
        ttycatd.set_label(board, self.devices, {"name": ""})
        self.assertEqual(board.sent[-1], {"t": "name", "name": ""})
        self.assertIsNone(board.label)
        self.assertIsNone(self.devices.label("a1b2c3d4e5f6"))

    def test_a_name_from_before_the_board_could_keep_one_moves_to_it(self):
        ttycatd.set_label(Board({}), self.devices, {"name": "LCD Bit"})  # older firmware: on the Mac only
        board = Board({"id": "10bda3fffedf"})  # updated: it says its chip now, and has no name yet
        ttycatd.sync_label(board, self.devices)
        self.assertEqual(board.sent, [{"t": "name", "name": "LCD Bit"}])
        self.assertEqual(self.devices.label("10bda3fffedf"), "LCD Bit")
        self.assertIsNone(self.devices.label("model:my-board"))

    def test_a_wrong_id_counts_as_none(self):
        self.assertIsNone(Board({"id": "../../etc"}).id)


if __name__ == "__main__":
    unittest.main()
