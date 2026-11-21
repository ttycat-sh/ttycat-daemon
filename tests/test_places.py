"""Places an agent's terminal can be in, whose screens can be read. tmux here is a table and a list of calls."""
import sys
import unittest
from pathlib import Path
from unittest import mock

DAEMON = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DAEMON))
import claude_sessions as cs  # noqa: E402
import discovery as d  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
