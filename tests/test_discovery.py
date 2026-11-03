"""Recognising agents, here and on other machines. Everything is text: no real process or machine."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

DAEMON = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DAEMON))
import claude_sessions as cs  # noqa: E402
import discovery as d  # noqa: E402
import sources  # noqa: E402
from petstate import Pet  # noqa: E402
from sources import Fleet, HerdrSource  # noqa: E402

SID = "5d6d8b61-343d-4d8b-b463-a82fe6f71940"
PANE = "s:5d6d8b61"


class Commands(unittest.TestCase):
    def test_agents_by_their_own_name(self):
        for command, agent in (("claude", "claude"), ("/Users/x/.local/bin/codex --full-auto", "codex"),
                               ("opencode", "opencode"), ("/opt/homebrew/bin/gemini", "gemini"), ("CODEX", "codex")):
            self.assertEqual(d.agent_in_command(command), agent, command)

    def test_through_a_runtime(self):
        for command, agent in (("node /Users/x/.nvm/versions/node/v25/bin/gemini", "gemini"),
                               ("node /x/node_modules/@openai/codex/bin/codex.js", "codex"),
                               ("node --no-warnings /x/node_modules/@mariozechner/pi-coding-agent/dist/cli.js", "pi"),
                               ("bun /x/node_modules/opencode-ai/bin/run.js", "opencode"),
                               ("python3 /opt/bin/aider", "aider"),
                               ("node --require /x/hook.js /x/bin/codex", "codex")):
            self.assertEqual(d.agent_in_command(command), agent, command)

    def test_a_build_of_it(self):
        self.assertEqual(d.agent_in_command("/x/codex-aarch64-apple-darwin"), "codex")

    def test_not_when_it_is_only_mentioned(self):
        for command in ("vim codex.md", "git log --grep claude", "python3 -c codex", "node -e claude", "sh -c codex",
                        "python3 -m codex", "grep pi file", "cat /x/node_modules/@openai/codex/README.md",
                        "tmux new -s claude", "ssh claude", "", "codexify", "piano", "-zsh"):
            self.assertIsNone(d.agent_in_command(command), command)

    def test_a_runtime_alone_is_nothing(self):
        self.assertIsNone(d.agent_in_command("node"))
        self.assertIsNone(d.agent_in_command("python3 --version"))


class Foreground(unittest.TestCase):
    def test_a_real_claudes_job_is_one_agent(self):
        # Its helpers share its group, and one of them is named like an agent on purpose.
        rows = [(85804, 85804, 85804, "ttys001", "claude"),
                (85850, 85804, 85804, "ttys001", "npm exec chrome-devtools-mcp@1.11.0"),
                (85919, 85804, 85804, "ttys001", "node /x/.bin/mcp-server"),
                (85950, 85804, 85804, "ttys001", "/x/bin/gemini")]
        found = d.foreground_agents(rows)
        self.assertEqual([(f.pid, f.agent) for f in found], [(85804, "claude")])

    def test_a_wrapper_leading_the_job_doesnt_hide_it(self):
        rows = [(10, 10, 10, "ttys002", "npm exec thing"), (11, 10, 10, "ttys002", "/x/bin/codex")]
        self.assertEqual([f.pid for f in d.foreground_agents(rows)], [11])

    def test_not_running_in_the_background(self):
        self.assertEqual(d.foreground_agents([(10, 10, 99, "ttys002", "codex")]), [])

    def test_not_without_a_terminal(self):
        self.assertEqual(d.foreground_agents([(10, 10, 10, "??", "codex"), (11, 11, 11, "?", "codex")]), [])

    def test_two_terminals_two_agents(self):
        rows = [(10, 10, 10, "ttys002", "codex"), (20, 20, 20, "ttys003", "opencode")]
        self.assertEqual(sorted(f.agent for f in d.foreground_agents(rows)), ["codex", "opencode"])

    def test_ps_lines(self):
        self.assertEqual(d.parse_ps("  12 12 12 ttys001 /x/codex --a b\nnonsense\n13 x 13 ttys1 y\n"),
                         [(12, 12, 12, "ttys001", "/x/codex --a b")])


class States(unittest.TestCase):
    def entry(self, status):
        return {"pid": 1, "sessionId": SID, "status": status, "cwd": "/w"}

    def test_each_state(self):
        s = d.SessionStates()
        self.assertEqual(s.agents([self.entry("idle")])[0]["agent_status"], "idle")
        self.assertEqual(s.agents([self.entry("busy")])[0]["agent_status"], "working")
        self.assertEqual(s.agents([self.entry("waiting")])[0]["agent_status"], "blocked")
        self.assertEqual(s.agents([self.entry("idle")])[0]["agent_status"], "done")
        self.assertEqual(s.agents([self.entry("later")])[0]["agent_status"], "unknown")

    def test_what_its_waiting_for(self):
        s = d.SessionStates()
        s.agents([{**self.entry("waiting"), "waitingFor": "permission prompt"}])
        self.assertIn("permission prompt", s.asking(PANE)[1])
        s.agents([self.entry("busy")])
        self.assertEqual(s.asking(PANE)[1], "Waiting at its terminal")


def report(*blocks):
    return "\n".join(blocks) + "\n"


def session_block(in_herdr="0", **entry):
    return f"@@session {in_herdr}\n" + json.dumps({"pid": 5, "sessionId": SID, "status": "idle", "cwd": "/w/shop", **entry})


def process_block(line, in_herdr="0", cwd="/w/api"):
    return f"@@process {in_herdr} {cwd}\n{line}"


class Reports(unittest.TestCase):
    def test_sessions_and_agents(self):
        entries, found = d.parse_report(report(session_block(), process_block("7 7 7 ttys1 /x/codex --a")))
        self.assertEqual(entries[0]["sessionId"], SID)
        self.assertFalse(entries[0]["in_herdr"])
        self.assertEqual((found[0].pid, found[0].agent, found[0].cwd, found[0].in_herdr), (7, "codex", "/w/api", False))

    def test_in_herdr_is_noted(self):
        entries, found = d.parse_report(report(session_block("1"), process_block("7 7 7 ttys1 codex", in_herdr="1")))
        self.assertTrue(entries[0]["in_herdr"])
        self.assertTrue(found[0].in_herdr)

    def test_tmux_panes_are_matched_by_terminal(self):
        text = "@@tmux /tmp/tmux-5/default /dev/ttys1 %3\n" + report(process_block("7 7 7 ttys1 /x/codex", in_herdr="1"))
        _, found = d.parse_report(text)
        self.assertEqual((found[0].tmux_socket, found[0].tmux_pane, found[0].in_herdr), ("/tmp/tmux-5/default", "%3", False))

    def test_another_terminal_isnt_in_that_pane(self):
        text = "@@tmux /tmp/tmux-5/default /dev/ttys9 %3\n" + report(process_block("7 7 7 ttys1 /x/codex"))
        _, found = d.parse_report(text)
        self.assertEqual(found[0].tmux_pane, "")

    def test_a_tmux_line_in_a_record_cant_add_a_pane(self):
        self.assertEqual(d.tmux_panes("@@tmux relative /dev/t %3\n@@tmux /a b /dev/t %3\n@@tmux /a /dev/t 3\n"), {})

    def test_a_folder_with_spaces(self):
        _, found = d.parse_report(report(process_block("7 7 7 ttys1 codex", cwd="/w/my project")))
        self.assertEqual(found[0].cwd, "/w/my project")

    def test_a_multi_line_record_still_parses(self):
        entries, _ = d.parse_report("@@session 0\n" + json.dumps({"pid": 5, "sessionId": SID}, indent=2) + "\n")
        self.assertEqual(len(entries), 1)

    def test_junk_is_left_out(self):
        for text in ("", "login banner\nmore\n", "@@session 0\n{ half\n", "@@session 0\n[1]\n",
                     '@@session 0\n{"pid": "5", "sessionId": "x"}\n', "@@process 0 /w\nnot a line\n", "@@process"):
            self.assertEqual(d.parse_report(text), ([], []), text)

    def test_a_shell_greeting_before_it_does_no_harm(self):
        entries, _ = d.parse_report("Welcome!\n" + report(session_block()))
        self.assertEqual(len(entries), 1)

    def test_the_script_is_plain_shell_and_sends_no_environment(self):
        self.assertTrue(d.REPORT_SCRIPT.strip().startswith("PATH="))
        check = subprocess.run(["sh", "-n"], input=d.REPORT_SCRIPT.encode(), capture_output=True)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertIn('echo "@@process $(in_herdr "$pid") $cwd"', d.REPORT_SCRIPT)  # the environment is looked at, never sent
        self.assertIn("codex", d.ROUGH_MATCH)

    def test_the_script_runs_with_nothing_there(self):
        done = subprocess.run(["sh", "-c", d.REPORT_SCRIPT], capture_output=True, env={"HOME": "/nonexistent", "PATH": "/usr/bin:/bin"})
        self.assertEqual(done.returncode, 0)


class Merging(unittest.TestCase):
    def merge(self, herdr=(), entries=(), found=()):
        return d.merge_agents(list(herdr), list(entries), list(found), d.SessionStates())

    def test_herdrs_own_stay_as_they_are(self):
        pane = {"pane_id": "w1:p1", "agent": "claude", "agent_status": "working", "agent_session": {"value": SID}}
        self.assertEqual(self.merge([pane], [{"pid": 5, "sessionId": SID}]), [pane])

    def test_a_session_in_a_herdr_pane_isnt_doubled_even_if_herdr_doesnt_name_it(self):
        self.assertEqual(self.merge(entries=[{"pid": 5, "sessionId": SID, "in_herdr": True}]), [])

    def test_a_session_outside_is_added(self):
        self.assertEqual(self.merge(entries=[{"pid": 5, "sessionId": SID}])[0]["pane_id"], PANE)

    def test_claude_found_as_a_process_isnt_shown_twice(self):
        out = self.merge(entries=[{"pid": 5, "sessionId": SID}], found=[d.Found(5, "claude", "t")])
        self.assertEqual([a["pane_id"] for a in out], [PANE])

    def test_a_claude_with_no_record_still_shows_as_running(self):
        out = self.merge(found=[d.Found(5, "claude", "t")])
        self.assertEqual((out[0]["pane_id"], out[0]["agent_status"]), ("p:5", "unknown"))

    def test_an_agent_in_a_herdr_pane_is_left_to_herdr(self):
        self.assertEqual(self.merge(found=[d.Found(7, "codex", "t", in_herdr=True)]), [])


class RunningHere(unittest.TestCase):
    def source(self, rows, env=None, **kw):
        self.rows, self.now, self.scans = rows, 100.0, 0

        def scan():
            self.scans += 1
            return self.rows
        kw.setdefault("panes", lambda: "")
        kw.setdefault("parent_check", lambda pid: False)
        return cs.RunningAgents(pet=Pet(None), scan=scan, env_of=lambda p: env or {}, cwd_of=lambda p: "/w/api", clock=lambda: self.now, **kw)

    def poll(self, source):
        source.last_poll = 0
        with mock.patch.object(source, "_follow"):
            return source.poll()

    def test_tmux_is_found_by_terminal_even_if_the_environment_shows_nothing(self):
        panes = "@@tmux /tmp/tmux-1/default /dev/t %4\n"
        captured = []
        source = self.source([(7, 7, 7, "t", "pi")], env={}, panes=lambda: panes,
                             capture=lambda s, p: captured.append((s, p)) or ("", "x"))
        self.poll(source)
        self.assertEqual(captured, [("/tmp/tmux-1/default", "%4")])

    def test_it_shows_as_running_and_says_it_cant_tell_more(self):
        row = self.poll(self.source([(7, 7, 7, "t", "/x/codex")]))[0]
        self.assertEqual((row["id"], row["a"], row["n"], row["p"], row["s"], row["d"]), ("p:7", "CX", "Codex", "api", "idle", "state unknown"))

    def test_outside_tmux_its_screen_is_never_asked_for(self):
        capture = mock.Mock()
        self.poll(self.source([(7, 7, 7, "t", "codex")], capture=capture))
        capture.assert_not_called()

    def test_a_process_with_both_herdr_and_tmux_is_out_of_herdrs_sight(self):
        source = self.source([(7, 7, 7, "t", "codex")], env={"HERDR_PANE_ID": "w:p1", "TMUX": "/tmp/s,1,0", "TMUX_PANE": "%1"},
                             capture=lambda s, p: ("", "output"))
        source.herdr_up = True
        self.assertEqual(len(self.poll(source)), 1)

    def test_one_that_hides_its_environment_is_placed_by_its_parents(self):
        # pi sets its own process title, and that leaves nothing for ps to show.
        source = self.source([(7, 7, 7, "t", "pi")], env={}, parent_check=lambda pid: True)
        source.herdr_up = True
        self.assertEqual(self.poll(source), [])
        outside = self.source([(7, 7, 7, "t", "pi")], env={}, parent_check=lambda pid: False)
        outside.herdr_up = True
        self.assertEqual(len(self.poll(outside)), 1)

    def test_the_parents_are_asked_once_and_only_with_nothing_else_to_go_on(self):
        check = mock.Mock(return_value=False)
        source = self.source([(7, 7, 7, "t", "pi")], env={}, parent_check=check)
        source.herdr_up = True
        self.poll(source)
        self.poll(source)
        self.assertEqual(check.call_count, 1)
        has_env = mock.Mock()
        self.poll(self.source([(7, 7, 7, "t", "codex")], env={"TERM_PROGRAM": "ghostty"}, parent_check=has_env))
        has_env.assert_not_called()

    def test_nothing_it_says_can_be_answered(self):
        source = self.source([(7, 7, 7, "t", "codex")])
        self.poll(source)
        self.assertFalse(source.act("allow", "p:7", "anything")[0])

    def test_a_claude_with_a_record_is_left_alone(self):
        source = self.source([(7, 7, 7, "t", "claude")])
        source.claude_pids = {7}
        self.assertEqual(self.poll(source), [])

    def test_inside_herdr_is_left_to_herdr(self):
        source = self.source([(7, 7, 7, "t", "codex")], env={"HERDR_PANE_ID": "w:p1"})
        source.herdr_up = True
        self.assertEqual(self.poll(source), [])

    def test_ending_removes_it(self):
        source = self.source([(7, 7, 7, "t", "codex")])
        self.poll(source)
        self.rows, self.now = [], 110.0
        self.assertEqual(self.poll(source), [])

    def test_it_looks_at_every_process_only_now_and_then(self):
        source = self.source([(7, 7, 7, "t", "codex")])
        self.poll(source)
        self.poll(source)
        self.assertEqual(self.scans, 1)
        self.now += cs.SCAN_EVERY_S
        self.poll(source)
        self.assertEqual(self.scans, 2)

    def test_the_pet_doesnt_count_what_it_cant_see_finish(self):
        source = self.source([(7, 7, 7, "t", "codex")])
        self.poll(source)
        before = source.pet_stats.total
        self.rows, self.now = [], 110.0
        self.poll(source)
        self.assertEqual(source.pet_stats.total, before)

    def test_fleet_routes_it_and_tells_it_which_are_claude(self):
        running = self.source([(5, 5, 5, "a", "claude"), (7, 7, 7, "b", "codex")])
        sessions = cs.ClaudeSessions(pet=Pet(None), read=lambda f: [{"pid": 5, "sessionId": SID, "status": "idle", "cwd": "/w/shop"}], env_of=lambda p: {})
        local = HerdrSource(pet=Pet(None))
        fleet = Fleet(local, [], start=False, sessions=sessions, running=running)
        with mock.patch("subprocess.run", side_effect=FileNotFoundError), mock.patch.object(sessions, "_follow"), mock.patch.object(running, "_follow"):
            local.last_poll = sessions.last_poll = running.last_poll = 0
            rows = fleet.poll()
        self.assertEqual(sorted(r["id"] for r in rows), ["p:7", PANE])
        self.assertIs(fleet._route("p:7")[0], running)


class TheTerminal(unittest.TestCase):
    def source(self, **kw):
        return HerdrSource(pet=Pet(None), **kw)

    def test_a_chosen_one_is_used(self):
        self.assertEqual(self.source(focus_app="iTerm").focus_app, "iTerm")

    def test_otherwise_its_where_herdr_is(self):
        with mock.patch("sources.herdr_terminal", return_value="iTerm2") as look:
            source = self.source(focus_app=None)
            self.assertEqual((source.focus_app, source.focus_app), ("iTerm2", "iTerm2"))
        self.assertEqual(look.call_count, 1)  # not looked for every time it's asked

    def test_without_herdr_it_falls_back(self):
        with mock.patch("sources.herdr_terminal", return_value=None):
            self.assertEqual(self.source(focus_app=None).focus_app, "Terminal")

    def test_it_finds_the_client_not_the_server(self):
        table = mock.Mock(stdout=" 10 ??       /x/herdr\n 20 ttys001  /Users/x/.local/bin/herdr\n 30 ttys002  -zsh\n")
        with mock.patch("subprocess.run", return_value=table), mock.patch("sources.app_among_parents", return_value="/Applications/iTerm2.app") as parents:
            self.assertEqual(sources.herdr_terminal(), "iTerm2")
        parents.assert_called_once_with(20)

if __name__ == "__main__":
    unittest.main()
