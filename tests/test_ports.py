"""Finding the board among whatever's on the serial ports, and telling it nothing until it says who it is."""
import os
import pty
import sys
import tty
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ttycatd


def terminal():
    """A stand-in for a serial port: what's written to `far` arrives at the path, as from a device."""
    far, near = pty.openpty()
    tty.setraw(near)
    return far, os.ttyname(near), near


class Ports(unittest.TestCase):
    def setUp(self):
        self.first = terminal()
        self.second = terminal()
        for t in (self.first, self.second):
            self.addCleanup(os.close, t[0])
            self.addCleanup(os.close, t[2])
        self.ports = [self.first[1], self.second[1]]
        patch = mock.patch.object(ttycatd, "serial_ports", lambda: list(self.ports))
        patch.start()
        self.addCleanup(patch.stop)
        self.board = ttycatd.Board(None)
        self.addCleanup(self.board.close)

    def later(self):
        return self.board.opened_at + ttycatd.HELLO_WAIT_S

    def test_a_silent_port_gets_one_try_and_then_the_next_gets_its_turn(self):
        self.assertTrue(self.board.open())
        self.assertEqual(self.board.path, self.first[1])
        self.board.give_up_if_silent(self.later() - 1)
        self.assertIsNotNone(self.board.fd)
        self.board.give_up_if_silent(self.later())
        self.assertIsNone(self.board.fd)
        self.assertEqual(self.board.silent, {self.first[1]})
        self.assertTrue(self.board.open())
        self.assertEqual(self.board.path, self.second[1])

    def test_once_every_port_has_had_a_try_they_are_left_alone(self):
        for _ in self.ports:
            self.board.open()
            self.board.give_up_if_silent(self.later())
        self.assertFalse(self.board.open())
        self.assertIsNone(self.board.fd)
        self.assertEqual(sorted(self.board.silent), sorted(self.ports))

    def test_a_port_that_comes_back_gets_another_try(self):
        self.board.open()
        self.board.give_up_if_silent(self.later())
        self.ports.remove(self.first[1])
        self.board.open()
        self.board.close()
        self.assertEqual(self.board.silent, set())
        self.ports.insert(0, self.first[1])
        self.board.open()
        self.assertEqual(self.board.path, self.first[1])

    def test_one_that_says_hello_stays(self):
        self.board.open()
        self.board.said_hello({"fw": "0.9.1", "b": "a-board", "p": ttycatd.PROTOCOL})
        self.board.give_up_if_silent(self.later() + 100)
        self.assertIsNotNone(self.board.fd)
        self.assertEqual((self.board.fw, self.board.name), ("0.9.1", "a-board"))
        self.assertTrue(self.board.speaks)

    def test_nothing_is_sent_until_a_device_speaks_the_protocol(self):
        self.board.open()
        self.assertFalse(self.board.speaks)
        self.board.said_hello({"fw": "my-lamp"})
        self.assertFalse(self.board.speaks)
        self.board.said_hello({"fw": "my-lamp", "p": ttycatd.PROTOCOL + 1})
        self.assertFalse(self.board.speaks)
        self.board.said_hello({"fw": "my-lamp", "p": ttycatd.PROTOCOL})
        self.assertTrue(self.board.speaks)

    def test_an_explicit_port_is_used_whatever_it_says(self):
        board = ttycatd.Board(self.second[1])
        self.addCleanup(board.close)
        self.assertTrue(board.open())
        self.assertEqual(board.path, self.second[1])
        board.give_up_if_silent(board.opened_at + ttycatd.HELLO_WAIT_S)
        self.assertIsNotNone(board.fd)
        self.assertIn(self.second[1], board.silent)


class StatusSaysWhere(unittest.TestCase):
    class Source:
        def poll(self):
            return []

        def pet(self):
            return {}

    def board(self, **fields):
        board = ttycatd.Board(None)
        for k, v in fields.items():
            setattr(board, k, v)
        return ttycatd.status_message(self.Source(), board)["board"]

    def test_only_a_device_that_said_hello_counts_as_connected(self):
        self.assertFalse(self.board(fd=9, path="/dev/x")["connected"])
        self.assertTrue(self.board(fd=9, path="/dev/x")["probing"])
        said = self.board(fd=9, path="/dev/x", fw="0.9.1", protocol=ttycatd.PROTOCOL)
        self.assertTrue(said["connected"])
        self.assertFalse(said["probing"])
        self.assertFalse(said["outdated"])

    def test_one_speaking_another_protocol_is_outdated(self):
        self.assertTrue(self.board(fd=9, path="/dev/x", fw="my-lamp", protocol=0)["outdated"])

    def test_silent_ports_are_listed_for_setup(self):
        self.assertEqual(self.board(silent={"/dev/b", "/dev/a"})["silent"], ["/dev/a", "/dev/b"])


class Flashing(unittest.TestCase):
    class Flasher:
        def __init__(self):
            self.calls = []

        def start(self, folder, port, fw, unknown, name=None):
            self.calls.append((port, fw, unknown, name))
            return (True, "") if port else (False, "no board connected")

    def setUp(self):
        self.flasher = self.Flasher()
        self.board = ttycatd.Board(None)
        self.board.close = lambda: setattr(self.board, "fd", None)

    def flash(self, **cmd):
        return ttycatd.start_flash(self.board, self.flasher, {"dir": "/x", **cmd})

    def test_the_connected_board_by_default(self):
        self.board.fd, self.board.path, self.board.fw, self.board.name = 9, "/dev/b", "0.9.1", "a"
        self.assertTrue(self.flash()["ok"])
        self.assertEqual(self.flasher.calls, [("/dev/b", "0.9.1", False, "a")])
        self.assertIsNone(self.board.fd)

    def test_a_port_still_being_tried_isnt_flashed_unasked(self):
        self.board.fd, self.board.path = 9, "/dev/b"
        self.assertFalse(self.flash()["ok"])
        self.assertIsNotNone(self.board.fd)

    def test_a_silent_port_can_be_named(self):
        self.board.silent = {"/dev/b"}
        self.assertTrue(self.flash(port="/dev/b", unknown=True)["ok"])
        self.assertEqual(self.flasher.calls, [("/dev/b", None, True, None)])
        self.assertEqual(self.board.silent, set())

    def test_an_unknown_port_cant_be_named(self):
        self.assertFalse(self.flash(port="/dev/disk0", unknown=True)["ok"])
        self.assertEqual(self.flasher.calls, [])

    def test_a_port_cant_be_named_past_a_connected_board(self):
        self.board.fd, self.board.path, self.board.fw = 9, "/dev/b", "0.9.1"
        self.board.silent = {"/dev/c"}
        self.assertFalse(self.flash(port="/dev/c", unknown=True)["ok"])
        self.assertEqual(self.flasher.calls, [])


class WhatTheDeviceIsTold(unittest.TestCase):
    Source = StatusSaysWhere.Source

    def test_a_device_that_cant_approve_is_told(self):
        self.assertTrue(ttycatd.state_message(self.Source(), None, None, False)["na"])
        self.assertNotIn("na", ttycatd.state_message(self.Source(), None, None, True))


if __name__ == "__main__":
    unittest.main()
