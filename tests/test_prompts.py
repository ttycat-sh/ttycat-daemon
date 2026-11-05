"""Run: python3 -m unittest discover -s daemon/tests"""
import calendar
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
from pathlib import Path

DAEMON = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DAEMON))
import activity  # noqa: E402
import approvals  # noqa: E402
import ttycatd  # noqa: E402
import prompts  # noqa: E402
import sources  # noqa: E402
from petstate import Pet  # noqa: E402
import petstate  # noqa: E402
from sources import Fleet  # noqa: E402

REAL_RUN = subprocess.run


def _no_outside_effects(command, *args, **kwargs):
    """A test must never touch the Mac it runs on: `open` brings an app forward, over whatever you're doing."""
    program = command[0] if isinstance(command, (list, tuple)) and command else command
    if program in ("open", "osascript", "herdr", "ssh", "pgrep"):
        raise AssertionError(f"a test tried to run {program}: {command}")
    return REAL_RUN(command, *args, **kwargs)


def setUpModule():
    subprocess.run = _no_outside_effects


def tearDownModule():
    subprocess.run = REAL_RUN


TRUST = """
─────────────────────────────────────────
 Accessing workspace:

 /Users/someone

 Quick safety check: Is this a project you created or one you trust?

 ❯ No, exit
   Yes, I trust this folder

 Enter to confirm · Esc to cancel
"""


class TerminalText(unittest.TestCase):
    def test_shows_the_prompt_without_its_options(self):
        a = prompts.parse("claude", TRUST)
        self.assertIn("Quick safety check", a.body)
        self.assertNotIn("Esc to cancel", a.body)

    def test_empty(self):
        self.assertEqual(prompts.parse("claude", "").body, "Waiting at its terminal")

    def test_codex_command(self):
        a = prompts.parse("codex", "─" * 20 + "\n$ pnpm add culori\n")
        self.assertEqual((a.tool, a.body), ("SHELL", "pnpm add culori"))


class Describe(unittest.TestCase):
    """The payloads are the shapes captured from real Claude Code 2.1.316."""

    def view(self, tool, args, cwd="/repo"):
        return approvals.describe({"tool_name": tool, "tool_input": args, "cwd": cwd})

    def test_bash_shows_the_command_not_the_description(self):
        v = self.view("Bash", {"command": "touch a.txt", "description": "Harmless little thing"})
        self.assertEqual((v.kind, v.tool, v.body), ("approve", "BASH", "touch a.txt"))

    def test_multiline_command_keeps_its_lines(self):
        self.assertEqual(self.view("Bash", {"command": "a\nrm -rf b"}).body, "a\nrm -rf b")

    def test_edit_is_a_diff(self):
        v = self.view("Edit", {"file_path": "/repo/gateway.tf", "old_string": "timeout = 30",
                               "new_string": "timeout = 90\nretry = 3", "replace_all": False})
        self.assertEqual((v.kind, v.tool, v.file, v.add, v.delete), ("approve", "EDIT", "gateway.tf", 2, 1))
        self.assertEqual(v.body, "- timeout = 30\n+ timeout = 90\n+ retry = 3")
        self.assertEqual(v.verb, "wants to change")

    def test_replace_all_says_so(self):
        v = self.view("Edit", {"file_path": "/repo/a", "old_string": "x", "new_string": "y", "replace_all": True})
        self.assertEqual(v.verb, "wants to change all")

    def test_a_file_outside_the_project_shows_where(self):
        v = self.view("Edit", {"file_path": str(Path.home() / ".ssh/config"), "old_string": "a", "new_string": "b"})
        self.assertEqual(v.file, "~/.ssh/config")

    def test_long_path_keeps_its_end(self):
        v = self.view("Write", {"file_path": "/elsewhere/" + "deep/" * 20 + "name.txt", "content": "x"})
        self.assertTrue(v.file.endswith("name.txt"))
        self.assertLessEqual(len(v.file), 31)

    def test_read_fetch_and_search(self):
        self.assertEqual(self.view("Read", {"file_path": "/etc/hosts"}).body, "/etc/hosts")
        self.assertEqual(self.view("WebFetch", {"url": "https://example.com", "prompt": "x"}).body, "https://example.com")
        self.assertEqual(self.view("WebSearch", {"query": "cats"}).tool, "SEARCH")

    def test_mcp_tool_shows_name_and_arguments(self):
        v = self.view("mcp__linear__create_issue", {"title": "Bug", "priority": 2})
        self.assertEqual((v.kind, v.tool), ("approve", "MCP"))
        self.assertEqual(v.body, "linear.create_issue\ntitle: Bug\npriority: 2")

    def test_unknown_tool_shows_name_and_arguments(self):
        v = self.view("NotebookEdit", {"notebook_path": "a.ipynb"})
        self.assertEqual((v.kind, v.tool), ("approve", "TOOL"))
        self.assertIn("NotebookEdit", v.body)
        self.assertIn("a.ipynb", v.body)

    def test_plans_are_terminal_only(self):
        self.assertEqual(self.view("ExitPlanMode", {"plan": "Do it"}).kind, "terminal")

    QUESTION = {"questions": [{"question": "Red or blue?", "header": "Colour", "multiSelect": False, "options": [
        {"label": "Red", "description": "Warm"}, {"label": "Blue", "description": "Cool"}]}]}

    def test_a_question_is_a_choice(self):
        v = self.view("AskUserQuestion", self.QUESTION)
        self.assertEqual((v.kind, v.tool, v.detail), ("choice", "ASK", "Red or blue?"))
        self.assertEqual(v.questions[0]["o"][1], {"l": "Blue", "d": "Cool"})
        self.assertEqual(v.body, "Red or blue?\n- Red: Warm\n- Blue: Cool")

    def test_a_question_it_cannot_show_is_terminal_only(self):
        five = [{"label": str(i)} for i in range(5)]
        same = {"question": "Q", "options": [{"label": "A"}]}
        for args in ({"questions": []}, {}, {"questions": "x"},
                     {"questions": [{"question": "Q", "options": []}]},
                     {"questions": [{"question": "Q", "options": five}]},
                     {"questions": [{"question": "", "options": [{"label": "A"}]}]},
                     {"questions": [{"question": "Q", "options": [{"label": ""}]}]},
                     {"questions": [same, same]},
                     {"questions": [dict(same, question=str(i)) for i in range(5)]}):
            self.assertEqual(self.view("AskUserQuestion", args).kind, "terminal", args)

    def test_a_long_question_is_terminal_only(self):
        args = {"questions": [{"question": "x" * 1100, "options": [{"label": "A"}]}]}
        self.assertEqual(self.view("AskUserQuestion", args).kind, "terminal")

    def test_too_long_to_read_is_terminal_only(self):
        for v in (self.view("Bash", {"command": "x" * (approvals.MAX_BODY + 1)}),
                  self.view("Write", {"file_path": "/repo/a", "content": "line\n" * 500})):
            self.assertEqual(v.kind, "terminal")
            self.assertEqual(v.note, approvals.TOO_LONG_NOTE)
            self.assertLessEqual(len(v.body), approvals.MAX_BODY)

    def test_longest_offered_body_fits_the_board(self):
        v = self.view("Bash", {"command": "x" * approvals.MAX_BODY})
        self.assertEqual(v.kind, "approve")
        self.assertEqual(len(approvals.fit(v.body, 1000)), approvals.MAX_BODY)

    def test_empty_request_is_never_offered(self):
        self.assertEqual(self.view("Bash", {"command": ""}).kind, "terminal")
        self.assertEqual(self.view("Bash", {}).kind, "terminal")


class HubCase(unittest.TestCase):
    """The real hook script, over a real socket, against the hub."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp")  # socket paths have a length limit
        self.sock = Path(self.tmp.name) / "h.sock"
        self.clock = 100.0
        self.hub = approvals.Hub(self.sock, clock=lambda: self.clock)
        self.running = True
        self.hooks = []
        self.pump = threading.Thread(target=self._pump, daemon=True)
        self.pump.start()

    def tearDown(self):
        self.running = False
        self.pump.join()
        for h in self.hooks:
            h.kill()
            h.wait()
            h.stdout.close()
        self.hub.close()
        self.tmp.cleanup()

    def _pump(self):
        import select
        while self.running:
            try:
                r, _, _ = select.select(self.hub.fds(), [], [], 0.02)
                for s in r:
                    if self.hub.owns(s):
                        self.hub.handle(s)
            except (OSError, ValueError):
                pass  # a test closed a request between the select and the read

    def ask(self, pane="w:p1", tool="Bash", args=None, env=None):
        e = {**os.environ, "TTYCAT_HOOK_SOCK": str(self.sock), "HERDR_PANE_ID": pane, **(env or {})}
        if env and env.get("HERDR_PANE_ID") == "":
            del e["HERDR_PANE_ID"]
        h = subprocess.Popen([sys.executable, "-I", str(DAEMON / "hook.py")], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, env=e, text=True)
        h.stdin.write(json.dumps({"tool_name": tool, "tool_input": args or {"command": "touch a"}, "cwd": "/repo"}))
        h.stdin.close()
        self.hooks.append(h)
        return h

    def wait_pending(self, pane="w:p1", differs_from=None):
        for _ in range(300):
            p = self.hub.get(pane)
            if p and p.id != differs_from:
                return p
            time.sleep(0.01)
        self.fail("the request never arrived")

    def finish(self, h):
        h.wait(timeout=5)
        out = h.stdout.read()
        self.assertEqual(h.returncode, 0)
        return json.loads(out)["hookSpecificOutput"]["decision"] if out else None

    def test_allow_is_plain(self):
        h = self.ask()
        p = self.wait_pending()
        self.assertEqual(self.hub.answer("w:p1", p.id, "allow"), (True, ""))
        self.assertEqual(self.finish(h), {"behavior": "allow"})

    def test_deny_ends_the_turn_and_says_not_to_work_around_it(self):
        h = self.ask()
        self.hub.answer("w:p1", self.wait_pending().id, "deny")
        d = self.finish(h)
        self.assertEqual((d["behavior"], d["interrupt"]), ("deny", True))
        self.assertIn("Do not do it", d["message"])
        self.assertIn("another way", d["message"])

    def test_no_answer_denies_and_lets_the_agent_carry_on(self):
        h = self.ask()
        p = self.wait_pending()
        for action in ("deny-retry", "deny-explain"):
            self.assertEqual(self.hub.answer("w:p1", p.id, action), (False, "unknown action"))
        self.assertIsNone(h.poll())
        self.hub.answer("w:p1", p.id, "deny")
        self.finish(h)

    def test_wrong_rev_decides_nothing(self):
        h = self.ask()
        p = self.wait_pending()
        self.assertEqual(self.hub.answer("w:p1", "someotherrev", "allow"), (False, "prompt already gone"))
        self.assertIsNone(h.poll())
        self.hub.answer("w:p1", p.id, "deny")
        self.finish(h)

    def test_unknown_action_decides_nothing(self):
        h = self.ask()
        p = self.wait_pending()
        self.assertEqual(self.hub.answer("w:p1", p.id, "always"), (False, "unknown action"))
        self.hub.close_pane("w:p1")
        self.assertIsNone(self.finish(h))

    def test_terminal_only_cannot_be_answered(self):
        h = self.ask(tool="ExitPlanMode", args={"plan": "Do it"})
        p = self.wait_pending()
        self.assertEqual(self.hub.answer("w:p1", p.id, "allow"), (False, "answer it in the terminal"))
        self.hub.close_pane("w:p1")
        self.assertIsNone(self.finish(h))

    FRUIT = {"questions": [
        {"question": "Which fruit?", "multiSelect": True, "options": [
            {"label": "Äpfel"}, {"label": "Pear"}, {"label": "Plum"}]},
        {"question": "Size?", "multiSelect": False, "options": [{"label": "Small"}, {"label": "Large"}]}]}

    def answer_fruit(self, picks):
        h = self.ask(tool="AskUserQuestion", args=self.FRUIT)
        p = self.wait_pending()
        return h, p, self.hub.answer("w:p1", p.id, "answer", picks)

    def test_answers_reach_claude_with_the_original_labels(self):
        h, _, result = self.answer_fruit([{"o": [2, 0]}, {"o": [1]}])
        self.assertEqual(result, (True, ""))
        d = self.finish(h)
        self.assertEqual(d["behavior"], "allow")
        # as asked, accents included, in the order offered; the questions go back untouched
        self.assertEqual(d["updatedInput"]["answers"], {"Which fruit?": "Äpfel, Plum", "Size?": "Large"})
        self.assertEqual(d["updatedInput"]["questions"], self.FRUIT["questions"])

    def test_typed_answers_are_used_and_cleaned(self):
        h, _, result = self.answer_fruit([{"o": [1], "t": " figs\x07 too "}, {"t": "Medium"}])
        self.assertEqual(result, (True, ""))
        self.assertEqual(self.finish(h)["updatedInput"]["answers"], {"Which fruit?": "Pear, figs too", "Size?": "Medium"})

    def test_incomplete_or_invalid_answers_decide_nothing(self):
        h = self.ask(tool="AskUserQuestion", args=self.FRUIT)
        p = self.wait_pending()
        for picks in (None, [], [{"o": [0]}],                     # not every question
                      [{"o": [0]}, {}],                           # one left empty
                      [{"o": [0]}, {"t": "   "}],                 # only spaces
                      [{"o": [9]}, {"o": [0]}],                   # no such option
                      [{"o": [-1]}, {"o": [0]}],
                      [{"o": ["Pear"]}, {"o": [0]}],              # a label isn't a choice
                      [{"o": [True]}, {"o": [0]}],
                      [{"o": [0]}, {"o": [0, 1]}],                # two answers to a single choice
                      [{"o": [0]}, {"o": [0], "t": "and this"}],
                      ["x", "y"]):
            self.assertEqual(self.hub.answer("w:p1", p.id, "answer", picks), (False, "answer every question"), picks)
        self.assertIsNone(h.poll())
        self.assertIsNotNone(self.hub.get("w:p1"))

    def test_a_question_cannot_be_allowed_or_denied(self):
        h = self.ask(tool="AskUserQuestion", args=self.FRUIT)
        p = self.wait_pending()
        for action in ("allow", "deny"):
            self.assertEqual(self.hub.answer("w:p1", p.id, action), (False, "answer it in the terminal"))
        self.assertIsNone(h.poll())

    def test_a_command_cannot_be_answered(self):
        h = self.ask()
        p = self.wait_pending()
        self.assertEqual(self.hub.answer("w:p1", p.id, "answer", [{"o": [0]}]), (False, "unknown action"))
        self.assertIsNone(h.poll())

    def test_the_hook_refuses_answers_for_any_other_tool(self):
        sys.path.insert(0, str(DAEMON))
        import hook
        request = {"tool_name": "Bash", "tool_input": {"command": "x"}}
        self.assertIsNone(hook.decision({"behavior": "allow", "answers": {"a": "b"}}, request))
        ok = {"tool_name": "AskUserQuestion", "tool_input": {"questions": []}}
        self.assertIsNone(hook.decision({"behavior": "allow", "answers": "nope"}, ok))

    def test_new_request_replaces_the_old_without_deciding_it(self):
        first = self.ask(args={"command": "one"})
        p1 = self.wait_pending()
        second = self.ask(args={"command": "two"})
        p2 = self.wait_pending(differs_from=p1.id)
        self.assertIsNone(self.finish(first))
        self.assertEqual(self.hub.answer("w:p1", p1.id, "allow"), (False, "prompt already gone"))
        self.assertEqual(p2.view.body, "two")
        self.hub.answer("w:p1", p2.id, "allow")
        self.assertEqual(self.finish(second), {"behavior": "allow"})

    def test_a_killed_hook_is_forgotten(self):
        h = self.ask()
        p = self.wait_pending()
        h.kill()
        for _ in range(300):
            if not self.hub.get("w:p1"):
                break
            time.sleep(0.01)
        self.assertIsNone(self.hub.get("w:p1"))
        self.assertEqual(self.hub.answer("w:p1", p.id, "allow"), (False, "prompt already gone"))

    def test_prune_waits_out_herdrs_lag_then_lets_go(self):
        h = self.ask()
        self.wait_pending()
        self.hub.prune({"w:p1": "working"})
        self.assertIsNotNone(self.hub.get("w:p1"))  # herdr may just not have noticed yet
        self.clock += approvals.GRACE_S
        self.hub.prune({"w:p1": "blocked"})
        self.assertIsNotNone(self.hub.get("w:p1"))
        self.hub.prune({"w:p1": "working"})  # answered in the terminal
        self.assertIsNone(self.hub.get("w:p1"))
        self.assertIsNone(self.finish(h))

    def test_daemon_stopping_leaves_no_decision(self):
        h = self.ask()
        self.wait_pending()
        self.hub.close_pane("w:p1")
        self.assertIsNone(self.finish(h))

    def test_outside_herdr_does_nothing(self):
        self.assertIsNone(self.finish(self.ask(env={"HERDR_PANE_ID": ""})))
        self.assertEqual(self.hub.pending, {})

    def test_no_daemon_leaves_no_decision(self):
        self.assertIsNone(self.finish(self.ask(env={"TTYCAT_HOOK_SOCK": str(self.sock) + ".none"})))

    def test_garbage_on_stdin_leaves_no_decision(self):
        e = {**os.environ, "TTYCAT_HOOK_SOCK": str(self.sock), "HERDR_PANE_ID": "w:p1"}
        out = subprocess.run([sys.executable, "-I", str(DAEMON / "hook.py")], input="not json", env=e,
                             capture_output=True, text=True, timeout=5)
        self.assertEqual((out.returncode, out.stdout), (0, ""))


class FakeHerdr(sources.HerdrSource):
    def __init__(self, status="blocked", screen="", **kw):
        kw.setdefault("pet", Pet(Path(tempfile.gettempdir()) / "ttycat-test-absent" / "pet.json"))
        super().__init__(**kw)
        self.status, self.screen, self.calls = status, screen, []

    def _herdr(self, *args):
        self.calls.append(args)
        if args[:2] == ("agent", "list"):
            return {"result": {"agents": [{"pane_id": "w:p1", "agent": "claude", "agent_status": self.status,
                                           "cwd": "/repo"}]}}
        if args[:2] == ("agent", "focus"):
            return {"result": {}}
        return None

    def _read_prompt(self, pane, agent):
        return self.screen, prompts.parse(agent, self.screen)

    def row(self):
        self.last_poll = 0
        return self.poll()[0]


class FakeHub:
    """Enough of a hub to steer the source without sockets."""

    def __init__(self, view=None):
        self.pending = {"w:p1": approvals.Pending("rev123", "w:p1", view, None, 0)} if view else {}
        self.answers = []

    def get(self, pane):
        return self.pending.get(pane)

    def prune(self, statuses):
        pass

    def keep_only(self, alive):
        for pane in set(self.pending) - alive:
            self.close_pane(pane)

    def close_pane(self, pane):
        self.pending.pop(pane, None)

    def answer(self, pane, rev, action, picks=None):
        self.answers.append((pane, rev, action) if picks is None else (pane, rev, action, picks))
        self.pending.pop(pane, None)
        return True, ""


class Source(unittest.TestCase):
    def test_no_key_is_ever_sent(self):
        src = FakeHerdr(screen=TRUST, hub=FakeHub(approvals.describe({"tool_name": "Bash", "tool_input": {"command": "x"}})))
        src.act("allow", "w:p1", "rev123")
        src.act("deny", "w:p1", "rev123")
        self.assertFalse([c for c in src.calls if "send-keys" in c])

    def test_a_request_shows_before_herdr_says_blocked(self):
        view = approvals.describe({"tool_name": "Bash", "tool_input": {"command": "touch a"}})
        s = FakeHerdr(status="working", hub=FakeHub(view)).row()
        self.assertEqual((s["s"], s["ask"], s["rev"], s["body"]), ("blocked", "approve", "rev123", "touch a"))

    def test_blocked_without_a_request_is_terminal_only(self):
        s = FakeHerdr(screen=TRUST, hub=FakeHub()).row()
        self.assertEqual((s["s"], s["ask"]), ("blocked", "terminal"))

    def test_terminal_only_cannot_be_answered_without_a_hub_request(self):
        src = FakeHerdr(screen=TRUST)
        self.assertEqual(src.act("allow", "w:p1", "x"), (False, "answer it in the terminal"))

    def test_after_an_answer_it_reads_as_working_until_herdr_catches_up(self):
        view = approvals.describe({"tool_name": "Bash", "tool_input": {"command": "x"}})
        src = FakeHerdr(screen=TRUST, hub=FakeHub(view))
        self.assertEqual(src.act("allow", "w:p1", "rev123"), (True, ""))
        s = src.row()
        self.assertEqual((s["s"], s["d"]), ("working", "answered on board"))
        at, where = src.answered["w:p1"]
        src.answered["w:p1"] = (at - sources.ANSWER_SETTLES_S - 1, where)
        self.assertEqual(src.row()["s"], "blocked")  # still blocked: it's a real prompt

    def test_a_question_goes_to_the_board_with_its_options(self):
        view = approvals.describe({"tool_name": "AskUserQuestion", "tool_input": Describe.QUESTION})
        s = FakeHerdr(hub=FakeHub(view)).row()
        self.assertEqual((s["ask"], s["tool"], s["qs"][0]["q"]), ("choice", "ASK", "Red or blue?"))

    def test_picks_reach_the_hub_and_show_as_answered(self):
        view = approvals.describe({"tool_name": "AskUserQuestion", "tool_input": Describe.QUESTION})
        hub = FakeHub(view)
        src = FakeHerdr(hub=hub, screen=TRUST)
        self.assertEqual(src.act("answer", "w:p1", "rev123", [{"o": [1]}]), (True, ""))
        self.assertEqual(hub.answers, [("w:p1", "rev123", "answer", [{"o": [1]}])])
        self.assertEqual(src.row()["d"], "answered on board")

    def test_only_pane_limits_answers_and_offers(self):
        view = approvals.describe({"tool_name": "Bash", "tool_input": {"command": "x"}})
        hub = FakeHub(view)
        src = FakeHerdr(screen=TRUST, hub=hub, only_panes=["w:p9"])
        self.assertEqual(src.act("allow", "w:p1", "rev123"), (False, "pane not enabled"))
        self.assertEqual(hub.answers, [])
        self.assertEqual(src.row()["ask"], "terminal")

    def test_open_reports_success_and_failure(self):
        src = FakeHerdr(focus_app="Some App")
        with mock.patch.object(sources.subprocess, "run", return_value=types.SimpleNamespace(returncode=0)) as run:
            self.assertEqual(src.act("open", "w:p1", ""), (True, ""))
        self.assertEqual(run.call_args.args[0], ["open", "-a", "Some App"])
        with mock.patch.object(sources.subprocess, "run", return_value=types.SimpleNamespace(returncode=1)):
            self.assertEqual(src.act("open", "w:p1", ""), (False, "could not open Some App"))
        src._herdr = lambda *a, **k: None
        self.assertEqual(src.act("open", "w:p1", ""), (False, "herdr could not focus it"))


class History(unittest.TestCase):
    def source(self):
        return FakeHerdr()

    def test_long_lines_reach_the_board_to_scroll(self):
        src = self.source()
        src._note("p", 0, "x" * 200)
        self.assertEqual(len(src.history("p")[0]["text"]), 120)

    def test_a_line_stays_one_line(self):
        src = self.source()
        src._note("p", 1, "Bash  git status\n  && ls")
        self.assertEqual(src.history("p")[0]["text"], "Bash git status && ls")


SESSION = "5d6d8b61-343d-4d8b-b463-a82fe6f71940"


def log_line(kind, content, **extra):
    return json.dumps({"type": kind, "timestamp": "2026-11-02T08:59:02.145Z", "cwd": "/repo",
                       "message": {"content": content}, **extra}, separators=(",", ":")).encode() + b"\n"


def tool(name, tool_id="t1", **args):
    return log_line("assistant", [{"type": "tool_use", "id": tool_id, "name": name, "input": args}])


def said(text):
    return log_line("assistant", [{"type": "text", "text": text}])


class ToolLines(unittest.TestCase):
    def test_each_says_what_is_going_on(self):
        for name, args, want in [
            ("Bash", {"command": "swift build", "description": "Build the app"}, "Build the app"),
            ("Bash", {"command": "swift  build\n -c release"}, "$ swift build -c release"),
            ("Read", {"file_path": "/repo/daemon/x.py"}, "Read daemon/x.py"),
            ("Edit", {"file_path": "/other/x.py"}, "Edit /other/x.py"),
            ("Grep", {"pattern": "TODO"}, 'Search for "TODO"'),
            ("WebFetch", {"url": "https://a.dev/b?c=1"}, "Fetch a.dev/b"),
            ("Agent", {"description": "Review it"}, "Subagent: Review it"),
            ("TodoWrite", {"todos": [{"status": "completed", "activeForm": "Old"},
                                     {"status": "in_progress", "activeForm": "Building"}]}, "Building"),
            ("AskUserQuestion", {"questions": [{"question": "Which?"}]}, "Asked: Which?"),
            ("mcp__context7__query-docs", {}, "context7: query-docs"),
            ("SomeNewTool", {}, "SomeNewTool"),
        ]:
            self.assertEqual(activity.tool_summary(name, args, "/repo"), want)

    def test_a_long_path_keeps_its_file_name(self):
        line = activity.tool_summary("Read", {"file_path": "/private/tmp/" + "x" * 80 + "/question.png"}, "/repo")
        self.assertTrue(line.endswith("/question.png"))
        self.assertLess(len(line), 60)

    def test_housekeeping_is_left_out(self):
        self.assertIsNone(activity.tool_summary("ToolSearch", {}))
        self.assertIsNone(activity.tool_summary("TodoWrite", {"todos": [{"status": "pending"}]}))

    def test_odd_arguments_do_not_raise(self):
        for name in ("Bash", "Read", "Grep", "WebFetch", "Agent", "TodoWrite", "AskUserQuestion", "Skill"):
            activity.tool_summary(name, {"command": 1, "file_path": None, "todos": "x", "questions": [1], "url": 5})


class Trail(unittest.TestCase):
    def fed(self, *lines):
        trail = activity.Trail(SESSION)
        trail.feed(b"".join(lines))
        return trail

    def texts(self, *lines):
        return [e.shown for e in self.fed(*lines).events]

    def test_a_turn_reads_in_order(self):
        self.assertEqual(self.texts(
            log_line("user", "fix the **bug**"),
            log_line("assistant", [{"type": "thinking", "thinking": "secret thoughts"}]),
            tool("Read", file_path="/repo/a.py"),
            log_line("assistant", [{"type": "text", "text": "\n## **Fixed** it.\nMore detail"}]),
        ), ["You: fix the **bug**", "Read a.py", "Fixed it."])

    def test_real_timestamps_are_kept(self):
        self.assertEqual(int(self.fed(tool("Read", file_path="a")).events[0].at), calendar.timegm((2026, 11, 2, 8, 59, 2)))

    def test_claude_wrappers_are_not_your_prompts(self):
        self.assertEqual(self.texts(
            log_line("user", "<local-command-stdout>hi</local-command-stdout>"),
            log_line("user", [{"type": "text", "text": "[Image: original 10x10]"}]),
            log_line("user", "Caveat: the messages below"),
            log_line("user", "<command-name>/compact</command-name>"),
            log_line("user", "summary", isCompactSummary=True),
            log_line("user", "meta", isMeta=True),
        ), ["You: /compact"])

    def test_subagents_are_their_own_story(self):
        self.assertEqual(self.texts(tool("Read", file_path="a", ), log_line(
            "assistant", [{"type": "text", "text": "inside"}], isSidechain=True)), ["Read a"])

    def test_a_tool_that_failed_says_so(self):
        trail = self.fed(tool("Bash", "t1", command="make"),
                         log_line("user", [{"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "exit 2"}]))
        self.assertEqual((trail.events[0].tone, trail.events[0].shown), (1, "Failed: $ make"))

    def test_being_told_no_is_not_failing(self):
        trail = self.fed(tool("Bash", "t1", command="make"), log_line(
            "user", [{"type": "tool_result", "tool_use_id": "t1", "is_error": True,
                      "content": "The person denied this from ttycat."}]))
        self.assertEqual((trail.events[0].tone, trail.events[0].shown), (0, "$ make"))

    def test_the_same_thing_twice_is_one_row(self):
        self.assertEqual(self.texts(tool("Read", "1", file_path="a"), tool("Read", "2", file_path="a"),
                                    tool("Read", "3", file_path="b")), ["Read a x2", "Read b"])

    def test_it_keeps_a_bounded_number(self):
        lines = [tool("Read", str(i), file_path=f"f{i}") for i in range(activity.KEEP + 5)]
        self.assertEqual(len(self.fed(*lines).events), activity.KEEP)

    def test_a_changed_format_gives_nothing_instead_of_an_error(self):
        self.assertEqual(self.texts(
            b"not json\n", b'{"type":"user"\n', b"[1,2]\n", b'"type":"user"\n',
            b'{"type":"user","message":5}\n',
            b'{"type":"assistant","message":{"content":[5,{"type":"tool_use"}]}}\n',
            b'{"type":"assistant","message":{"content":"text now"}}\n',
            b'{"type":"user","message":{"content":[null]}}\n',
            tool("Read", file_path="still works"),
        ), ["Read still works"])

    def test_noise_and_huge_lines_are_skipped(self):
        blob = json.dumps({"type": "api-request-blob", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "old"}}]}}, separators=(",", ":")).encode() + b"\n"
        huge = tool("Read", file_path="x" * activity.LONGEST_LINE)
        self.assertEqual(self.texts(blob, huge), [])


class Logs(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.home = Path(d.name)
        self.root = self.home / ".claude" / "projects"
        (self.root / "-repo").mkdir(parents=True)
        self.path = self.root / "-repo" / f"{SESSION}.jsonl"

    def activity_on(self, logs):
        a = activity.Activity(logs)
        a.sync({"w:p1": SESSION}, {"w:p1"})
        return a

    def local(self):
        return activity.LocalLogs(self.root)

    def test_only_new_lines_are_read(self):
        self.path.write_bytes(tool("Read", "1", file_path="a"))
        a = self.activity_on(self.local())
        self.assertEqual(a.now("w:p1"), "Read a")
        offset = a.trails["w:p1"].offset
        with self.path.open("ab") as f:
            f.write(tool("Read", "2", file_path="b"))
        a.sync({"w:p1": SESSION}, {"w:p1"})
        self.assertEqual([e.text for e in a.events("w:p1")], ["Read a", "Read b"])
        self.assertGreater(a.trails["w:p1"].offset, offset)

    def test_a_line_still_being_written_waits(self):
        whole = tool("Read", file_path="a")
        self.path.write_bytes(whole[:20])
        a = self.activity_on(self.local())
        self.assertEqual(a.events("w:p1"), [])
        with self.path.open("ab") as f:
            f.write(whole[20:])
        a.sync({"w:p1": SESSION}, {"w:p1"})
        self.assertEqual(a.now("w:p1"), "Read a")

    def test_a_long_log_is_only_read_from_near_its_end(self):
        with mock.patch.object(activity, "READ_BACK", 200):
            self.path.write_bytes(tool("Read", "0", file_path="old") + b"x" * 500 + b"\n" + tool("Read", "1", file_path="new"))
            self.assertEqual([e.text for e in self.activity_on(self.local()).events("w:p1")], ["Read new"])

    def test_a_log_that_started_over_starts_over(self):
        self.path.write_bytes(tool("Read", "1", file_path="a") * 2)
        a = self.activity_on(self.local())
        self.path.write_bytes(tool("Read", "2", file_path="b"))
        a.sync({"w:p1": SESSION}, {"w:p1"})
        self.assertEqual([e.shown for e in a.events("w:p1")], ["Read b"])

    def test_a_new_session_in_the_pane_starts_a_new_list(self):
        self.path.write_bytes(tool("Read", file_path="a"))
        a = self.activity_on(self.local())
        other = "aaaaaaaa-343d-4d8b-b463-a82fe6f71940"
        (self.root / "-repo" / f"{other}.jsonl").write_bytes(tool("Read", file_path="b"))
        a.sync({"w:p1": other}, {"w:p1"})
        self.assertEqual(a.now("w:p1"), "Read b")

    def test_a_missing_log_is_looked_for_again_later(self):
        now = [100.0]
        logs = activity.LocalLogs(self.root, clock=lambda: now[0])
        a = self.activity_on(logs)
        self.assertEqual(a.events("w:p1"), [])
        self.path.write_bytes(tool("Read", file_path="a"))
        a.sync({"w:p1": SESSION}, {"w:p1"})
        self.assertEqual(a.events("w:p1"), [])  # too soon
        now[0] += activity.LOOK_AGAIN_S
        a.sync({"w:p1": SESSION}, {"w:p1"})
        self.assertEqual(a.now("w:p1"), "Read a")

    def test_only_a_real_session_id_is_used(self):
        asked = []

        class Logs:
            def read(self, wanted):
                asked.extend(wanted)
                return {}

        a = activity.Activity(Logs())
        bad = ["", "../../secrets", "$(touch x)", "a b", "*", "5d6d8b61-343d-4d8b-b463-a82fe6f7194;"]
        a.sync({str(i): s for i, s in enumerate(bad)}, {str(i) for i in range(len(bad))})
        self.assertEqual((asked, a.trails), ([], {}))

    def test_first_reads_are_spread_out(self):
        sessions = {f"p{i}": f"{i:08d}-343d-4d8b-b463-a82fe6f71940" for i in range(activity.FIRST_READS_PER_SYNC + 2)}
        seen = []

        class Logs:
            def read(self, wanted):
                seen.append(len(wanted))
                return {s: (1, b"", False) for s in wanted}

        a = activity.Activity(Logs())
        a.sync(sessions, set(sessions))
        a.sync(sessions, set(sessions))
        self.assertEqual(seen, [activity.FIRST_READS_PER_SYNC, len(sessions)])

    # The remote reader's real script, run by a real sh, against a temporary home.
    def run_sh(self, script):
        done = subprocess.run(["sh", "-c", script], capture_output=True, env={"HOME": str(self.home), "PATH": os.environ["PATH"]})
        self.assertEqual(done.stderr, b"")
        return done.stdout

    def test_the_remote_script_reads_the_same_events(self):
        self.path.write_bytes(tool("Read", "1", file_path="a") + b'{"type":"api-request","x":"noise"}\n')
        a = self.activity_on(activity.RemoteLogs(self.run_sh))
        self.assertEqual(a.now("w:p1"), "Read a")
        with self.path.open("ab") as f:
            f.write(tool("Read", "2", file_path="b"))
        a.sync({"w:p1": SESSION}, {"w:p1"})
        self.assertEqual([e.text for e in a.events("w:p1")], ["Read a", "Read b"])

    def test_the_remote_script_sends_nothing_when_nothing_changed(self):
        self.path.write_bytes(tool("Read", file_path="a"))
        size = self.path.stat().st_size
        output = self.run_sh(activity.RemoteLogs.script({SESSION: size}))
        self.assertEqual(output, f"@@ {SESSION} {size}\n".encode())

    def test_the_remote_script_filters_before_sending(self):
        self.path.write_bytes(tool("Read", file_path="a") + b'{"type":"api-request-blob","x":"' + b"y" * 5000 + b'"}\n'
                              + tool("Read", file_path="z" * activity.LONGEST_LINE))
        output = self.run_sh(activity.RemoteLogs.script({SESSION: None}))
        self.assertLess(len(output), 1000)

    def test_the_remote_script_survives_a_missing_log_and_folder(self):
        self.assertEqual(self.run_sh(activity.RemoteLogs.script({"aaaaaaaa-343d-4d8b-b463-a82fe6f71940": None})), b"")
        shutil.rmtree(self.root)
        self.assertEqual(self.run_sh(activity.RemoteLogs.script({SESSION: None})), b"")

    def test_the_remote_script_goes_through_a_login_shell_unchanged(self):
        command = activity.shell_command(activity.RemoteLogs.script({SESSION: None}))
        self.path.write_bytes(tool("Read", file_path="a"))
        shell = shutil.which("zsh") or "sh"  # a Mac's login shell, where a glob with no match is an error
        done = subprocess.run([shell, "-c", command], capture_output=True, env={"HOME": str(self.home), "PATH": os.environ["PATH"]})
        self.assertIn(b"@@ " + SESSION.encode(), done.stdout)
        self.assertEqual(done.stderr, b"")


class Recent(unittest.TestCase):
    """What the board and the app are shown."""

    def source(self, lines=b""):
        src = FakeHerdr(status="working")
        src.activity.logs = types.SimpleNamespace(
            read=lambda wanted: {s: (len(lines), lines if at is None else b"", False) for s, at in wanted.items()})
        base = src._herdr

        def herdr(*args):
            reply = base(*args)
            if args[:2] == ("agent", "list"):
                reply["result"]["agents"][0]["agent_session"] = {"value": SESSION}
            return reply

        src._herdr = herdr
        return src

    def test_a_working_row_says_what_it_is_doing(self):
        self.assertEqual(self.source(tool("Edit", file_path="/repo/x.py")).row()["d"], "Edit x.py")

    def test_recent_lists_tool_calls_not_status_words(self):
        src = self.source(tool("Edit", file_path="/repo/x.py"))
        src.row()
        src.status = "idle"
        src.row()
        self.assertEqual([i["text"] for i in src.history("w:p1")], ["Edit x.py"])

    def test_answers_are_listed_among_them(self):
        src = self.source(tool("Edit", file_path="/repo/x.py"))
        src.row()
        src._note("w:p1", 2, "allowed on board")
        self.assertEqual([i["text"] for i in src.history("w:p1")], ["allowed on board", "Edit x.py"])

    def test_without_a_log_status_changes_are_still_shown(self):
        src = FakeHerdr(status="working")
        src.row()
        src.status = "idle"
        src.row()
        self.assertEqual([i["text"] for i in src.history("w:p1")], ["idle"])

    def test_a_finished_row_says_how_it_ended(self):
        src = self.source(tool("Edit", file_path="a") + log_line("assistant", [{"type": "text", "text": "All 94 tests pass."}]))
        src.status = "done"
        self.assertEqual(src.row()["d"], "All 94 tests pass.")

    def test_an_earlier_turns_words_are_not_how_this_one_ended(self):
        src = self.source(log_line("assistant", [{"type": "text", "text": "Old news."}]) + log_line("user", "do more")
                          + tool("Edit", file_path="a"))
        src.status = "done"
        self.assertEqual(src.row()["d"], "finished")

    def test_the_board_gets_five_in_ascii_and_the_app_gets_more_as_written(self):
        lines = b"".join(tool("Read", str(i), file_path=f"f{i}") for i in range(8)) + log_line("user", "f\u00fcr \u2713")
        src = self.source(lines)
        src.row()
        board = src.history("w:p1")
        self.assertEqual((len(board), board[0]["text"]), (5, "You: f?r ?"))
        app = src.history("w:p1", 30, False)
        self.assertEqual((len(app), app[0]["text"]), (9, "You: f\u00fcr \u2713"))

class MacApp(unittest.TestCase):
    """What the menu bar app may ask of the daemon."""

    class Recording:
        def __init__(self):
            self.calls = []

        def act(self, action, sid, rev, picks=None, via="board"):
            self.calls.append((action, sid, rev, picks, via))
            return True, ""

        def poll(self):
            return [{"id": "p1", "s": "working"}]

        def pet(self):
            return {"name": "Bit"}

    def test_the_app_can_do_only_what_is_listed(self):
        source = self.Recording()
        for action in ("deny-stop", "deny-retry", "deny-explain", "tap", "unknown", ""):
            reply = ttycatd.app_action(source, {"t": "app-act", "a": action, "id": "p1", "rev": "r", "ans": [1]})
            self.assertFalse(reply["ok"], action)
        self.assertEqual(source.calls, [])

    def test_the_app_can_open_an_agent(self):
        source = self.Recording()
        self.assertTrue(ttycatd.app_action(source, {"t": "app-act", "a": "open", "id": "p1"})["ok"])
        self.assertEqual(source.calls, [("open", "p1", "", None, "app")])

    def test_answers_carry_the_prompt_they_were_given_for(self):
        source = self.Recording()
        picks = [{"o": [1], "t": ""}]
        ttycatd.app_action(source, {"t": "app-act", "a": "answer", "id": "p1", "rev": "r1", "ans": picks})
        self.assertEqual(source.calls, [("answer", "p1", "r1", picks, "app")])

    def answerable(self, request=None):
        """A real hub and a real source, with one prompt waiting."""
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        hub = approvals.Hub(Path(d.name) / "hook.sock")
        self.addCleanup(hub.close)
        hook = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(hook.close)
        hook.connect(str(hub.path))
        conn, _ = hub.listener.accept(), None
        conn = conn[0]
        conn.setblocking(False)
        hub.incoming[conn] = bytearray()
        hook.sendall(json.dumps({"pane": "w:p1", "request": request or {"tool_name": "Bash", "tool_input": {"command": "touch a"}}}).encode() + b"\n")
        time.sleep(0.02)
        hub.handle(conn)
        hook.settimeout(1)
        return FakeHerdr(hub=hub), hub, hook

    def test_allow_from_the_app_reaches_the_hook_that_asked(self):
        src, hub, hook = self.answerable()
        rev = hub.get("w:p1").id
        reply = ttycatd.app_action(src, {"t": "app-act", "a": "allow", "id": "w:p1", "rev": rev})
        self.assertEqual(reply, {"ok": True, "msg": ""})
        self.assertEqual(json.loads(hook.recv(4096)), {"behavior": "allow"})
        self.assertEqual(src.history("w:p1")[0]["text"], "allowed in the app")

    def test_an_answer_meant_for_another_prompt_is_refused(self):
        src, hub, hook = self.answerable()
        for rev in ("", "someotherrev"):
            reply = ttycatd.app_action(src, {"t": "app-act", "a": "allow", "id": "w:p1", "rev": rev})
            self.assertEqual(reply, {"ok": False, "msg": "prompt already gone"})
        self.assertIsNotNone(hub.get("w:p1"))  # still waiting, and undecided
        hook.setblocking(False)
        with self.assertRaises(BlockingIOError):
            hook.recv(4096)

    def test_typing_on_a_mac_keeps_what_was_typed(self):
        question = {"tool_name": "AskUserQuestion", "tool_input": {"questions": [
            {"question": "Which?", "options": [{"label": "A"}, {"label": "B"}]}]}}
        src, hub, hook = self.answerable(question)
        picks = [{"o": [], "t": "f\u00fcr beide \u2713\u202e\x07"}]
        reply = ttycatd.app_action(src, {"t": "app-act", "a": "answer", "id": "w:p1", "rev": hub.get("w:p1").id, "ans": picks})
        self.assertTrue(reply["ok"], reply)
        # Umlauts stay; invisible direction overrides and control characters don't.
        self.assertEqual(json.loads(hook.recv(4096))["answers"], {"Which?": "f\u00fcr beide \u2713"})

    def test_a_big_request_can_take_several_reads(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        body = json.dumps({"t": "app-act", "ans": ["x" * 9000]}).encode()
        threading.Thread(target=lambda: (a.sendall(body[:100]), time.sleep(0.02), a.sendall(body[100:]))).start()
        b.settimeout(1)
        self.assertEqual(len(ttycatd.read_request(b)["ans"][0]), 9000)

    def test_garbage_is_not_a_request(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        a.sendall(b"[1,2]")
        a.close()
        with self.assertRaises(ValueError):
            ttycatd.read_request(b)

    def test_status_says_where_things_stand(self):
        board = ttycatd.Board(None)
        reply = ttycatd.status_message(self.Recording(), board)
        self.assertEqual(reply["pid"], os.getpid())
        self.assertEqual(reply["board"], {"connected": False, "port": None, "fw": None, "dim": False, "approves": False})
        self.assertEqual(reply["s"], [{"id": "p1", "s": "working"}])
        self.assertEqual(reply["machines"], [])
        self.assertNotIn("hist", reply)

    def test_status_adds_what_notifications_need_but_the_board_does_not_get(self):
        source = self.Recording()
        source.focus_app = "Ghostty"
        source.desk = lambda: {"p1": {"foc": True, "said": "Fertig: Tests laufen"}}
        board = ttycatd.Board(None)
        reply = ttycatd.status_message(source, board)
        self.assertEqual(reply["s"], [{"id": "p1", "s": "working", "foc": True, "said": "Fertig: Tests laufen"}])
        self.assertEqual(reply["term"], "Ghostty")
        self.assertEqual(ttycatd.state_message(source, None)["s"], [{"id": "p1", "s": "working"}])

    def test_status_lists_other_machines(self):
        source = self.Recording()
        source.remotes = [types.SimpleNamespace(host="studio", reachable=True, stale=False),
                          types.SimpleNamespace(host="laptop", reachable=True, stale=True)]
        machines = ttycatd.status_message(source, ttycatd.Board(None))["machines"]
        self.assertEqual(machines, [{"host": "studio", "up": True, "answers": False}, {"host": "laptop", "up": False, "answers": False}])

    def test_a_second_daemon_refuses_to_start(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "daemon.lock"
            held = ttycatd.only_instance(path)
            with mock.patch("sys.stderr"), self.assertRaises(SystemExit) as stopped:
                ttycatd.only_instance(path)
            self.assertEqual(stopped.exception.code, ttycatd.ALREADY_RUNNING)
            held.close()
            ttycatd.only_instance(path).close()  # free again once the first has gone


BASH_PROMPT = """
─────────────────────────────────────────
 Bash command
 touch a
 Do you want to proceed?
 ❯ 1. Yes
   2. No
"""


class ShellPromptText(unittest.TestCase):
    """What a prompt only the terminal can answer says on the board and in a notification."""

    RULE = "─" * 40
    DASH = "╌" * 40

    def test_a_shell_prompt_is_just_its_command(self):
        screen = f"""
{self.RULE}
 Bash command
 Create directory two-dir
{self.DASH}
 mkdir -p two-dir
{self.DASH}
 Do you want to proceed?
 ❯ 1. Yes
   2. Yes, and always allow access to /private/tmp/claude-502/-Users-long-5d6d
      3d-4d8b-b463-a82fe6f71940/scratchpad/notify from this project
   3. No
 Esc to cancel · Tab to amend
"""
        ask = prompts.parse("claude", screen)
        self.assertEqual((ask.tool, ask.verb, ask.body), ("BASH", "wants to run", "mkdir -p two-dir"))
        self.assertEqual(ask.detail, "mkdir -p two-dir")

    def test_a_shell_prompt_without_dashes(self):
        ask = prompts.parse("claude", BASH_PROMPT)
        self.assertEqual((ask.tool, ask.body), ("BASH", "touch a"))

    def test_a_wrapped_option_is_not_part_of_any_prompt(self):
        screen = f"""
{self.RULE}
 Do you trust this folder?
 ❯ 1. Yes
   2. Yes, and remember this for /a/very/long/path/that
      /wraps/onto/another/line
"""
        self.assertEqual(prompts.parse("claude", screen).body, "Do you trust this folder?")


class Desk(unittest.TestCase):
    """What the app's notifications are told, on top of the board's rows."""

    def source(self, focused=False, words=b""):
        src = Recent().source(words)
        base = src._herdr

        def herdr(*args):
            reply = base(*args)
            if args[:2] == ("agent", "list"):
                reply["result"]["agents"][0]["focused"] = focused
            return reply

        src._herdr = herdr
        return src

    def test_the_focused_pane_is_named(self):
        src = self.source(focused=True)
        src.row()
        self.assertEqual(src.desk()["w:p1"].get("foc"), True)

    def test_other_panes_are_not(self):
        src = self.source(focused=False)
        src.row()
        self.assertNotIn("w:p1", src.desk())

    def test_a_finished_turn_says_how_it_ended_in_full_characters(self):
        src = self.source(words=said("Fertig: alle Tests laufen und grün ✓"))
        src.row()
        src.status = "done"
        src.row()
        self.assertEqual(src.desk()["w:p1"]["said"], "Fertig: alle Tests laufen und grün ✓")

    def test_a_turn_still_going_says_nothing_yet(self):
        src = self.source(words=said("Looking at it"))
        src.row()
        self.assertNotIn("w:p1", src.desk())

    def test_control_characters_never_reach_a_notification(self):
        src = self.source(words=said("ok\x1b[31m \u202eevil"))
        src.row()
        src.status = "done"
        src.row()
        text = src.desk()["w:p1"]["said"]
        self.assertTrue(all(c.isprintable() for c in text), text)

    def test_long_words_are_cut(self):
        src = self.source(words=said("x" * 5000))
        src.row()
        src.status = "done"
        src.row()
        self.assertLessEqual(len(src.desk()["w:p1"]["said"]), sources.MAX_SAID)

    def test_the_board_never_gets_these(self):
        src = self.source(focused=True)
        self.assertNotIn("foc", src.row())

class Life(unittest.TestCase):
    """Noon on a fixed day unless a test moves the clock. Nights are real local hours."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "pet.json"
        self.now = 1_000_000.0
        self.hour = 12

    def local(self, t):
        hour = (self.hour + int((t - 1_000_000.0) // 3600)) % 24
        return time.struct_time((2026, 1, 1 + int((t - 1_000_000.0) // 86400), hour, 0, 0, 3, 1, 0))

    def tearDown(self):
        self.tmp.cleanup()

    def pet(self):
        return Pet(self.path, clock=lambda: self.now, local=self.local)

    def later(self, p, hours):
        self.now += hours * 3600
        p.tick()

    def test_it_gets_hungry_and_bored_awake(self):
        p = self.pet()
        p.food, p.fun = 50.0, 50.0
        self.later(p, 5)
        self.assertAlmostEqual(p.food, 20, places=3)
        self.assertAlmostEqual(p.fun, 25, places=3)

    def test_sleep_restores_energy_and_slows_hunger(self):
        self.hour = 23
        p = self.pet()
        p.food, p.energy = 50.0, 10.0
        self.later(p, 4)
        self.assertAlmostEqual(p.food, 42, places=3)
        self.assertAlmostEqual(p.energy, 70, places=3)
        self.assertTrue(p.message()["asleep"])

    def test_a_turn_earns_a_snack_and_a_snack_fills_more_than_rice(self):
        p = self.pet()
        p.snacks, p.food = 0, 10.0
        p.finished()
        self.assertEqual((p.snacks, p.total, p.today), (1, 1, 1))
        self.assertEqual(p.act("feed"), (True, ""))
        self.assertEqual((p.snacks, round(p.food)), (0, 40))
        self.assertEqual(p.act("feed"), (True, ""))  # the pantry's empty: it still gets fed
        self.assertEqual((p.snacks, round(p.food)), (0, 50))

    def test_it_refuses_food_when_full(self):
        p = self.pet()
        p.food = 96.0
        self.assertEqual(p.act("feed"), (False, "Bit is full"))

    def test_messes_pile_up_and_get_cleaned(self):
        p = self.pet()
        self.assertEqual(p.act("clean"), (False, "already clean"))
        p.food = 100.0
        self.later(p, petstate.POOP_EVERY_H + 0.1)
        self.assertEqual(p.poops, 1)
        self.assertEqual(p.need(), "mess")
        self.assertEqual(p.act("clean"), (True, ""))
        self.assertEqual(p.poops, 0)

    def test_messes_stop_at_the_limit(self):
        p = self.pet()
        for _ in range(6):
            p.food = 100.0
            self.hour = 12
            self.later(p, 1)
            p.poops = min(petstate.MAX_POOPS, p.poops + 1)
        self.later(p, 11)
        self.assertLessEqual(p.poops, petstate.MAX_POOPS)

    def test_it_can_be_kept_up_and_put_down(self):
        self.hour = 23
        p = self.pet()
        self.assertEqual(p.act("feed"), (False, "Bit is asleep"))
        p.act("sleep")
        self.assertFalse(p.message()["asleep"])
        self.assertEqual(p.act("feed")[0], True)
        self.hour, self.now = 12, self.now
        q = self.pet()
        q.act("sleep")
        self.assertTrue(q.message()["asleep"])
        q.act("sleep")
        self.assertFalse(q.message()["asleep"])

    def test_a_nap_ends_when_it_is_rested(self):
        p = self.pet()
        p.energy = 90.0
        p.act("sleep")
        self.later(p, 1)
        self.assertEqual(p.nap_until, 0)
        self.assertFalse(p.message()["asleep"])

    def test_play_helps_fun_costs_energy_and_keeps_the_best_score(self):
        p = self.pet()
        p.fun, p.energy, p.food = 20.0, 50.0, 50.0
        self.assertEqual(p.act("play", "5"), (True, ""))
        self.assertEqual((round(p.fun), round(p.energy), round(p.food), p.best), (50, 42, 46, 5))
        p.act("play", "2")
        self.assertEqual(p.best, 5)

    def test_a_bad_score_counts_as_zero(self):
        p = self.pet()
        self.assertEqual(p.act("play", "lots")[0], True)
        self.assertEqual(p.best, 0)

    def test_patting_is_limited_so_it_cannot_replace_play(self):
        p = self.pet()
        p.fun = 50.0
        for _ in range(20):
            p.act("pat")
        self.assertEqual(round(p.fun), 52)

    def test_it_is_always_healthy(self):
        p = self.pet()
        p.food, p.poops = 0.0, petstate.MAX_POOPS
        for _ in range(4):
            self.later(p, 10)
            p.here()
        self.assertNotIn("sick", p.message())
        self.assertNotEqual(p.need(), "sick")
        p.awake_until = self.now + 3600
        self.assertEqual(p.act("heal"), (False, "unknown action"))

    def test_a_day_goes_by_and_it_grows(self):
        p = self.pet()
        self.assertEqual(p.message()["grown"], 0)
        self.now += 2 * 86400
        self.assertEqual(p.message()["grown"], 2 * petstate.GROW_PER_DAY)

    def test_a_turn_alone_does_not_make_it_grow(self):
        p = self.pet()
        for _ in range(50):
            p.finished()
        self.assertEqual((p.message()["total"], p.message()["grown"]), (50, 0))

    def test_tokens_pay_less_and_less_up_to_a_cap(self):
        bonus = petstate.token_bonus
        self.assertEqual(bonus(0), 0)
        self.assertGreater(bonus(100_000) - bonus(0), bonus(200_000) - bonus(100_000))
        self.assertGreater(bonus(200_000) - bonus(100_000), bonus(300_000) - bonus(200_000))
        self.assertAlmostEqual(bonus(10_000_000), petstate.TOKEN_BONUS, places=6)
        self.assertLessEqual(bonus(10 ** 12), petstate.TOKEN_BONUS)

    def test_no_amount_of_tokens_buys_more_than_the_cap(self):
        p, q = self.pet(), Pet(None, clock=lambda: self.now, local=self.local)
        p.note_tokens({"2026-01-01": 10_000_000})
        q.note_tokens({"2026-01-01": 10 ** 12})
        self.assertEqual(p.grown(), q.grown())
        self.assertEqual(p.grown(), int(petstate.TOKEN_BONUS))

    def test_the_bonus_resets_each_day(self):
        p = self.pet()
        self.now += 86400
        p.note_tokens({"2026-01-01": 10_000_000, "2026-01-02": 10_000_000})
        self.assertEqual(p.grown(), int(1 * petstate.GROW_PER_DAY + 2 * petstate.TOKEN_BONUS))

    def test_only_five_days_a_week_count(self):
        week = [f"2026-01-{d:02}" for d in range(5, 12)]  # Monday to Sunday
        self.now += 11 * 86400
        every_day = Pet(None, clock=lambda: self.now, local=self.local)
        two_off = Pet(None, clock=lambda: self.now, local=self.local)
        every_day.counted_from = two_off.counted_from = "2026-01-05"
        every_day.note_tokens({d: 10_000_000 for d in week})
        two_off.note_tokens({d: 10_000_000 for d in week[:5]})
        self.assertEqual(every_day.grown(), two_off.grown())

    def test_a_week_of_days_off_earns_only_the_days(self):
        p = self.pet()
        self.now += 7 * 86400
        p.here()
        self.assertEqual(p.grown(), 7 * petstate.GROW_PER_DAY)

    def test_other_machines_add_to_the_same_day(self):
        p = self.pet()
        p.note_tokens({"2026-01-01": 100_000})
        p.note_tokens({"2026-01-01": 100_000}, where="box")
        self.assertAlmostEqual(sum(p._tokens_by_day().values()), 200_000)

    def test_a_days_count_never_falls(self):
        p = self.pet()
        p.note_tokens({"2026-01-01": 500})
        p.note_tokens({"2026-01-01": 100})
        self.assertEqual(p.tokens["here"]["2026-01-01"], 500)

    def test_a_pet_from_before_keeps_its_level_and_counts_on(self):
        self.path.write_text(json.dumps({"total": 147, "today": 0, "day": "2026-01-01", "born": 1.0}))
        p = self.pet()
        self.assertEqual(p.grown(), 147)
        p.save()
        self.now += 86400
        q = self.pet()
        q.note_tokens({"2025-12-25": 10_000_000})  # from before growth counted
        self.assertEqual(q.grown(), 147 + petstate.GROW_PER_DAY)

    def test_growth_survives_a_restart(self):
        p = self.pet()
        p.note_tokens({"2026-01-01": 10_000_000})
        p.here()
        p.save()
        q = self.pet()
        self.assertEqual((q.grown(), q.around), (int(petstate.TOKEN_BONUS), ["2026-01-01"]))

    def test_coming_back_from_a_day_off_it_is_rested_and_glad(self):
        p = self.pet()
        p.here()
        p.food, p.fun, p.energy, p.poops = 5.0, 5.0, 5.0, 3
        self.now += 2 * 86400
        before = p.ev
        p.here()
        m = p.message()
        self.assertEqual((m["ev"], m["evk"], m["need"], m["poops"]), (before + 1, "rested", "", 0))
        self.assertGreaterEqual((m["food"], m["fun"], m["energy"]), (70, 70, 100))

    def test_it_says_hello_once_a_day(self):
        p = self.pet()
        p.here()
        self.now += 2 * 86400
        p.here()
        before = p.ev
        p.here()
        p.act("here")
        self.assertEqual(p.ev, before)

    def test_longer_away_is_a_bigger_hello(self):
        p = self.pet()
        p.here()
        self.now += (petstate.LONG_AWAY_DAYS + 1) * 86400
        p.here()
        self.assertEqual(p.evk, "welcome")

    def test_a_day_with_agents_is_not_a_day_off(self):
        p = self.pet()
        p.here()
        p.note_tokens({"2026-01-02": 500})
        self.now += 2 * 86400
        before = p.ev
        p.here()
        self.assertEqual(p.ev, before)

    def test_coming_back_the_next_day_is_nothing_special(self):
        p = self.pet()
        p.here()
        self.now += 86400
        before = p.ev
        p.here()
        self.assertEqual(p.ev, before)

    def test_a_new_pet_has_nothing_to_come_back_to(self):
        p = self.pet()
        before = p.ev
        p.here()
        self.assertEqual(p.ev, before)

    def test_it_still_says_hello_after_working_in_the_terminal_first(self):
        p = self.pet()
        p.here()
        self.now += 2 * 86400
        p.finished()
        before = p.ev
        p.here()
        self.assertEqual(p.ev, before + 1)

    def test_while_you_are_away_its_needs_wait_and_it_asks_for_nothing(self):
        p = self.pet()
        p.food, p.awake_until = 20.0, self.now + 10 ** 8
        p.here()
        self.later(p, 2)
        self.assertLess(p.food, 20)
        food = 10.0
        p.food = food
        self.later(p, petstate.AWAY_AFTER_S / 3600)
        low = p.food
        self.later(p, 24)
        self.assertEqual(p.food, low)
        self.assertEqual(p.need(), "")

    def test_a_finished_turn_counts_as_being_around(self):
        p = self.pet()
        p.food, p.awake_until = 90.0, self.now + 10 ** 8
        self.later(p, petstate.AWAY_AFTER_S / 3600 + 1)
        p.finished()
        food = p.food
        self.later(p, 1)
        self.assertLess(p.food, food)

    def birthday_pet(self, day=1):
        local = lambda t: time.struct_time((2026, 2, day, 12, 0, 0, 6, 32, 0))
        p = Pet(None, clock=lambda: self.now, local=local)
        p.born = self.now - 5 * 86400
        return p

    def test_the_first_of_the_month_is_its_birthday(self):
        self.assertTrue(self.birthday_pet().message()["birthday"])
        self.assertFalse(self.birthday_pet(day=2).message()["birthday"])

    def test_the_day_it_is_born_is_not_a_birthday(self):
        p = self.birthday_pet()
        p.born = self.now - 3600
        self.assertFalse(p.birthday())

    def test_it_says_happy_birthday_once(self):
        p = self.birthday_pet()
        before = p.ev
        p.here()
        p.here()
        self.assertEqual((p.ev, p.evk), (before + 1, "birthday"))

    def test_a_birthday_earns_no_growth_and_gives_no_top_up(self):
        p = self.birthday_pet()
        p.food = 10.0
        before = p.grown()
        p.here()
        self.assertEqual((p.grown(), p.food), (before, 10.0))

    def test_missing_a_birthday_costs_nothing(self):
        p = self.birthday_pet(day=2)
        p.here()
        self.assertEqual(p.evk, "")
        self.assertEqual(p.need(), "")

    def test_unknown_action_changes_nothing(self):
        self.assertEqual(self.pet().act("allow"), (False, "unknown action"))

    def test_it_survives_a_restart(self):
        p = self.pet()
        p.finished()
        p.act("feed")
        p.rename("Pixel")
        q = self.pet()
        self.assertEqual((q.total, q.snacks, q.name), (1, p.snacks, "Pixel"))
        self.assertEqual(q.message()["name"], "Pixel")

    def test_time_off_is_capped(self):
        p = self.pet()
        p.food, p.awake_until = 100.0, self.now + 10 ** 8
        p.save()
        self.now += 30 * 86400
        q = self.pet()
        q.tick()
        self.assertEqual(q.food, 100)  # nobody's been around, so it waited

    def test_the_old_file_format_keeps_its_counts(self):
        self.path.write_text(json.dumps({"total": 9, "today": 4, "day": "2026-01-01"}))
        m = self.pet().message()
        self.assertEqual((m["total"], m["today"], m["snacks"]), (9, 4, petstate.STARTING_SNACKS))

    def test_today_rolls_over(self):
        self.path.write_text(json.dumps({"total": 9, "today": 4, "day": "2001-01-01"}))
        self.assertEqual(self.pet().message()["today"], 0)

    def test_broken_file_starts_over(self):
        self.path.write_text("{nope")
        self.assertEqual(self.pet().message()["total"], 0)

    def test_rename_is_cleaned_for_the_board(self):
        p = self.pet()
        p.rename("  Bitty\x07 the Long Named")
        self.assertEqual(p.name, "Bitty the L")
        p.rename("   ")
        self.assertEqual(p.name, "Bitty the L")

    def test_needs_are_ranked(self):
        p = self.pet()
        p.food, p.poops = 0.0, 1
        self.assertEqual(p.need(), "food")
        p.food = 80.0
        self.assertEqual(p.need(), "mess")
        p.poops, p.energy = 0, 10.0
        self.assertEqual(p.need(), "tired")
        p.energy, p.fun = 80.0, 10.0
        self.assertEqual(p.need(), "bored")
        p.fun = 80.0
        self.assertEqual(p.need(), "")

    def source(self, *states):
        src = FakeHerdr(pet=Pet(self.path), hub=FakeHub(), screen=TRUST)
        for st in states:
            src.status = st
            src.row()
        return src.pet()["total"]

    def test_a_finished_turn_feeds_it(self):
        self.assertEqual(self.source("working", "done"), 1)
        self.assertEqual(self.source("working", "idle"), 2)

    def test_denying_earns_what_allowing_does(self):
        allowed = self.source("working", "blocked", "working", "done")
        self.path.unlink(missing_ok=True)
        denied = self.source("working", "blocked", "idle")
        self.assertEqual((allowed, denied), (1, 1))

    def test_a_prompt_on_its_own_does_not_feed_it(self):
        self.assertEqual(self.source("working", "blocked"), 0)

    def test_the_app_can_look_after_the_pet(self):
        p = Pet(self.path)
        p.food, p.nap_until, p.awake_until = 10.0, 0, time.time() + 1000
        fleet = Fleet(FakeHerdr(pet=p, hub=FakeHub()), [], start=False)
        reply = ttycatd.app_action(fleet, {"a": "pet-feed", "id": "", "rev": ""})
        self.assertEqual(reply, {"ok": True, "msg": ""})
        self.assertGreater(p.food, 10)
        reply = ttycatd.app_action(fleet, {"a": "pet-play", "id": "", "rev": "12"})
        self.assertTrue(reply["ok"])
        self.assertEqual(p.best, 12)

    def test_care_is_announced_to_both_views(self):
        p = Pet(self.path)
        p.food, p.nap_until, p.awake_until = 10.0, 0, time.time() + 1000
        before = p.message()["ev"]
        self.assertEqual(p.message()["evk"], "")
        self.assertTrue(p.act("feed")[0])
        after = p.message()
        self.assertEqual((after["ev"], after["evk"]), (before + 1, "eat"))
        self.assertTrue(p.act("pat")[0])
        self.assertEqual(p.message()["evk"], "heart")

    def test_a_refused_action_announces_nothing(self):
        p = Pet(self.path)
        p.poops, p.nap_until, p.awake_until = 0, 0, time.time() + 1000
        before = p.message()["ev"]
        self.assertFalse(p.act("clean")[0])
        self.assertEqual(p.message()["ev"], before)

    def test_sleeping_is_news_but_has_no_reaction(self):
        p = Pet(self.path)
        before = p.message()["ev"]
        self.assertTrue(p.act("sleep")[0])
        self.assertEqual((p.message()["ev"], p.message()["evk"]), (before + 1, ""))

    def test_the_app_cant_do_anything_else_to_the_pet(self):
        p = Pet(self.path)
        fleet = Fleet(FakeHerdr(pet=p, hub=FakeHub()), [], start=False)
        for action in ("pet-reward", "pet-", "pet-rename"):
            reply = ttycatd.app_action(fleet, {"a": action, "id": "", "rev": ""})
            self.assertFalse(reply["ok"], action)

    def test_care_goes_to_the_pet_whatever_the_session(self):
        p = Pet(self.path)
        p.food, p.nap_until, p.awake_until = 10.0, 0, time.time() + 1000
        fleet = Fleet(FakeHerdr(pet=p, hub=FakeHub()), [], start=False)
        self.assertEqual(fleet.act("pet-feed", "", ""), (True, ""))
        self.assertGreater(p.food, 10)


class HookInstall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "settings.json"
        self.other = {"matcher": "*", "hooks": [{"type": "command", "command": "other-tool"}]}
        self.path.write_text(json.dumps({"env": {"A": "1"}, "hooks": {
            "PreToolUse": [self.other], "PermissionRequest": [self.other]}}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_install_is_idempotent_and_keeps_everything_else(self):
        ttycatd.hook_command("install", self.path)
        ttycatd.hook_command("install", self.path)
        s = json.loads(self.path.read_text())
        self.assertEqual(s["env"], {"A": "1"})
        self.assertEqual(s["hooks"]["PreToolUse"], [self.other])
        self.assertEqual(len(s["hooks"]["PermissionRequest"]), 2)
        self.assertEqual(s["hooks"]["PermissionRequest"][0], self.other)
        self.assertEqual(ttycatd.hook_command("status", self.path), "installed")

    def test_remove_takes_only_ours(self):
        ttycatd.hook_command("install", self.path)
        ttycatd.hook_command("remove", self.path)
        s = json.loads(self.path.read_text())
        self.assertEqual(s["hooks"]["PermissionRequest"], [self.other])
        self.assertEqual(ttycatd.hook_command("status", self.path), "not installed")

    def hook_entry(self, command):
        return {"matcher": "*", "hooks": [{"type": "command", "command": command, "timeout": 86400}]}

    def commands(self):
        return [h["command"] for e in json.loads(self.path.read_text())["hooks"]["PermissionRequest"] for h in e["hooks"]]

    def with_entry(self, command):
        s = json.loads(self.path.read_text())
        s["hooks"]["PermissionRequest"].append(self.hook_entry(command))
        self.path.write_text(json.dumps(s))

    def test_an_older_entry_from_the_repo_counts_as_ours(self):
        self.with_entry("python3 -I /Users/x/project/daemon/hook.py")
        self.assertEqual(ttycatd.hook_command("status", self.path), "installed")
        ttycatd.hook_command("install", self.path)
        self.assertEqual(len(self.commands()), 2)  # replaced, not added to

    def test_an_entry_in_an_app_with_a_space_in_its_name_counts_as_ours(self):
        self.with_entry("'/Applications/ttycat.app/Contents/Resources/python/bin/python3' -I "
                        "'/Applications/ttycat.app/Contents/Resources/daemon/hook.py'")
        self.assertEqual(ttycatd.hook_command("status", self.path), "installed")

    def test_someone_elses_hook_isnt_ours(self):
        for command in ("python3 /x/my-daemon/hook.py", "cat daemon/hook.py.bak", "other-tool"):
            self.assertFalse(ttycatd._is_ours(self.hook_entry(command)), command)

    def test_repair_leaves_a_working_entry_alone(self):
        self.with_entry(f"python3 -I {self.tmp.name}/daemon/hook.py")
        Path(self.tmp.name, "daemon").mkdir()
        Path(self.tmp.name, "daemon", "hook.py").write_text("")
        before = self.path.read_text()
        self.assertEqual(ttycatd.hook_command("repair", self.path), "installed")
        self.assertEqual(self.path.read_text(), before)

    def test_repair_never_adds_a_hook_you_didnt_choose(self):
        before = self.path.read_text()
        self.assertEqual(ttycatd.hook_command("repair", self.path), "not installed")
        self.assertEqual(self.path.read_text(), before)

    def test_repair_fixes_one_pointing_at_an_app_thats_moved(self):
        self.with_entry("'/Applications/Old Place.app/Contents/Resources/python/bin/python3' -I "
                        "'/Applications/Old Place.app/Contents/Resources/daemon/hook.py'")
        self.assertEqual(ttycatd.hook_command("repair", self.path), "installed")
        self.assertNotIn("Old Place", self.path.read_text())
        self.assertEqual(self.commands()[0], "other-tool")
        self.assertEqual(self.commands()[1], ttycatd.hook_shell_command())

    def test_the_command_survives_spaces_in_the_path(self):
        with mock.patch.object(ttycatd.sys, "executable", "/Applications/ttycat.app/Contents/Resources/python/bin/python3"), \
                mock.patch.object(ttycatd, "HOOK_SCRIPT", Path("/Applications/ttycat.app/Contents/Resources/daemon/hook.py")):
            words = shlex.split(ttycatd.hook_shell_command())
        self.assertEqual(words[0], "/Applications/ttycat.app/Contents/Resources/python/bin/python3")
        self.assertEqual(words[1:], ["-I", "/Applications/ttycat.app/Contents/Resources/daemon/hook.py"])

    def test_outside_the_app_it_uses_the_shells_python(self):
        with mock.patch.object(ttycatd.sys, "executable", "/opt/homebrew/bin/python3.14"):
            self.assertTrue(ttycatd.hook_shell_command().startswith("python3 -I "))

    def test_broken_settings_are_left_alone(self):
        self.path.write_text("{broken")
        with self.assertRaises(ValueError):
            ttycatd.hook_command("install", self.path)
        self.assertEqual(self.path.read_text(), "{broken")

    def test_backup_is_written(self):
        before = self.path.read_text()
        ttycatd.hook_command("install", self.path)
        self.assertEqual(self.path.with_name("settings.json.ttycat-backup").read_text(), before)


if __name__ == "__main__":
    unittest.main()
