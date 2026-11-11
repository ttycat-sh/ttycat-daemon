"""Codex's own record of its subagents and the app's commands. The database and logs here are made up."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

DAEMON = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DAEMON))
import codex_spawns  # noqa: E402
from harness import Codex, Running  # noqa: E402
from test_harness import stayed  # noqa: E402

NOW = 1_000_000.0
MAIN = "019f4ab9-7c3a-7d90-a753-bc9a46ff61a5"
STARTED = '{"type":"event_msg","payload":{"type":"task_started"}}\n'
COMPLETE = '{"type":"event_msg","payload":{"type":"task_complete"}}\n'


class Home:
    def __init__(self):
        self.path = Path(tempfile.mkdtemp())
        (self.path / "sessions").mkdir()
        self.db = sqlite3.connect(self.path / "state_5.sqlite")
        self.db.execute("create table threads (id text primary key, rollout_path text, updated_at integer)")
        self.db.execute("create table thread_spawn_edges (parent_thread_id text, child_thread_id text primary key, status text)")
        self.n = 0

    def spawn(self, log=STARTED, status="open", updated=NOW - 5, parent=MAIN):
        self.n += 1
        child = f"019f4ab9-7c3a-7d90-a753-{self.n:012d}"
        path = self.path / "sessions" / f"rollout-{child}.jsonl"
        path.write_text(log)
        self.db.execute("insert into threads values (?, ?, ?)", (child, str(path), int(updated)))
        self.db.execute("insert into thread_spawn_edges values (?, ?, ?)", (parent, child, status))
        self.db.commit()

    def record(self):
        return codex_spawns.Record(home=self.path, clock=lambda: 0.0, now=lambda: NOW)


class Subagents(unittest.TestCase):
    def test_those_mid_turn_are_running(self):
        home = Home()
        home.spawn()
        home.spawn(STARTED + COMPLETE + STARTED)
        home.spawn(STARTED + COMPLETE)
        self.assertEqual(home.record().look().subagents, {MAIN: 2})

    def test_one_that_was_stopped_or_closed_is_not(self):
        home = Home()
        home.spawn(STARTED + '{"type":"event_msg","payload":{"type":"turn_aborted"}}\n')
        home.spawn(status="closed")
        self.assertEqual(home.record().look().subagents, {})

    def test_one_silent_for_long_may_have_died_and_is_not_counted(self):
        home = Home()
        home.spawn(updated=NOW - 3600)
        self.assertEqual(home.record().look().subagents, {})

    def test_a_log_outside_codexs_sessions_is_never_read(self):
        home = Home()
        home.db.execute("insert into threads values ('x', '/etc/passwd', ?)", (int(NOW),))
        home.db.execute("insert into thread_spawn_edges values (?, 'x', 'open')", (MAIN,))
        home.db.commit()
        self.assertEqual(codex_spawns.open_spawns(home.path, 0), [])

    def test_a_codex_that_keeps_this_differently_gives_no_answer(self):
        home = Home()
        home.db.execute("drop table thread_spawn_edges")
        home.db.commit()
        self.assertIsNone(home.record().look())
        self.assertIsNone(codex_spawns.Record(home=Path(tempfile.mkdtemp())).look())


class Commands(unittest.TestCase):
    def test_only_those_still_running_count(self):
        home = Home()
        folder = home.path / "process_manager"
        folder.mkdir()
        (folder / "chat_processes.json").write_text(json.dumps([
            {"conversationId": MAIN, "osPid": os.getpid()}, {"conversationId": MAIN, "osPid": None},
            {"conversationId": MAIN, "osPid": 10}, {"conversationId": "other", "osPid": 11}, "odd"]))
        self.assertEqual(codex_spawns.app_commands(home.path, is_alive=lambda pid: pid in (os.getpid(), 11)), {MAIN: 1, "other": 1})

    def test_no_list_no_commands(self):
        self.assertEqual(codex_spawns.app_commands(Path(tempfile.mkdtemp())), {})
        self.assertFalse(codex_spawns.alive(None))
        self.assertFalse(codex_spawns.alive(True))


class Counts(unittest.TestCase):
    def setUp(self):
        self.home = Home()
        self.home.spawn()
        self.kind = Codex(record=self.home.record())

    def test_in_a_terminal_subagents_are_its_record_and_shells_an_estimate(self):
        processes = stayed([(10, 1, "codex"), (11, 10, "bash -lc 'cargo watch'")])
        self.assertEqual(self.kind.counts(Running(MAIN, 10), processes), {"sub": 1, "sh": 1, "guess": ["sh"]})

    def test_in_the_app_the_commands_are_the_apps_own_list(self):
        self.kind.record.commands = lambda home: {MAIN: 2}
        self.assertEqual(self.kind.counts(Running(MAIN, in_app=True), stayed([])), {"sub": 1, "sh": 2})

    def test_a_thread_that_cannot_be_told_gets_only_what_its_process_shows(self):
        self.assertEqual(self.kind.counts(Running(pid=10), stayed([])), {"sh": 0, "guess": ["sh"]})
        self.assertEqual(self.kind.counts(Running(), stayed([])), {})

    def test_in_a_terminal_without_its_process_no_shells_are_claimed(self):
        self.assertEqual(self.kind.counts(Running(MAIN), stayed([])), {"sub": 1})


if __name__ == "__main__":
    unittest.main()
