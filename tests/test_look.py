"""How Bit and the board look: chosen by number."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ttycatd  # noqa: E402
import look as lk  # noqa: E402


class FakeSource:
    def poll(self):
        return []

    def pet(self):
        return None


class Numbers(unittest.TestCase):
    def test_it_remembers(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "look.json"
            lk.Look(path).set(2, 3)
            self.assertEqual(lk.Look(path).info(), {"skin": 2, "accent": 3})

    def test_nonsense_becomes_the_default(self):
        look = lk.Look(None)
        for bad in (99, -1, "2", True, None, 1.5):
            self.assertEqual(look.set(bad, bad), {"skin": 0, "accent": 0})

    def test_a_broken_file_is_the_default(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "look.json"
            path.write_text("{nope")
            self.assertEqual(lk.Look(path).info(), {"skin": 0, "accent": 0})

    def test_the_board_hears_it_only_when_its_not_the_default(self):
        look = lk.Look(None)
        self.assertNotIn("look", ttycatd.state_message(FakeSource(), None, look))
        look.set(1, 0)
        self.assertEqual(ttycatd.state_message(FakeSource(), None, look)["look"], [1, 0])


class Tables(unittest.TestCase):
    def test_the_counts_come_from_the_tables(self):
        self.assertEqual((lk.SKINS, lk.ACCENTS), (6, 5))
        self.assertEqual(lk.SKIN_COLOURS[0][0], "Ember")  # 0, the default


if __name__ == "__main__":
    unittest.main()
