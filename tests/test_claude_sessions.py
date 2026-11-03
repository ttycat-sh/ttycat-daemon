"""Claude sessions outside herdr. The registry here is a temp folder; nothing real is read."""
import json
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

DAEMON = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DAEMON))
import approvals  # noqa: E402
import claude_sessions as cs  # noqa: E402
from petstate import Pet  # noqa: E402
from sources import Fleet, HerdrSource  # noqa: E402

SID = "5d6d8b61-343d-4d8b-b463-a82fe6f71940"
PANE = "s:5d6d8b61"


def entry(pid=100, session=SID, status="idle", **more):
    return {"pid": pid, "sessionId": session, "status": status, "cwd": "/work/shop", "kind": "interactive", **more}


class Registry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)

    def write(self, name, value):
        (self.folder / name).write_text(value if isinstance(value, str) else json.dumps(value))

    def read(self, dead=()):
        return cs.read_registry(self.folder, is_alive=lambda pid: pid not in dead)

    def test_running_sessions_are_listed(self):
        self.write("100.json", entry())
        self.assertEqual([e["sessionId"] for e in self.read()], [SID])

    def test_a_crash_leaves_a_file_that_doesnt_count(self):
        self.write("100.json", entry())
        self.assertEqual(self.read(dead={100}), [])

    def test_what_it_cant_understand_is_skipped_not_fatal(self):
        for name, bad in (("a.json", "{ half"), ("b.json", "[1]"), ("c.json", {"pid": "100", "sessionId": SID}),
                          ("d.json", {"pid": 100}), ("e.json", {"pid": 100, "sessionId": ""})):
            self.write(name, bad)
        self.write("100.json", entry())
        self.assertEqual(len(self.read()), 1)

    def test_other_kinds_of_session_arent_listed(self):
        self.write("100.json", entry(kind="print"))
        self.assertEqual(self.read(), [])

    def test_no_registry_at_all_means_no_rows(self):
        self.assertEqual(cs.read_registry(self.folder / "nothing"), [])


class Rows(unittest.TestCase):
    def source(self, entries, **kw):
        self.entries = entries
        made = cs.ClaudeSessions(pet=Pet(None), read=lambda folder: self.entries, env_of=kw.pop("env_of", lambda pid: {}), **kw)
        return made

    def poll(self, source):
        source.last_poll = 0
        with mock.patch.object(source, "_follow"):
            return source.poll()

    def test_a_row_says_what_a_herdr_row_says(self):
        row = self.poll(self.source([entry(status="busy")]))[0]
        self.assertEqual((row["id"], row["a"], row["n"], row["p"], row["s"]), (PANE, "CC", "Claude", "shop", "working"))

    def test_ids_fit_on_the_board_even_from_another_machine(self):
        self.assertLessEqual(len("1/" + cs.pane_of(SID)), 15)

    def test_finishing_is_done_and_counts_for_the_pet(self):
        source = self.source([entry(status="busy")])
        self.poll(source)
        before = source.pet_stats.total
        self.entries = [entry(status="idle")]
        self.assertEqual(self.poll(source)[0]["s"], "done")
        self.assertEqual(source.pet_stats.total, before + 1)

    def test_already_idle_at_start_is_idle_not_done(self):
        self.assertEqual(self.poll(self.source([entry(status="idle")]))[0]["s"], "idle")

    def test_seen_goes_back_to_idle(self):
        source = self.source([entry(status="busy")])
        self.poll(source)
        self.entries = [entry(status="idle")]
        self.poll(source)
        source.act("seen", PANE, "")
        self.assertEqual(self.poll(source)[0]["s"], "idle")

    def test_a_state_it_doesnt_know_says_so(self):
        self.assertEqual(self.poll(self.source([entry(status="new-in-3.0")]))[0]["d"], "state unknown")

    def test_a_session_that_ends_goes(self):
        source = self.source([entry(status="busy")])
        self.poll(source)
        self.entries = []
        self.assertEqual(self.poll(source), [])
        self.assertEqual(source.claude.ran, set())

    def test_waiting_without_the_hook_is_terminal_only(self):
        row = self.poll(self.source([entry(status="waiting", waitingFor="permission prompt")]))[0]
        self.assertEqual((row["s"], row["ask"]), ("blocked", "terminal"))
        self.assertIn("permission prompt", row["body"])

    def test_it_cant_be_answered_without_a_hook_request(self):
        source = self.source([entry(status="waiting")])
        row = self.poll(source)[0]
        self.assertFalse(source.act("allow", PANE, row["rev"])[0])

    def test_where_it_runs_is_asked_once_per_process(self):
        env_of = mock.Mock(return_value={"HERDR_PANE_ID": "w:p1"})
        source = self.source([entry()], env_of=env_of)
        source.herdr_up = True
        self.poll(source)
        self.poll(source)
        self.assertEqual(env_of.call_count, 1)


class NeverTwice(unittest.TestCase):
    def source(self, env):
        self.rows_entries = [entry()]
        return cs.ClaudeSessions(pet=Pet(None), read=lambda folder: self.rows_entries, env_of=lambda pid: env)

    def poll(self, source):
        source.last_poll = 0
        with mock.patch.object(source, "_follow"):
            return source.poll()

    def test_a_session_herdr_lists_is_left_to_herdr(self):
        source = self.source({})
        source.herdr_sessions = {SID}
        self.assertEqual(self.poll(source), [])

    def test_a_session_in_a_herdr_pane_is_left_to_herdr(self):
        source = self.source({"HERDR_PANE_ID": "w:p1"})
        source.herdr_up = True
        self.assertEqual(self.poll(source), [])

    def test_with_herdr_gone_it_isnt_lost(self):
        source = self.source({"HERDR_PANE_ID": "w:p1"})
        source.herdr_up = False
        self.assertEqual(len(self.poll(source)), 1)

    def test_tmux_inside_a_herdr_pane_is_ours(self):
        source = self.source({"HERDR_PANE_ID": "w:p1", "TMUX": "/tmp/s,1,0"})
        source.herdr_up = True
        self.assertEqual(len(self.poll(source)), 1)

    def test_any_other_terminal_is_ours(self):
        source = self.source({"TERM_PROGRAM": "Apple_Terminal"})
        source.herdr_up = True
        self.assertEqual(len(self.poll(source)), 1)

    def test_fleet_tells_it_what_herdr_has(self):
        local = mock.Mock(spec=HerdrSource, reachable=True,
                          agents={"w:p1": {"agent_session": {"value": SID}}})
        local.poll.return_value = [{"id": "w:p1", "s": "idle"}]
        sessions = self.source({})
        fleet = Fleet(local, [], start=False, sessions=sessions)
        with mock.patch.object(sessions, "_follow"):
            rows = fleet.poll()
        self.assertEqual([r["id"] for r in rows], ["w:p1"])

    def test_without_herdr_installed_fleet_still_shows_sessions(self):
        local = HerdrSource(pet=Pet(None), hub=None)
        sessions = self.source({})
        fleet = Fleet(local, [], start=False, sessions=sessions)
        with mock.patch("subprocess.run", side_effect=FileNotFoundError), mock.patch.object(sessions, "_follow"):
            local.last_poll = sessions.last_poll = 0
            rows = fleet.poll()
        self.assertEqual([r["id"] for r in rows], [PANE])


class Scopes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(self.tmp.cleanup)
        self.hub = approvals.Hub(Path(self.tmp.name) / "h.sock")
        self.addCleanup(self.hub.close)
        self.herdr = approvals.HubScope(self.hub, lambda p: not cs.is_session_pane(p))
        self.sessions = approvals.HubScope(self.hub, cs.is_session_pane)
        self.clients = []

    def ask(self, pane):
        import select
        client = socket.socket(socket.AF_UNIX)
        client.connect(str(self.hub.path))
        self.addCleanup(client.close)
        request = {"tool_name": "Bash", "tool_input": {"command": "touch a"}}
        client.sendall(json.dumps({"pane": pane, "request": request}).encode() + b"\n")
        for _ in range(4):
            for sock in select.select(self.hub.fds(), [], [], 1)[0]:
                self.hub.handle(sock)
            if pane in self.hub.pending:
                break
        return client

    def test_herdr_knowing_nothing_of_a_session_doesnt_end_its_prompt(self):
        self.ask(PANE)
        self.hub.pending[PANE].at -= 100
        self.herdr.prune({})
        self.herdr.keep_only(set())
        self.assertIn(PANE, self.hub.pending)

    def test_sessions_knowing_nothing_of_a_pane_dont_end_its_prompt(self):
        self.ask("w:p1")
        self.hub.pending["w:p1"].at -= 100
        self.sessions.prune({})
        self.sessions.keep_only(set())
        self.assertIn("w:p1", self.hub.pending)

    def test_a_prompt_answered_in_the_terminal_is_let_go(self):
        client = self.ask(PANE)
        self.hub.pending[PANE].at -= 100
        self.sessions.prune({PANE: "working"})
        self.assertNotIn(PANE, self.hub.pending)
        client.settimeout(1)
        self.assertEqual(client.recv(100), b"")  # nothing decided

    def test_a_scope_cant_see_or_answer_anothers_prompt(self):
        self.ask("w:p1")
        rev = self.hub.pending["w:p1"].id
        self.assertIsNone(self.sessions.get("w:p1"))
        self.assertFalse(self.sessions.answer("w:p1", rev, "allow")[0])
        self.assertIn("w:p1", self.hub.pending)

    def test_a_session_prompt_shows_and_is_answered_through_its_hook(self):
        client = self.ask(PANE)
        source = cs.ClaudeSessions(pet=Pet(None), hub=self.sessions, read=lambda f: [entry(status="waiting")], env_of=lambda p: {})
        with mock.patch.object(source, "_follow"):
            row = source.poll()[0]
        self.assertEqual((row["ask"], row["body"]), ("approve", "touch a"))
        self.assertEqual(source.act("allow", PANE, row["rev"], via="app"), (True, ""))
        client.settimeout(1)
        self.assertEqual(json.loads(client.recv(1000)), {"behavior": "allow"})


class HookKeys(unittest.TestCase):
    """hook.py picks the same name for a session as the source does."""

    def run_hook(self, env, request):
        tmp = tempfile.TemporaryDirectory(dir="/tmp")
        self.addCleanup(tmp.cleanup)
        path = f"{tmp.name}/h.sock"
        server = socket.socket(socket.AF_UNIX)
        server.bind(path)
        server.listen(1)
        server.settimeout(3)
        self.addCleanup(server.close)
        process = subprocess.Popen([sys.executable, "-I", str(DAEMON / "hook.py")], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env={"TTYCAT_HOOK_SOCK": path, **env})
        process.stdin.write(json.dumps(request).encode())
        process.stdin.close()
        self.addCleanup(process.kill)
        self.addCleanup(process.stdout.close)
        try:
            conn, _ = server.accept()
        except socket.timeout:
            return None
        self.addCleanup(conn.close)
        conn.settimeout(3)
        return json.loads(conn.recv(65536))["pane"]

    def test_outside_herdr_it_names_the_session(self):
        self.assertEqual(self.run_hook({}, {"session_id": SID, "tool_name": "Bash"}), cs.pane_of(SID))

    def test_in_herdr_it_names_the_pane(self):
        self.assertEqual(self.run_hook({"HERDR_PANE_ID": "w:p1"}, {"session_id": SID}), "w:p1")

    def test_tmux_inside_a_herdr_pane_is_out_of_herdrs_sight(self):
        self.assertEqual(self.run_hook({"HERDR_PANE_ID": "w:p1", "TMUX": "/tmp/s,1,0"}, {"session_id": SID}), cs.pane_of(SID))

    def test_with_nothing_to_go_on_it_stays_out_of_it(self):
        self.assertIsNone(self.run_hook({}, {"tool_name": "Bash"}))


class Opening(unittest.TestCase):
    def source(self, env=None, app="/Applications/iTerm.app"):
        self.calls = []

        def run(command, **kw):
            self.calls.append(command)
            return mock.Mock(returncode=0)
        source = cs.ClaudeSessions(pet=Pet(None), read=lambda f: [entry()], env_of=lambda p: env or {},
                                   app_of=lambda p: app, run=run)
        source.last_poll = 0
        with mock.patch.object(source, "_follow"):
            source.poll()
        return source

    def test_it_opens_the_app_its_in(self):
        self.assertEqual(self.source().act("open", PANE, ""), (True, ""))
        self.assertEqual(self.calls, [["open", "-a", "/Applications/iTerm.app"]])

    def test_failing_that_it_goes_by_the_terminals_name(self):
        self.source({"TERM_PROGRAM": "Apple_Terminal"}, app=None).act("open", PANE, "")
        self.assertEqual(self.calls, [["open", "-a", "Terminal"]])

    def test_in_tmux_it_goes_to_the_pane_first(self):
        self.source({"TMUX_PANE": "%3"}).act("open", PANE, "")
        self.assertEqual(self.calls[:2], [["tmux", "select-window", "-t", "%3"], ["tmux", "select-pane", "-t", "%3"]])

    def test_on_its_own_tmux_server(self):
        self.source({"TMUX": "/tmp/tmux-1/work,5,0", "TMUX_PANE": "%3"}).act("open", PANE, "")
        self.assertEqual(self.calls[0], ["tmux", "-S", "/tmp/tmux-1/work", "select-window", "-t", "%3"])

    def test_it_says_so_if_it_cant_tell(self):
        ok, msg = self.source({}, app=None).act("open", PANE, "")
        self.assertFalse(ok)
        self.assertIn("which terminal", msg)

    def test_a_session_thats_gone_cant_be_opened(self):
        self.assertFalse(self.source().act("open", "s:00000000", "")[0])

    def test_an_app_path_comes_out_of_a_process_path(self):
        self.assertEqual(cs.APP_IN_PATH.match("/Applications/Ghostty.app/Contents/MacOS/ghostty").group(1), "/Applications/Ghostty.app")
        self.assertIsNone(cs.APP_IN_PATH.match("/usr/bin/login"))


class ProcessEnv(unittest.TestCase):
    def test_it_reads_only_what_it_needs_and_tolerates_nothing(self):
        result = mock.Mock(stdout="claude --x TERM_PROGRAM=ghostty SECRET=abc HERDR_PANE_ID=w:p1 NOT_HERDR_PANE_ID=q")
        with mock.patch("subprocess.run", return_value=result):
            self.assertEqual(cs.process_env(1), {"HERDR_PANE_ID": "w:p1", "TERM_PROGRAM": "ghostty"})
        with mock.patch("subprocess.run", side_effect=OSError):
            self.assertEqual(cs.process_env(1), {})


if __name__ == "__main__":
    unittest.main()
