"""Art for Bit's stage: what's sent, and only to a board that can draw it."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import art
import ttycatd

SNOW = Path(__file__).resolve().parents[2] / "tools" / "preview" / "art" / "snow.json"


class Source:
    def poll(self):
        return []

    def pet(self):
        return {}


class Checking(unittest.TestCase):
    def test_the_test_art_can_be_sent(self):
        self.assertEqual(art.check(json.loads(SNOW.read_text())), "")

    def test_what_no_board_would_take_is_kept_out(self):
        good = {"t": "art", "id": "snow", "f": 1}
        self.assertEqual(art.check(good), "")
        self.assertIn('"t"', art.check({**good, "t": "state"}))
        self.assertIn("id", art.check({**good, "id": "far-too-long-an-id"}))
        self.assertIn("id", art.check({**good, "id": "snow!"}))
        self.assertIn('"f"', art.check({**good, "f": 0}))
        self.assertIn('"f"', art.check({**good, "f": True}))
        self.assertIn("KB", art.check({**good, "say": "x" * art.LINE_MAX}))
        self.assertNotEqual(art.check([good]), "")


class WhoGetsIt(unittest.TestCase):
    def test_only_a_board_that_draws_the_format(self):
        snow = {"t": "art", "id": "snow", "f": 1}
        self.assertTrue(art.drawable(snow, 1))
        self.assertTrue(art.drawable(snow, 2))
        self.assertFalse(art.drawable({**snow, "f": 2}, 1))
        self.assertFalse(art.drawable(snow, None))  # built before there was art
        self.assertFalse(art.drawable(snow, True))
        self.assertFalse(art.drawable(None, 1))

    def test_a_board_says_which_format_in_its_hello_and_forgets_it_on_a_new_port(self):
        board = ttycatd.Board(None)
        board.said_hello({"fw": "0.11.0", "p": ttycatd.PROTOCOL, "art": 1})
        self.assertEqual(board.art, 1)
        board.said_hello({"fw": "0.10.1", "p": ttycatd.PROTOCOL})
        self.assertIsNone(board.art)

    def test_the_state_names_it_only_when_it_was_sent(self):
        snow = {"t": "art", "id": "snow", "f": 1}
        self.assertEqual(ttycatd.state_message(Source(), None, shown=snow)["art"], "snow")
        self.assertNotIn("art", ttycatd.state_message(Source(), None))

    def test_it_goes_to_boards_that_cant_approve_too(self):
        snow = {"t": "art", "id": "snow", "f": 1}
        msg = ttycatd.state_message(Source(), None, may_approve=False, shown=snow)
        self.assertEqual((msg["art"], msg["na"]), ("snow", True))


if __name__ == "__main__":
    unittest.main()
