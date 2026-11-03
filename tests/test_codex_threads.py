"""Codex's records. All of it in temp folders: nothing real is read."""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

DAEMON = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DAEMON))
import claude_sessions as cs  # noqa: E402
import codex_threads as ct  # noqa: E402
from petstate import Pet  # noqa: E402
from sources import Fleet, HerdrSource  # noqa: E402

TID = "01a19cf4-41da-7391-9ef3-a8949d6edc31"
PANE = "c:" + TID[-8:]


def line(kind, **payload):
    return json.dumps({"timestamp": "2026-11-02T15:50:15.822Z", "type": kind, "payload": payload}) + "\n"


def event(name, **more):
    return line("event_msg", type=name, **more)


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        (self.home / "sessions").mkdir()
        self.log = self.home / "sessions" / "rollout.jsonl"
        self.log.write_text("")

    def write(self, *lines):
        with open(self.log, "a") as f:
            f.write("".join(lines))

    def age(self, seconds):
        when = time.time() - seconds
        os.utime(self.log, (when, when))


class Database(Base):
    def make(self, rows=(), name="state_5.sqlite"):
        db = sqlite3.connect(self.home / name)
        db.execute("create table threads (id text, rollout_path text, cwd text, title text, source text, approval_mode text, updated_at int, archived int)")
        db.executemany("insert into threads values (?,?,?,?,?,?,?,?)", rows)
        db.commit()
        db.close()

    def row(self, id=TID, source="vscode", updated=None, archived=0, path=None):
        return (id, str(path or self.log), "/w/shop", "A title", source, "on-request", int(updated or time.time()), archived)

    def test_the_apps_recent_threads(self):
        self.make([self.row()])
        self.assertEqual([t["id"] for t in ct.read_threads(self.home, ("vscode",), time.time() - 60)], [TID])

    def test_not_scripted_runs_or_subagents(self):
        self.make([self.row(source="exec"), self.row(source='{"subagent":"x"}')])
        self.assertEqual(ct.read_threads(self.home, ("vscode", "cli")), [])

    def test_not_archived_or_old(self):
        self.make([self.row(archived=1), self.row(updated=time.time() - 9999)])
        self.assertEqual(ct.read_threads(self.home, ("vscode",), time.time() - 60), [])

    def test_the_newest_database(self):
        self.make([self.row()], "state_12.sqlite")
        self.make([], "state_5.sqlite")
        self.assertEqual(ct.newest_db(self.home).name, "state_12.sqlite")

    def test_a_shape_it_doesnt_know_is_no_rows(self):
        db = sqlite3.connect(self.home / "state_5.sqlite")
        db.execute("create table threads (something text)")
        db.commit()
        db.close()
        self.assertEqual(ct.read_threads(self.home), [])

    def test_no_codex_at_all_is_no_rows(self):
        self.assertEqual(ct.read_threads(self.home / "nothing"), [])

    def test_a_log_outside_codexs_folder_is_ignored(self):
        self.make([self.row(path="/etc/passwd")])
        self.assertEqual(ct.read_threads(self.home), [])

    def test_a_strange_id_is_ignored(self):
        self.make([self.row(id="../../etc")])
        self.assertEqual(ct.read_threads(self.home), [])

    def test_it_only_ever_reads(self):
        self.make([self.row()])
        before = (self.home / "state_5.sqlite").read_bytes()
        ct.read_threads(self.home)
        self.assertEqual((self.home / "state_5.sqlite").read_bytes(), before)

    def test_ids_made_in_the_same_minute_stay_apart(self):
        a = ct.thread_pane("01a19cf4-41da-7391-9ef3-a8949d6edc31")
        b = ct.thread_pane("01a19cf4-99aa-7391-9ef3-000000000000")
        self.assertNotEqual(a, b)
        self.assertLess(len(a), 16)


class Reading(Base):
    def logs(self):
        logs = ct.Logs()
        logs.sync({PANE: str(self.log)})
        return logs

    def test_a_turn_starts_and_ends(self):
        self.write(event("task_started"))
        logs = self.logs()
        self.assertTrue(logs.running(PANE))
        self.write(event("task_complete"))
        logs.sync({PANE: str(self.log)})
        self.assertFalse(logs.running(PANE))

    def test_stopping_a_turn_ends_it_and_says_so(self):
        self.write(event("task_started"), event("turn_aborted"))
        logs = self.logs()
        self.assertFalse(logs.running(PANE))
        self.assertEqual(logs.events(PANE)[-1].text, "Stopped")

    def test_what_it_did(self):
        self.write(event("user_message", message="fix the cart"),
                   line("response_item", type="function_call", name="exec_command", arguments=json.dumps({"cmd": "npm test"})),
                   line("response_item", type="custom_tool_call", name="apply_patch", input="*** Update File: src/cart.ts\n@@"),
                   line("response_item", type="message", role="assistant", content=[{"type": "output_text", "text": "All fixed."}]))
        texts = [e.text for e in self.logs().events(PANE)]
        self.assertEqual(texts[1:], ["$ npm test", "Edit cart.ts", "All fixed."])
        self.assertIn("fix the cart", texts[0])

    def test_what_it_said_last_this_turn(self):
        self.write(line("response_item", type="message", role="assistant", content=[{"text": "Old."}]), event("user_message", message="next"))
        self.assertEqual(self.logs().last_said(PANE), "")

    def test_a_command_is_named_when_its_known(self):
        self.write(line("response_item", type="custom_tool_call", name="exec", input="code"))
        logs = self.logs()
        self.assertEqual(logs.now(PANE), "Running a command")
        self.write(event("item_completed", item={"type": "CommandExecution", "command": "touch a", "exit_code": 0}))
        logs.sync({PANE: str(self.log)})
        self.assertEqual([e.text for e in logs.events(PANE)], ["$ touch a"])

    def test_a_failed_command_says_so(self):
        self.write(event("item_completed", item={"type": "CommandExecution", "command": "make", "exit_code": 2}))
        last = self.logs().events(PANE)[-1]
        self.assertEqual((last.text, last.tone), ("Failed: $ make", 1))

    def test_a_line_still_being_written_waits(self):
        self.write(event("task_started"))
        complete = event("task_complete")
        self.write(complete[:20])
        logs = self.logs()
        self.assertTrue(logs.running(PANE))
        self.write(complete[20:])
        logs.sync({PANE: str(self.log)})
        self.assertFalse(logs.running(PANE))

    def test_only_the_end_of_a_huge_log_is_read(self):
        with mock.patch.object(ct, "TAIL_BYTES", 2000):
            self.write("x" * 500_000 + "\n", event("task_started"))
            logs = self.logs()
            self.assertTrue(logs.running(PANE))
            self.assertLess(self.log.stat().st_size - 2000, logs.trails[PANE].offset)

    def test_a_turn_that_began_before_the_part_read(self):
        with mock.patch.object(ct, "TAIL_BYTES", 300):
            self.write(event("task_started"), "y" * 400 + "\n", event("user_message", message="more"))
            self.assertTrue(self.logs().running(PANE))

    def test_junk_lines_are_skipped(self):
        self.write("not json\n", "[1,2]\n", '{"type":"event_msg","payload":"x"}\n', event("task_started"))
        self.assertTrue(self.logs().running(PANE))

    def test_a_log_that_goes_away_is_no_crash(self):
        logs = self.logs()
        self.log.unlink()
        logs.sync({PANE: str(self.log)})
        self.assertFalse(logs.running(PANE))

    def test_a_log_that_starts_over(self):
        self.write(event("task_started"), event("user_message", message="old"))
        logs = self.logs()
        self.log.write_text(event("task_complete"))
        logs.sync({PANE: str(self.log)})
        self.assertFalse(logs.running(PANE))
        self.assertEqual(logs.events(PANE), [])


class States(Base):
    def status(self, can_ask=True, silent=0, ran=None):
        logs = ct.Logs()
        logs.sync({PANE: str(self.log)})
        self.age(silent)
        logs.sync({PANE: str(self.log)})
        self.logs_used = logs
        return logs.status(PANE, can_ask, ran if ran is not None else set())

    def test_working(self):
        self.write(event("task_started"))
        self.assertEqual(self.status(), "working")

    def test_done_only_after_being_seen_at_work(self):
        self.write(event("task_started"), event("task_complete"))
        self.assertEqual(self.status(), "idle")
        self.assertEqual(self.status(ran={PANE}), "done")

    def test_silent_for_too_long_mid_turn_isnt_called_working(self):
        self.write(event("task_started"))
        self.assertEqual(self.status(silent=ct.STALE_S + 5), "unknown")

    def test_it_says_it_may_be_waiting_and_no_more(self):
        self.write(event("task_started"), line("response_item", type="custom_tool_call", name="exec", input="x"))
        self.assertEqual(self.status(silent=45), "working")
        self.assertIn("may be waiting", self.logs_used.now(PANE))

    def test_not_if_it_cant_ask(self):
        self.write(event("task_started"), line("response_item", type="custom_tool_call", name="exec", input="x"))
        self.status(can_ask=False, silent=45)
        self.assertNotIn("may be", self.logs_used.now(PANE))

    def test_not_while_thinking_with_no_tool_open(self):
        self.write(event("task_started"), line("response_item", type="custom_tool_call", name="exec", input="x"),
                   line("response_item", type="custom_tool_call_output", output="ok"))
        self.status(silent=45)
        self.assertNotIn("may be", self.logs_used.now(PANE))

    def test_not_too_soon(self):
        self.write(event("task_started"), line("response_item", type="custom_tool_call", name="exec", input="x"))
        self.status(silent=5)
        self.assertNotIn("may be", self.logs_used.now(PANE))


class Rows(Base):
    def source(self, threads=None, **kw):
        self.threads_list = threads if threads is not None else [self.thread()]
        self.calls = []

        def run(command, **k):
            self.calls.append(command)
            return mock.Mock(returncode=0)
        return ct.CodexThreads(pet=Pet(None), home=self.home, read=lambda home, sources, since: self.threads_list, run=run, **kw)

    def thread(self, mode="on-request"):
        return {"id": TID, "rollout_path": str(self.log), "cwd": "/w/shop", "title": "Fix the cart", "source": "vscode", "approval_mode": mode, "updated_at": int(time.time())}

    def poll(self, source):
        source.last_poll = source.scanned_at = 0
        return source.poll()

    def test_a_row_like_any_other(self):
        self.write(event("task_started"), line("response_item", type="function_call", name="exec_command", arguments='{"cmd":"npm test"}'))
        row = self.poll(self.source())[0]
        self.assertEqual((row["id"], row["a"], row["n"], row["p"], row["s"], row["d"]), (PANE, "CX", "Codex", "shop", "working", "$ npm test"))

    def test_finishing_says_what_it_said_and_feeds_the_pet(self):
        self.write(event("task_started"))
        source = self.source()
        self.poll(source)
        before = source.pet_stats.total
        self.write(line("response_item", type="message", role="assistant", content=[{"text": "All fixed."}]), event("task_complete"))
        row = self.poll(source)[0]
        self.assertEqual((row["s"], row["d"]), ("done", "All fixed."))
        self.assertEqual(source.pet_stats.total, before + 1)

    def test_recent_activity_shows_on_its_page(self):
        self.write(event("task_started"), line("response_item", type="function_call", name="exec_command", arguments='{"cmd":"ls"}'))
        source = self.source()
        self.poll(source)
        self.assertEqual(source.history(PANE)[0]["text"], "$ ls")

    def test_nothing_can_be_answered(self):
        self.write(event("task_started"))
        source = self.source()
        self.poll(source)
        self.assertFalse(source.act("allow", PANE, "x")[0])

    def test_a_stopped_turn_says_so(self):
        self.write(event("task_started"), line("response_item", type="message", role="assistant", content=[{"text": "I'll start."}]))
        source = self.source()
        self.poll(source)
        self.write(event("turn_aborted"))
        self.assertEqual(self.poll(source)[0]["d"], "Stopped")

    def test_a_thread_whose_log_has_gone_isnt_shown(self):
        self.log.unlink()
        self.assertEqual(self.poll(self.source()), [])

    def test_a_finished_thread_goes_when_its_no_longer_recent(self):
        self.write(event("task_started"), event("task_complete"))
        source = self.source()
        self.assertEqual(len(self.poll(source)), 1)
        self.threads_list = []
        self.assertEqual(self.poll(source), [])

    def test_a_long_turn_stays_though_the_database_has_moved_on(self):
        self.write(event("task_started"))
        source = self.source()
        self.poll(source)
        self.threads_list = []
        self.assertEqual(len(self.poll(source)), 1)

    def test_open_goes_to_that_thread(self):
        source = self.source()
        self.poll(source)
        self.assertEqual(source.act("open", PANE, ""), (True, ""))
        self.assertEqual(self.calls, [["open", f"codex://threads/{TID}"]])

    def test_a_thread_thats_gone_cant_be_opened(self):
        source = self.source([])
        self.poll(source)
        self.assertFalse(source.act("open", PANE, "")[0])

    def test_the_fleet_finds_it(self):
        self.write(event("task_started"))
        threads = self.source()
        local = mock.Mock(spec=HerdrSource, reachable=True, agents={})
        local.poll.return_value = []
        fleet = Fleet(local, [], start=False, threads=threads)
        threads.last_poll = threads.scanned_at = 0
        self.assertEqual([r["id"] for r in fleet.poll()], [PANE])
        self.assertIs(fleet._route(PANE)[0], threads)

    def test_the_real_reader_has_the_shape_the_source_wants(self):
        db = sqlite3.connect(self.home / "state_5.sqlite")
        db.execute("create table threads (id text, rollout_path text, cwd text, title text, source text, approval_mode text, updated_at int, archived int)")
        db.execute("insert into threads values (?,?,?,?,?,?,?,?)", (TID, str(self.log), "/w/shop", "T", "vscode", "never", int(time.time()), 0))
        db.commit()
        db.close()
        self.write(event("task_started"))
        source = ct.CodexThreads(pet=Pet(None), home=self.home)
        source.last_poll = 0
        self.assertEqual(source.poll()[0]["s"], "working")


class InAPlainTerminal(Base):
    def source(self, rows, threads, begun=None):
        self.now = 100.0
        return cs.RunningAgents(pet=Pet(None), scan=lambda: rows, env_of=lambda p: {"TERM_PROGRAM": "ghostty"}, cwd_of=lambda p: "/w/shop",
                                clock=lambda: self.now, panes=lambda: "", parent_check=lambda p: False, codex_home=self.home,
                                codex_threads=lambda home, sources, since: threads, begun=lambda pid: begun if begun is not None else time.time() - 60)

    def thread(self, cwd="/w/shop", updated=None):
        return {"id": TID, "rollout_path": str(self.log), "cwd": cwd, "approval_mode": "on-request", "updated_at": int(updated or time.time())}

    def poll(self, source):
        source.last_poll = 0
        return source.poll()

    def test_its_state_comes_from_its_thread(self):
        self.write(event("task_started"), line("response_item", type="function_call", name="exec_command", arguments='{"cmd":"npm test"}'))
        row = self.poll(self.source([(7, 7, 7, "t", "/x/codex")], [self.thread()]))[0]
        self.assertEqual((row["id"], row["s"], row["d"]), ("p:7", "working", "$ npm test"))

    def test_a_thread_from_before_it_started_isnt_its_own(self):
        self.write(event("task_started"))
        row = self.poll(self.source([(7, 7, 7, "t", "codex")], [self.thread(updated=time.time() - 500)]))[0]
        self.assertEqual(row["d"], "state unknown")

    def test_another_folders_thread_isnt_its_own(self):
        self.write(event("task_started"))
        row = self.poll(self.source([(7, 7, 7, "t", "codex")], [self.thread(cwd="/other")]))[0]
        self.assertEqual(row["d"], "state unknown")

    def test_two_in_one_folder_cant_be_told_apart(self):
        self.write(event("task_started"))
        rows = self.poll(self.source([(7, 7, 7, "a", "codex"), (8, 8, 8, "b", "codex")], [self.thread()]))
        self.assertEqual([r["d"] for r in rows], ["state unknown", "state unknown"])

    def test_other_agents_are_left_alone(self):
        row = self.poll(self.source([(7, 7, 7, "t", "opencode")], [self.thread()]))[0]
        self.assertEqual(row["d"], "state unknown")

    def test_how_long_a_process_has_run(self):
        for text, seconds in (("05:03", 303), ("1:05:03", 3903), ("2-01:00:00", 176400)):
            with mock.patch("subprocess.run", return_value=mock.Mock(stdout=f"  {text}\n")):
                self.assertAlmostEqual(cs.started_at(1), time.time() - seconds, delta=2)
        with mock.patch("subprocess.run", return_value=mock.Mock(stdout="")):
            self.assertIsNone(cs.started_at(1))


if __name__ == "__main__":
    unittest.main()
