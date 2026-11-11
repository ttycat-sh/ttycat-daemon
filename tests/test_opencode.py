"""What OpenCode's own server says about its subagents and shells. The server here is a table."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

DAEMON = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DAEMON))
import opencode  # noqa: E402
from harness import OpenCode, Running  # noqa: E402
from test_harness import stayed  # noqa: E402

NOW = 1_000_000.0
SHOP, DOCS = "/work/shop", "/work/docs"


def session(parent=None, where=SHOP):
    return {"parentID": parent, "location": {"directory": where}} if parent else {"location": {"directory": where}}


def shell(started=NOW - 60, status="running", by="ses_main"):
    return {"id": "sh_1", "status": status, "metadata": {"sessionID": by} if by else {}, "time": {"started": started * 1000}}


class Table:
    def __init__(self):
        self.active, self.sessions, self.opened, self.shells = {}, {}, [SHOP], {}
        self.asked, self.up, self.clock = [], True, 0.0

    def get(self, url, password, path):
        self.asked.append(path)
        if not self.up:
            return None
        if path == "/api/session/active":
            return {sid: {"type": "running"} for sid in self.active}
        if path == "/api/debug/location":
            return [{"directory": d} for d in self.opened]
        if path.startswith("/api/session/"):
            return self.sessions.get(path.rsplit("/", 1)[-1])
        if path.startswith("/api/shell?"):
            return next((found for place, found in self.shells.items() if place.replace("/", "%2F") in path), [])
        return None

    def server(self, service=lambda: ("http://127.0.0.1:4000", "secret")):
        return opencode.Server(service=service, get=self.get, clock=lambda: self.clock, now=lambda: NOW)


class Subagents(unittest.TestCase):
    def test_sessions_at_work_that_another_started_are_subagents(self):
        table = Table()
        table.sessions = {"ses_main": session(), "ses_a": session("ses_main"), "ses_b": session("ses_main"),
                          "ses_other": session("ses_x", DOCS)}
        table.active = dict.fromkeys(table.sessions)
        seen = table.server().look()
        self.assertEqual(seen.subagents, {SHOP: 2, DOCS: 1})

    def test_one_that_finished_is_no_longer_counted(self):
        table = Table()
        table.sessions = {"ses_a": session("ses_main")}
        table.active = {"ses_a": None}
        server = table.server()
        self.assertEqual(server.look().subagents, {SHOP: 1})
        table.active, table.clock = {}, 5.0
        self.assertEqual(server.look().subagents, {})

    def test_a_session_is_asked_about_once(self):
        table = Table()
        table.sessions = {"ses_a": session("ses_main")}
        table.active = {"ses_a": None}
        server = table.server()
        server.look()
        table.clock = 5.0
        server.look()
        self.assertEqual(table.asked.count("/api/session/ses_a"), 1)


class Shells(unittest.TestCase):
    def test_those_a_session_left_running(self):
        table = Table()
        table.shells = {SHOP: [shell(), shell(status="exited"), shell(by=None), shell(started=NOW - 1)]}
        self.assertEqual(table.server().look().shells, {SHOP: 1})

    def test_a_place_the_server_has_not_opened_is_not_asked_about(self):
        table = Table()
        table.opened = [SHOP]
        table.server().look()
        self.assertEqual([p for p in table.asked if p.startswith("/api/shell")], ["/api/shell?location%5Bdirectory%5D=%2Fwork%2Fshop"])

    def test_nothing_is_ever_sent_but_a_get_of_these(self):
        table = Table()
        table.sessions = {"ses_a": session("ses_main")}
        table.active = {"ses_a": None}
        table.server().look()
        self.assertTrue(all(p.startswith(("/api/session/", "/api/debug/location", "/api/shell?")) for p in table.asked))


class Reaching(unittest.TestCase):
    def test_no_server_no_answer(self):
        self.assertIsNone(Table().server(service=lambda: None).look())

    def test_one_that_does_not_answer_is_left_alone_for_a_while(self):
        table = Table()
        table.up = False
        server = table.server()
        self.assertIsNone(server.look())
        asked = len(table.asked)
        table.clock = 10.0
        self.assertIsNone(server.look())
        self.assertEqual(len(table.asked), asked)
        table.up, table.clock = True, 31.0
        self.assertIsNotNone(server.look())

    def test_it_looks_only_every_couple_of_seconds(self):
        table = Table()
        server = table.server()
        server.look()
        asked = len(table.asked)
        table.clock = 1.0
        server.look()
        self.assertEqual(len(table.asked), asked)

    def service_file(self, **info):
        path = Path(tempfile.mkdtemp()) / "service.json"
        path.write_text(json.dumps(info))
        return path

    def test_the_server_is_where_its_file_says(self):
        path = self.service_file(url="http://127.0.0.1:49374/", password="secret", pid=1)
        self.assertEqual(opencode.read_service(path), ("http://127.0.0.1:49374", "secret"))

    def test_the_password_never_goes_to_another_machine(self):
        self.assertIsNone(opencode.read_service(self.service_file(url="http://example.com:80", password="secret")))
        self.assertIsNone(opencode.read_service(self.service_file(url="http://127.0.0.1.example.com:80", password="secret")))

    def test_a_file_that_is_not_as_expected_is_no_server(self):
        self.assertIsNone(opencode.read_service(Path("/nonexistent/service.json")))
        self.assertIsNone(opencode.read_service(self.service_file()))


class Counts(unittest.TestCase):
    def table(self):
        table = Table()
        table.sessions = {"ses_a": session("ses_main")}
        table.active = {"ses_a": None}
        table.shells = {SHOP: [shell()]}
        return table

    def test_its_own_record_so_neither_is_a_guess(self):
        kind = OpenCode(server=self.table().server())
        self.assertEqual(kind.counts(Running(cwd=SHOP), stayed([])), {"sub": 1, "sh": 1})

    def test_a_folder_with_nothing_going_has_none(self):
        self.assertEqual(OpenCode(server=self.table().server()).counts(Running(cwd=DOCS), stayed([])), {"sub": 0, "sh": 0})

    def test_two_in_one_folder_cannot_be_told_apart(self):
        kind = OpenCode(server=self.table().server())
        self.assertEqual(kind.counts(Running(cwd=SHOP, alone=False), stayed([])), {})

    def test_without_its_server_it_is_an_agent_in_a_terminal(self):
        kind = OpenCode(server=Table().server(service=lambda: None))
        processes = stayed([(10, 1, "opencode"), (11, 10, "bash -c 'pnpm dev'")])
        self.assertEqual(kind.counts(Running(pid=10, cwd=SHOP), processes), {"sh": 1, "guess": ["sh"]})
        self.assertEqual(kind.counts(Running(cwd=SHOP), processes), {})


if __name__ == "__main__":
    unittest.main()
