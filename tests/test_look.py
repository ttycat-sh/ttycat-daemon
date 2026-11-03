"""How Bit and the board look: chosen by number, and the same tables in all three places."""
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "daemon"))
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


class SameEverywhere(unittest.TestCase):
    def test_firmware_and_app_have_the_same_colours(self):
        firmware = (ROOT / "firmware/main/gfx.c").read_text()
        app = (ROOT / "app/Sources/TTYCat/Look.swift").read_text()
        skins = re.findall(r"\{RGB\(0x(\w{6})\), RGB\(0x(\w{6})\)\},\s*// (\w+)", firmware)
        self.assertEqual(skins, [(b, s, n) for n, b, s in re.findall(r'Skin\(name: "(\w+)", body: 0x(\w{6}), shade: 0x(\w{6})\)', app)])
        accents = re.findall(r"^\s+RGB\(0x(\w{6})\),\s*// (\w+)", firmware, re.M)
        self.assertEqual(accents, [(v, n) for n, v in re.findall(r'Accent\(name: "(\w+)", value: 0x(\w{6})\)', app)])
        self.assertEqual((len(skins), len(accents)), (lk.SKINS, lk.ACCENTS))


if __name__ == "__main__":
    unittest.main()
