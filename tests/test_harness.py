"""What each kind of agent can tell about its subagents and open shells. No real process or session is read."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

DAEMON = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DAEMON))
from children import Processes  # noqa: E402
from harness import Claude, Harness, InATerminal, Running, known, runs_a_command  # noqa: E402
from petstate import Pet  # noqa: E402
from sources import HerdrSource  # noqa: E402

SESSION = "3ee55722-c96a-4c3d-bf01-20261ae2e015"
OWN_SHELL = "/bin/zsh -c source /Users/me/.claude/shell-snapshots/snapshot-zsh-1.sh && npm run dev"


class Table:
    """A process table that changes when told to."""

    def __init__(self, rows=()):
        self.rows, self.now = list(rows), 0.0

    def processes(self):
        return Processes(scan=lambda: self.rows, clock=lambda: self.now, every=3.0)

    def later(self, rows=None):
        self.now += 3.0
        if rows is not None:
            self.rows = list(rows)


def stayed(rows) -> Processes:
    table = Table(rows)
    processes = table.processes()
    processes.children(1)
    table.later()
    return processes


class Children(unittest.TestCase):
    def test_only_what_has_stayed_counts(self):
        table = Table([(10, 1, "claude"), (11, 10, OWN_SHELL)])
        processes = table.processes()
        self.assertEqual(processes.children(10), [])
        table.later()
        self.assertEqual(processes.children(10), [(11, OWN_SHELL)])

    def test_a_command_that_returned_is_gone(self):
        table = Table([(10, 1, "claude"), (11, 10, OWN_SHELL)])
        processes = table.processes()
        processes.children(10)
        table.later([(10, 1, "claude")])
        self.assertEqual(processes.children(10), [])

    def test_it_looks_only_every_few_seconds(self):
        looks = []
        processes = Processes(scan=lambda: looks.append(1) or [], clock=lambda: 0.0)
        processes.children(1)
        processes.children(2)
        self.assertEqual(len(looks), 1)

    def test_only_its_own_children(self):
        processes = stayed([(10, 1, "claude"), (11, 10, "zsh -c a"), (12, 11, "zsh -c b"), (13, 1, "zsh -c c")])
        self.assertEqual([pid for pid, _ in processes.children(10)], [11])


class Shells(unittest.TestCase):
    def test_a_shell_given_a_command(self):
        self.assertTrue(runs_a_command("/bin/bash -lc 'pnpm dev'"))
        self.assertTrue(runs_a_command("-zsh -c ls"))

    def test_not_a_shell_someone_types_into_nor_another_program(self):
        self.assertFalse(runs_a_command("-zsh"))
        self.assertFalse(runs_a_command("npm exec @playwright/mcp@latest"))
        self.assertFalse(runs_a_command("node server.js -c config"))
        self.assertFalse(runs_a_command(""))

    def test_claude_counts_only_the_commands_it_ran_itself(self):
        processes = stayed([(10, 1, "claude"), (11, 10, OWN_SHELL), (12, 10, "npm exec @playwright/mcp@latest"),
                            (13, 10, "/bin/sh -c ./my-mcp-server.sh")])
        self.assertEqual(Claude().counts(Running(pid=10), processes), {"sh": 1, "guess": ["sh"]})

    def test_another_agent_counts_any_command_shell(self):
        processes = stayed([(10, 1, "codex"), (11, 10, "bash -lc 'cargo watch'"), (12, 10, "codex-helper")])
        self.assertEqual(InATerminal().counts(Running(pid=10), processes), {"sh": 1, "guess": ["sh"]})

    def test_without_its_process_there_is_no_count(self):
        self.assertEqual(InATerminal().counts(Running(), stayed([])), {})


class Subagents(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.folder = self.root / "-work-shop" / SESSION / "subagents"
        self.folder.mkdir(parents=True)
        self.claude = Claude(root=self.root, clock=lambda: 0.0)

    def record(self, name, at=1000.0, **info):
        path = self.folder / f"agent-{name}.meta.json"
        path.write_text(json.dumps({"agentType": "general-purpose", **info}))
        os.utime(path, (at, at))

    def count(self, started=900.0):
        return self.claude.count_subagents(Running(SESSION, 10, started))

    def test_those_not_finished_are_running(self):
        self.record("a")
        self.record("b")
        self.record("c", finished=True)
        self.assertEqual(self.count(), 2)

    def test_one_that_finishes_stops_counting(self):
        self.record("a")
        self.assertEqual(self.count(), 1)
        self.record("a", finished=True)
        self.assertEqual(self.count(), 0)

    def test_one_left_unfinished_by_an_earlier_process_is_not_running(self):
        self.record("a", at=500.0)
        self.assertEqual(self.count(started=900.0), 0)

    def test_a_session_that_started_none_has_none(self):
        self.assertEqual(Claude(root=Path(tempfile.mkdtemp())).count_subagents(Running(SESSION, 10, 0.0)), 0)

    def test_a_record_that_cannot_be_read_counts_as_nothing(self):
        (self.folder / "agent-x.meta.json").write_text("{not json")
        (self.folder / "agent-y.meta.json").write_text("[]")
        self.assertEqual(self.count(), 0)

    def test_no_session_no_count(self):
        self.assertIsNone(self.claude.count_subagents(Running("../../etc", 10, 0.0)))
        self.assertEqual(self.claude.counts(Running(), stayed([])), {})

    def test_it_is_claudes_own_record_so_not_a_guess(self):
        self.record("a")
        self.assertEqual(self.claude.counts(Running(SESSION, None, 900.0), stayed([])), {"sub": 1})


class Capabilities(unittest.TestCase):
    def test_an_agent_that_cannot_tell_reports_nothing(self):
        self.assertEqual(Harness().counts(Running(SESSION, 10, 0.0), stayed([(10, 1, "x"), (11, 10, "sh -c y")])), {})

    def test_the_agents_supported(self):
        self.assertEqual(set(known()), {"claude", "codex", "opencode", "pi"})
        self.assertIsNone(known()["pi"].subagents)


class Source(HerdrSource):
    def __init__(self):
        super().__init__(pet=Pet(None))
        self.listed = []

    def _herdr(self, *args):
        return {"result": {"agents": self.listed}}


class InTheStatus(unittest.TestCase):
    def test_counts_go_with_the_agent_for_the_app(self):
        root = Path(tempfile.mkdtemp())
        folder = root / "-work-shop" / SESSION / "subagents"
        folder.mkdir(parents=True)
        (folder / "agent-a.meta.json").write_text("{}")
        source = Source()
        source.harnesses = {"claude": Claude(root=root)}
        source.processes = stayed([(10, 1, "claude"), (11, 10, OWN_SHELL)])
        source.procs = {SESSION: (10, 0.0)}
        source.listed = [{"pane_id": "p1", "agent": "claude", "agent_status": "working", "cwd": "/work/shop",
                          "agent_session": {"value": SESSION}},
                         {"pane_id": "p2", "agent": "gemini", "agent_status": "working", "cwd": "/work/shop"}]
        rows = source.poll()
        self.assertNotIn("sub", rows[0])  # the board's rows stay as they were
        desk = source.desk()
        self.assertEqual(desk["p1"], {"sub": 1, "sh": 1, "guess": ["sh"], "ses": SESSION})
        self.assertNotIn("p2", desk)  # no session it names, and nothing counted: nothing to add


if __name__ == "__main__":
    unittest.main()
