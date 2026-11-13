"""Names people give their agents: kept by ttycat, for every kind of agent alike. herdr here is a list in memory."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

DAEMON = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DAEMON))
import ttycatd  # noqa: E402
from agent_names import KEEP_PROCESS_S, KEEP_S, NAME_MAX, AgentNames, clean_name, for_board  # noqa: E402
from petstate import Pet  # noqa: E402
from sources import DemoSource, Fleet, HerdrSource  # noqa: E402


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


class Source(HerdrSource):
    """A source whose agents are a list: herdr itself, or one of the kinds found outside it."""

    def __init__(self, listed=()):
        super().__init__(pet=Pet(None))
        self.listed = list(listed)

    def _logs(self):
        return None

    def _follow(self, agents, now):
        pass

    def _herdr(self, *args):
        return {"result": {"agents": self.listed}}

    def poll(self):
        self.last_poll = 0.0
        return super().poll()


class Remote(Source):
    """Another machine's, as an extension adds them."""
    host, label, stale, hub = "studio", "studio", False, None

    @property
    def rows(self):
        return self.poll()


def agent(pane, cwd="/work/shop", **more):
    return {"pane_id": pane, "agent": "claude", "agent_status": "working", "cwd": cwd, **more}


def fleet(*agents, names=None, **more):
    made = Fleet(Source(agents), [], start=False, names=names, **more)
    made.poll()  # as the daemon does all the time: it's what tells it who's there
    return made


def rows(fleet):
    """As the app is told: the board's rows, with what's only for the app added."""
    listed = fleet.poll()
    desk = fleet.desk()
    return {row["id"]: {**row, **desk.get(row["id"], {})} for row in listed}


class WhatANameIs(unittest.TestCase):
    def test_a_name_is_one_line_of_what_can_be_shown(self):
        self.assertEqual(clean_name("  the   login\n fix \x07 "), "the login fix")
        self.assertEqual(clean_name("Zoë's refactor ✨"), "Zoë's refactor ✨")
        self.assertEqual(len(clean_name("x" * 200)), NAME_MAX)
        self.assertEqual(clean_name(None), "")
        self.assertEqual(clean_name(12), "")

    def test_the_board_gets_it_in_the_letters_it_has(self):
        self.assertEqual(for_board("Zoë's refactor ✨"), "Zoe's refactor")
        self.assertEqual(for_board("修正"), "")


class Kept(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.path = Path(tempfile.mkdtemp()) / "state" / "agent-names.json"

    def names(self):
        return AgentNames(self.path, self.clock)

    def test_a_name_is_there_after_a_restart(self):
        self.names().set("term_1", "  login fix ")
        self.assertEqual(self.names().get("term_1"), "login fix")
        self.assertEqual(self.names().get("term_2"), "")

    def test_no_name_forgets_the_one_it_had(self):
        self.names().set("term_1", "login fix")
        self.names().set("term_1", "   ")
        self.assertEqual(self.names().get("term_1"), "")
        self.assertEqual(json.loads(self.path.read_text()), {"names": {}})

    def test_a_file_that_isnt_ours_is_no_names(self):
        self.path.parent.mkdir(parents=True)
        for text in ("not json", "[]", '{"names": {"a": "b"}}', '{"names": {"a": {"name": 3, "seen": "x"}}}'):
            self.path.write_text(text)
            self.assertEqual(self.names().names, {})

    def test_a_name_outlives_its_agent_by_a_month(self):
        names = self.names()
        names.set("term_1", "login fix")
        self.clock.now += KEEP_S - 60
        names.tidy()
        self.assertEqual(names.get("term_1"), "login fix")  # and asking says it's still here
        self.clock.now += KEEP_S - 60
        names.tidy()
        self.assertEqual(names.get("term_1"), "login fix")
        self.clock.now += KEEP_S + 61
        names.tidy()
        self.assertEqual(names.get("term_1"), "")
        self.assertEqual(self.names().get("term_1"), "")

    def test_one_known_by_its_process_is_forgotten_soon_after(self):
        names = self.names()
        names.set("p:4242", "the migration")
        names.set("s:0123abcd", "the review")
        self.clock.now += KEEP_PROCESS_S + 61
        names.tidy()
        self.assertEqual(names.get("p:4242"), "")  # that number will be another process's
        self.assertEqual(names.get("s:0123abcd"), "the review")

    def test_a_process_that_ended_while_the_daemon_was_down_gets_its_ten_minutes(self):
        self.names().set("p:4242", "the migration")
        self.clock.now += 86400
        names = self.names()
        names.tidy()
        self.assertEqual(names.get("p:4242"), "the migration")

    def test_which_are_still_here_is_written_down_now_and_then(self):
        names = self.names()
        names.set("term_1", "login fix")
        self.clock.now += 7200
        names.get("term_1")
        names.tidy()
        self.assertEqual(json.loads(self.path.read_text())["names"]["term_1"]["seen"], self.clock.now)


class Named(unittest.TestCase):
    def test_an_agent_goes_by_its_folder_until_its_given_a_name(self):
        f = fleet(agent("w1:p1"))
        self.assertEqual(rows(f)["w1:p1"]["p"], "shop")
        self.assertNotIn("name", rows(f)["w1:p1"])
        self.assertEqual(f.rename("w1:p1", "  the login fix "), (True, ""))
        row = rows(f)["w1:p1"]
        self.assertEqual((row["p"], row["name"], row["folder"]), ("the login fix", "the login fix", "shop"))

    def test_the_board_isnt_sent_whats_only_for_the_app(self):
        f = fleet(agent("w1:p1"))
        f.rename("w1:p1", "the login fix")
        self.assertEqual(set(f.poll()[0]), {"id", "a", "n", "p", "age", "s", "d"})

    def test_two_in_one_folder_can_be_told_apart(self):
        f = fleet(agent("w1:p1"), agent("w1:p2"))
        f.rename("w1:p2", "the tests")
        self.assertEqual({sid: row["p"] for sid, row in rows(f).items()}, {"w1:p1": "shop", "w1:p2": "the tests"})

    def test_the_board_lists_it_in_its_own_letters_and_the_app_as_typed(self):
        f = fleet(agent("w1:p1"))
        f.rename("w1:p1", "Zoë's very long refactor of everything ✨")
        row = rows(f)["w1:p1"]
        self.assertEqual(row["p"], "Zoe's very long refactor of")
        self.assertEqual(row["name"], "Zoë's very long refactor of everything ✨")

    def test_a_name_the_board_cant_write_leaves_it_the_folder(self):
        f = fleet(agent("w1:p1"))
        f.rename("w1:p1", "修正")
        row = rows(f)["w1:p1"]
        self.assertEqual((row["p"], row["name"], row["folder"]), ("shop", "修正", "shop"))

    def test_no_name_takes_it_back(self):
        f = fleet(agent("w1:p1"))
        f.rename("w1:p1", "the login fix")
        self.assertEqual(f.rename("w1:p1", ""), (True, ""))
        self.assertEqual(rows(f)["w1:p1"]["p"], "shop")
        self.assertNotIn("name", rows(f)["w1:p1"])

    def test_one_thats_gone_cant_be_named(self):
        f = fleet(agent("w1:p1"))
        f.poll()
        for sid in ("w1:p9", "", "2/w1:p1", "s:nothing"):
            self.assertEqual(f.rename(sid, "x"), (False, "that agent has gone"), sid)

    def test_a_name_stays_with_herdrs_terminal_not_its_pane(self):
        f = fleet(agent("w1:p1", terminal_id="term_a"))
        f.rename("w1:p1", "the login fix")
        f.local.listed = [agent("w1:p1", terminal_id="term_b")]  # that pane closed, and its number came round again
        self.assertEqual(rows(f)["w1:p1"]["p"], "shop")
        f.local.listed = [agent("w1:p7", terminal_id="term_a")]  # and the terminal it named moved
        self.assertEqual(rows(f)["w1:p7"]["p"], "the login fix")

    def test_what_herdr_calls_it_is_its_name_until_its_given_one_here(self):
        f = fleet(agent("w1:p1", name=" api  work "))
        self.assertEqual(rows(f)["w1:p1"]["name"], "api work")
        f.rename("w1:p1", "the login fix")
        self.assertEqual(rows(f)["w1:p1"]["name"], "the login fix")
        f.rename("w1:p1", "")
        self.assertEqual(rows(f)["w1:p1"]["name"], "api work")

    def test_it_works_the_same_outside_herdr(self):
        sessions, running, threads = Source([agent("s:0123abcd")]), Source([agent("p:4242")]), Source([agent("c:beef")])
        sessions.herdr_up = running.herdr_up = False
        sessions.procs, sessions.all_pids = {}, set()
        f = Fleet(Source(), [], start=False, sessions=sessions, running=running, threads=threads)
        f.poll()
        for sid, name in (("s:0123abcd", "in a terminal"), ("p:4242", "in tmux"), ("c:beef", "in the Codex app")):
            self.assertEqual(f.rename(sid, name), (True, ""))
        self.assertEqual({sid: row["name"] for sid, row in rows(f).items()},
                         {"s:0123abcd": "in a terminal", "p:4242": "in tmux", "c:beef": "in the Codex app"})

    def test_another_machines_are_kept_apart_from_this_macs(self):
        f = Fleet(Source([agent("w1:p1", terminal_id="term_a")]), [Remote([agent("w1:p1", terminal_id="term_a")])], start=False)
        f.poll()
        self.assertEqual(f.rename("1/w1:p1", "on the studio"), (True, ""))
        named = rows(f)
        self.assertEqual((named["1/w1:p1"]["p"], named["w1:p1"]["p"]), ("on the studio", "shop"))
        self.assertEqual(f.name_key("1/w1:p1"), "studio/term_a")

    def test_a_machine_that_cant_be_reached_has_nothing_to_name(self):
        remote = Remote([agent("w1:p1")])
        remote.stale, remote.clock, remote.heard_at = True, lambda: 10.0, 0.0
        f = Fleet(Source(), [remote], start=False)
        self.assertEqual(f.poll()[0]["p"], "not reachable")
        self.assertEqual(f.rename("1/down", "x"), (False, "that agent has gone"))

    def test_names_are_kept_across_a_restart(self):
        path = Path(tempfile.mkdtemp()) / "agent-names.json"
        fleet(agent("w1:p1", terminal_id="term_a"), names=AgentNames(path)).rename("w1:p1", "the login fix")
        again = fleet(agent("w1:p1", terminal_id="term_a"), names=AgentNames(path))
        self.assertEqual(rows(again)["w1:p1"]["name"], "the login fix")

    def test_by_default_nothing_is_written(self):
        self.assertIsNone(fleet().names.path)


class FromTheApp(unittest.TestCase):
    def test_the_app_names_an_agent_and_takes_it_back(self):
        f = fleet(agent("w1:p1"))
        f.poll()
        self.assertEqual(ttycatd.name_agent(f, {"t": "agent-name", "id": "w1:p1", "name": "the login fix"}), {"ok": True, "msg": ""})
        self.assertEqual(rows(f)["w1:p1"]["name"], "the login fix")
        self.assertEqual(ttycatd.name_agent(f, {"t": "agent-name", "id": "w1:p1"}), {"ok": True, "msg": ""})
        self.assertNotIn("name", rows(f)["w1:p1"])

    def test_what_isnt_a_name_or_an_agent_is_refused_or_ignored(self):
        f = fleet(agent("w1:p1"))
        f.poll()
        self.assertEqual(ttycatd.name_agent(f, {"t": "agent-name", "id": "nope", "name": "x"}), {"ok": False, "msg": "that agent has gone"})
        self.assertEqual(ttycatd.name_agent(f, {"t": "agent-name", "id": "w1:p1", "name": ["x"]}), {"ok": True, "msg": ""})
        self.assertNotIn("name", rows(f)["w1:p1"])
        self.assertEqual(ttycatd.name_agent(object(), {"id": "w1:p1", "name": "x"}), {"ok": False, "msg": "agents can't be named here"})

    def test_naming_isnt_something_the_board_can_ask_for(self):
        self.assertNotIn("agent-name", ttycatd.APP_ACTIONS)
        self.assertEqual(Source([agent("w1:p1")]).act("agent-name", "w1:p1", ""), (False, "unknown action"))

    def test_the_demo_can_be_named_too_and_keeps_nothing(self):
        demo = DemoSource()
        self.assertEqual(ttycatd.name_agent(demo, {"id": "d4", "name": "the cart"}), {"ok": True, "msg": ""})
        row = next(row for row in demo.poll() if row["id"] == "d4")
        self.assertEqual((row["p"], demo.desk()["d4"]), ("the cart", {"name": "the cart", "folder": "web-shop"}))
        self.assertEqual(next(s for s in demo.all if s["id"] == "d4")["p"], "web-shop")
        self.assertEqual(ttycatd.name_agent(demo, {"id": "d4", "name": ""}), {"ok": True, "msg": ""})
        self.assertEqual(demo.desk(), {})
        self.assertEqual(ttycatd.name_agent(demo, {"id": "zz", "name": "x"}), {"ok": False, "msg": "that agent has gone"})


if __name__ == "__main__":
    unittest.main()
