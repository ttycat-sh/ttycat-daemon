"""Places an agent's terminal can be in, whose screens can be read. tmux here is a table and a list of calls,
and a session's host is what it would have sent, put where the reader finds it."""
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

DAEMON = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DAEMON))
sys.path.insert(0, str(DAEMON / "tests"))
import claude_sessions as cs  # noqa: E402
import discovery as d  # noqa: E402
import fake_host  # noqa: E402
import paths  # noqa: E402
import places  # noqa: E402
import screen_rules as sr  # noqa: E402
from petstate import Pet  # noqa: E402

RULES = sr.ScreenRules()


class InTmux(unittest.TestCase):
    def place(self, panes="@@tmux /tmp/tmux-1/default /dev/t %4\n", screen=("a title", "a screen")):
        self.asked, self.calls = [], []

        def capture(socket, pane):
            self.asked.append((socket, pane))
            return screen

        def run(command, **kw):
            self.calls.append(command)
            return mock.Mock(returncode=0)
        self.listed = 0

        def listing():
            self.listed += 1
            return panes
        return places.TmuxPlace(panes=listing, capture=capture, run=run)

    def test_an_agent_is_found_by_its_terminal(self):
        here, elsewhere = d.Found(7, "codex", "t"), d.Found(8, "codex", "u")
        self.assertEqual(self.place().find([here, elsewhere]), {7: ("/tmp/tmux-1/default", "%4")})

    def test_one_found_is_tmuxs_and_not_herdrs(self):
        found = d.Found(7, "codex", "t", in_herdr=True)
        self.place().find([found])
        self.assertEqual((found.tmux_socket, found.tmux_pane, found.in_herdr), ("/tmp/tmux-1/default", "%4", False))

    def test_tmux_isnt_asked_with_no_agent_to_place(self):
        self.assertEqual(self.place().find([]), {})
        self.assertEqual(self.listed, 0)

    def test_a_socket_that_isnt_safe_in_a_command_is_no_place(self):
        place = self.place(panes="@@tmux /tmp/a;b /dev/t %4\n")
        self.assertEqual(place.find([d.Found(7, "codex", "t")]), {})

    def test_it_shows_the_panes_title_and_screen(self):
        place = self.place()
        self.assertEqual(place.shown(("/tmp/tmux-1/default", "%4")), places.Shown("a title", "a screen"))
        self.assertEqual(self.asked, [("/tmp/tmux-1/default", "%4")])

    def test_a_pane_that_cant_be_read_shows_nothing(self):
        self.assertIsNone(self.place(screen=None).shown(("/tmp/tmux-1/default", "%4")))

    def test_it_goes_to_the_panes_window_then_the_pane(self):
        self.place().bring_forward(("/tmp/tmux-1/work", "%3"))
        self.assertEqual(self.calls, [["tmux", "-S", "/tmp/tmux-1/work", "select-window", "-t", "%3"],
                                      ["tmux", "-S", "/tmp/tmux-1/work", "select-pane", "-t", "%3"]])

    def test_without_a_socket_its_the_server_tmux_finds(self):
        self.place().bring_forward(("", "%3"))
        self.assertEqual(self.calls[0], ["tmux", "select-window", "-t", "%3"])

    def test_a_place_that_says_nothing_has_nothing(self):
        nowhere = places.Place()
        self.assertEqual((nowhere.find([d.Found(7, "codex", "t")]), nowhere.shown("x"), nowhere.bring_forward("x")), ({}, None, None))


class Somewhere(places.Place):
    """A place that isn't tmux: it knows a terminal by a name."""

    def __init__(self, holds, shown=None):
        self.holds, self.showing, self.asked, self.brought = holds, shown, [], []

    def find(self, found):
        return {f.pid: self.holds[f.tty] for f in found if f.tty in self.holds}

    def shown(self, held):
        self.asked.append(held)
        return self.showing

    def bring_forward(self, held):
        self.brought.append(held)


class AnAgentInAPlace(unittest.TestCase):
    def source(self, *where, env=None, app="/Applications/iTerm.app"):
        self.calls = []

        def run(command, **kw):
            self.calls.append(command)
            return mock.Mock(returncode=0)
        return cs.RunningAgents(pet=Pet(None), scan=lambda: [(7, 7, 7, "t", "codex")], env_of=lambda p: env or {},
                                cwd_of=lambda p: "/w/api", rules=RULES, places=where, parent_check=lambda pid: False,
                                app_of=lambda p: app, run=run)

    def poll(self, source):
        source.last_poll = 0
        with mock.patch.object(source, "_follow"):
            return source.poll()[0]

    def test_its_screen_says_what_its_doing_whatever_the_place(self):
        place = Somewhere({"t": "one"}, places.Shown("⠋ Thinking", "x\n"))
        self.assertEqual(self.poll(self.source(place))["s"], "working")
        self.assertEqual(place.asked, ["one"])

    def test_a_terminal_is_in_the_first_place_that_has_it(self):
        first, second = Somewhere({"t": "one"}, places.Shown("", "x\n")), Somewhere({"t": "two"}, places.Shown("", "x\n"))
        self.poll(self.source(first, second))
        self.assertEqual((first.asked, second.asked), (["one"], []))

    def test_in_no_place_its_running_and_no_more_is_known(self):
        place = Somewhere({})
        row = self.poll(self.source(place))
        self.assertEqual((row["s"], row["d"], place.asked), ("idle", "state unknown", []))

    def test_opening_it_brings_its_terminal_forward_there(self):
        place = Somewhere({"t": "one"})
        source = self.source(place)
        self.poll(source)
        self.assertEqual(source.act("open", "p:7", ""), (True, ""))
        self.assertEqual((place.brought, self.calls), (["one"], [["open", "-a", "/Applications/iTerm.app"]]))

    def test_in_no_place_tmux_is_asked_by_what_it_told_the_process(self):
        source = self.source(Somewhere({}), env={"TMUX": "/tmp/tmux-1/work,5,0", "TMUX_PANE": "%3"})
        self.poll(source)
        source.act("open", "p:7", "")
        self.assertEqual(self.calls[0], ["tmux", "-S", "/tmp/tmux-1/work", "select-window", "-t", "%3"])


RECORD = fake_host.FRAMES["record"]
SHOWS = fake_host.written("from_host", "it_shows")
QUIET = fake_host.written("from_host", "it_shows_and_says_nothing_of_itself")


class Hosted(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: [f.unlink() for f in self.folder.iterdir()] and None or self.folder.rmdir())
        self.now, self.running, self.opened = 100.0, {RECORD["host"]}, []

    def write(self, name=RECORD["id"], **changed):
        record = {**RECORD, "id": name, **changed}
        (self.folder / f"{name}.json").write_text(json.dumps({k: v for k, v in record.items() if v is not None}))

    def place(self, *hosts, patience=0.0):
        waiting = list(hosts)

        def connect(path):
            self.opened.append(path)
            if not waiting:
                raise ConnectionRefusedError
            return waiting.pop(0)
        return places.HostedPlace(self.folder, connect=connect, alive=lambda pid: pid in self.running,
                                  clock=lambda: self.now, patience=patience)

    def held(self, place):
        return place.find([d.Found(7, "codex", "ttys012")])


class ItsRecords(Hosted):
    def test_an_agent_is_found_by_its_sessions_terminal(self):
        self.write()
        place = self.place()
        self.assertEqual(place.find([d.Found(7, "codex", "ttys012"), d.Found(8, "codex", "ttys013")]), {7: RECORD["id"]})
        self.assertTrue(place.knows(RECORD["id"]))

    def test_finding_asks_no_host_anything(self):
        self.write()
        self.held(self.place())
        self.assertEqual(self.opened, [])

    def test_a_host_of_an_older_kind_is_never_spoken_to(self):
        self.write(v=1)
        place = self.place(fake_host.FakeHost(SHOWS))
        self.assertEqual(self.held(place), {})
        self.assertIsNone(place.shown(RECORD["id"]))
        self.assertEqual(self.opened, [])

    def test_a_newer_one_is_read_as_far_as_its_known(self):
        self.write(v=3, something_new="x")
        self.assertEqual(self.held(self.place()), {7: RECORD["id"]})

    def test_a_host_that_has_ended_holds_nothing(self):
        self.write()
        self.running = set()
        place = self.place()
        self.assertEqual((self.held(place), place.knows(RECORD["id"])), ({}, False))

    def test_what_isnt_a_record_is_passed_over(self):
        self.write()
        (self.folder / "half.json").write_text('{"v": 2, "id": "ha')
        (self.folder / "list.json").write_text("[1, 2]")
        self.write("other", id="not-its-file")
        for wrong in ({"host": "4242"}, {"host": True}, {"host": 1}, {"v": "2"}, {"v": True}, {"id": 5}):
            self.write("odd", **{"id": "odd", **wrong})
            self.assertEqual(set(self.place()._read_records()), {RECORD["id"]}, wrong)

    def test_a_name_that_could_leave_the_folder_is_no_session(self):
        for name in ("..", ".hidden", "a/b", "", "x" * 65):
            self.assertIsNone(places.SESSION_ID.fullmatch(name), name)

    def test_without_a_terminal_named_it_holds_no_agent(self):
        self.write(tty=None)
        place = self.place()
        self.assertEqual((self.held(place), place.knows(RECORD["id"])), ({}, True))

    def test_a_folder_that_isnt_there_has_no_sessions(self):
        place = places.HostedPlace(self.folder / "none")
        self.assertEqual(place.find([d.Found(7, "codex", "ttys012")]), {})

    def test_the_folder_is_the_state_folders_unless_its_told_another(self):
        with mock.patch.dict(os.environ, {"TTYCAT_HOME": "/somewhere/state"}):
            os.environ.pop("TTYCAT_SESSIONS_DIR", None)
            self.assertEqual(places.HostedPlace().folder, Path("/somewhere/state/sessions"))
            with mock.patch.dict(os.environ, {"TTYCAT_SESSIONS_DIR": "/elsewhere"}):
                self.assertEqual(paths.sessions_dir(), Path("/elsewhere"))
            with mock.patch.dict(os.environ, {"TTYCAT_SESSIONS_DIR": "not/a/whole/path"}):
                self.assertEqual(paths.sessions_dir(), Path("/somewhere/state/sessions"))


class WhatItShows(Hosted):
    def looking(self, *hosts, **kw):
        self.write()
        place = self.place(*hosts, **kw)
        self.held(place)
        return place

    def test_it_says_its_title_screen_progress_and_what_its_program_says(self):
        host = fake_host.FakeHost(SHOWS)
        shown = self.looking(host).shown(RECORD["id"])
        said = fake_host.FRAMES["from_host"]["it_shows"]["says"]
        self.assertEqual(shown, places.Shown(said["title"], said["text"], said["progress"], said["said"]))
        self.assertEqual(self.opened, [self.folder / f"{RECORD['id']}.sock"])

    def test_a_program_that_says_nothing_of_itself_is_left_to_its_screen(self):
        shown = self.looking(fake_host.FakeHost(QUIET)).shown(RECORD["id"])
        self.assertEqual((shown.said, shown.text), (None, "$ \n"))

    def test_it_greets_as_a_reader_and_only_ever_asks(self):
        host = fake_host.FakeHost(SHOWS, QUIET, SHOWS)
        place = self.looking(host)
        for _ in range(3):
            place.shown(RECORD["id"])
        self.assertEqual(host.kinds_sent(), [b"h", b"q", b"q", b"q"])
        self.assertEqual(host.sent[:len(fake_host.written("to_host", "hello_as_reader"))], fake_host.written("to_host", "hello_as_reader"))
        self.assertEqual(self.opened, [self.folder / f"{RECORD['id']}.sock"])   # one socket, kept

    def test_what_it_writes_is_what_the_frames_say(self):
        for name, wrote in (("hello_as_reader", places.frame(b"h", json.dumps({"v": places.SPEAKS, "role": "read"}).encode())),
                            ("what_does_it_show", places.frame(b"q"))):
            kind, payload = fake_host.frames_in(wrote)[0]
            listed = fake_host.FRAMES["to_host"][name]
            self.assertEqual((kind.decode(), json.loads(payload) if payload else None), (listed["kind"], listed["says"]))
            self.assertEqual(wrote, fake_host.written("to_host", name))

    def test_an_answer_that_comes_late_counts_the_next_time_and_isnt_asked_for_twice(self):
        host = fake_host.FakeHost(SHOWS, late=True)
        place = self.looking(host)
        self.assertIsNone(place.shown(RECORD["id"]))
        self.assertIsNone(place.shown(RECORD["id"]))
        host.arrives()
        self.assertEqual(place.shown(RECORD["id"]).title, "⠋ Thinking")
        self.assertEqual(host.kinds_sent(), [b"h", b"q"])

    def test_a_host_thats_slow_is_waited_for_once_and_no_longer_than_its_given(self):
        def waited(seconds):
            self.now += seconds
        host = fake_host.FakeHost(waited=waited)   # it never answers
        place = self.looking(host, patience=0.25)
        before = self.now
        self.assertIsNone(place.shown(RECORD["id"]))
        asked = len(host.waits)
        self.assertEqual((max(host.waits), self.now - before), (0.25, 0.25))   # its hello came at once, then nothing
        self.assertIsNone(place.shown(RECORD["id"]))
        self.assertEqual((host.waits[asked:], self.now - before), ([0.0], 0.25))   # the next look only takes what's there

    def test_while_its_answer_is_late_the_last_one_stands(self):
        host = fake_host.FakeHost(SHOWS)
        place = self.looking(host)
        first = place.shown(RECORD["id"])
        self.assertEqual(place.shown(RECORD["id"]), first)   # asked again, nothing came
        self.assertEqual(host.kinds_sent(), [b"h", b"q", b"q"])

    def test_one_that_says_nothing_for_long_is_let_go_and_asked_afresh(self):
        silent, fresh = fake_host.FakeHost(SHOWS), fake_host.FakeHost(QUIET)
        place = self.looking(silent, fresh)
        place.shown(RECORD["id"])
        self.now += places.SILENT_FOR_S
        self.assertIsNone(place.shown(RECORD["id"]))
        self.assertTrue(silent.closed)
        self.assertEqual(place.shown(RECORD["id"]).text, "$ \n")
        self.assertEqual(len(self.opened), 2)

    def test_a_host_that_went_away_shows_nothing(self):
        host = fake_host.FakeHost(SHOWS)
        place = self.looking(host)
        place.shown(RECORD["id"])
        host.gone = True
        self.assertIsNone(place.shown(RECORD["id"]))
        self.assertTrue(host.closed)

    def test_one_that_cant_be_reached_shows_nothing(self):
        self.assertIsNone(self.looking().shown(RECORD["id"]))

    def test_a_session_thats_no_longer_there_isnt_asked_for(self):
        host = fake_host.FakeHost(SHOWS)
        place = self.looking(host)
        place.shown(RECORD["id"])
        (self.folder / f"{RECORD['id']}.json").unlink()
        self.held(place)
        self.assertTrue(host.closed)
        self.assertIsNone(place.shown(RECORD["id"]))
        self.assertEqual(len(self.opened), 1)

    def test_a_frame_too_large_to_be_a_screen_ends_it(self):
        host = fake_host.FakeHost(b"Q" + (places.LARGEST_ANSWER + 1).to_bytes(4, "big"))
        self.assertIsNone(self.looking(host).shown(RECORD["id"]))
        self.assertTrue(host.closed)

    def test_frames_it_doesnt_know_are_skipped(self):
        host = fake_host.FakeHost(fake_host.says(b"Z", {"new": 1}) + fake_host.frame(b"o", b"bytes") + SHOWS)
        self.assertEqual(self.looking(host).shown(RECORD["id"]).title, "⠋ Thinking")

    def test_an_answer_is_read_loosely(self):
        self.assertIsNone(places.shown_in(b"not json"))
        self.assertIsNone(places.shown_in(b"[1]"))
        odd = places.shown_in(json.dumps({"title": 5, "text": "x" * (places.LONGEST_SCREEN + 9), "said": {"state": "dancing"},
                                          "progress": None, "more": "ignored"}).encode())
        self.assertEqual((odd.title, len(odd.text), odd.progress, odd.said), ("", places.LONGEST_SCREEN, "", None))
        self.assertIsNone(places.shown_in(json.dumps({"said": "working"}).encode()).said)

    def test_over_a_real_socket(self):
        # Short enough for a socket's path, wherever the tests' own folder is.
        folder = Path(tempfile.mkdtemp(dir="/tmp"))
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(lambda: (listener.close(), [f.unlink() for f in folder.iterdir()], folder.rmdir()))
        listener.bind(str(folder / f"{RECORD['id']}.sock"))
        listener.listen(1)
        (folder / f"{RECORD['id']}.json").write_text(json.dumps(RECORD))
        place = places.HostedPlace(folder, alive=lambda pid: True, clock=lambda: self.now, patience=0.0)
        self.addCleanup(lambda: [host.close() for host in place.hosts.values()])
        self.held(place)
        self.assertIsNone(place.shown(RECORD["id"]))   # asked: nothing has answered yet
        host, _ = listener.accept()
        self.addCleanup(host.close)
        host.settimeout(5)
        asked = host.recv(4096)
        self.assertEqual([kind for kind, _ in fake_host.frames_in(asked)], [b"h", b"q"])
        host.sendall(fake_host.written("from_host", "hello_back") + SHOWS)
        self.assertEqual(place.shown(RECORD["id"]).title, "⠋ Thinking")


if __name__ == "__main__":
    unittest.main()
