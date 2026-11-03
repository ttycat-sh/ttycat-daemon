"""The port of herdr's detection engine, against answers recorded from herdr itself."""
import base64
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

DAEMON = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DAEMON))
import claude_sessions as cs  # noqa: E402
import discovery as d  # noqa: E402
import screen_rules as sr  # noqa: E402
from petstate import Pet  # noqa: E402

RULES = sr.ScreenRules()
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "screen_rules_from_herdr.json").read_text())


class Engine(unittest.TestCase):
    def test_every_rule_compiles(self):
        data = json.loads(sr.RULES.read_text())
        self.assertEqual(sorted(RULES.agents), sorted(data["agents"]))
        self.assertEqual(sum(len(v) for v in RULES.agents.values()), sum(len(a["rules"]) for a in data["agents"].values()))
        self.assertGreater(len(RULES.agents), 20)

    def test_it_says_what_herdr_said(self):
        for case in FIXTURE["screens"]:
            found = RULES.detect(case["agent"], case["screen"])
            self.assertEqual((found.state, found.rule), (case["state"], case["rule"]), f'{case["agent"]}: {case["screen"]!r}')

    def test_the_recording_covers_a_good_part_of_the_rules(self):
        self.assertGreater(len({(c["agent"], c["rule"]) for c in FIXTURE["screens"] if c["rule"]}), 90)

    def test_no_match_is_idle_as_in_herdr(self):
        self.assertEqual(RULES.detect("codex", "nothing of note\n").state, "idle")

    def test_an_agent_without_rules_is_unknown_not_idle(self):
        self.assertEqual(RULES.detect("aider", "whatever").state, "unknown")
        self.assertFalse(RULES.knows("aider"))

    def test_titles(self):  # herdr's explain takes a file, not a title, so these are from the rules themselves
        self.assertEqual(RULES.detect("codex", "", "Action Required").state, "blocked")
        self.assertEqual(RULES.detect("codex", "", "⠋ working on it").state, "working")
        self.assertEqual(RULES.detect("claude", "", "⠋ Doing a thing").state, "working")
        self.assertEqual(RULES.detect("claude", "", "◐ Doing a thing").state, "working")

    def test_no_rules_file_means_no_guessing(self):
        none = sr.ScreenRules(Path("/nonexistent"))
        self.assertFalse(none.knows("codex"))
        self.assertEqual(none.detect("codex", "x").state, "unknown")

    def test_it_runs_on_the_python_a_mac_comes_with(self):
        python = "/usr/bin/python3"
        if not Path(python).exists():
            self.skipTest("no system Python")
        code = ("import sys; sys.path.insert(0, %r); import screen_rules as s; "
                "r = s.ScreenRules(); print(len(r.agents), r.detect('codex', '', 'Action Required').state)" % str(DAEMON))
        done = subprocess.run([python, "-I", "-c", code], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(done.stdout.strip().endswith("blocked"))


class Regions(unittest.TestCase):
    def test_bottom_lines_count_only_lines_with_something_on(self):
        self.assertEqual(sr.region_of("bottom_non_empty_lines(2)", "a\n\nb\n\nc\n\n"), "b\n\nc\n\n")

    def test_top_lines(self):
        self.assertEqual(sr.region_of("top_non_empty_lines(2)", "\na\nb\nc\n"), "\na\nb\n")

    def test_after_the_last_rule(self):
        self.assertEqual(sr.region_of("after_last_horizontal_rule", "a\n────\nb\n────\nc\n"), "c\n")

    def test_the_box_between_the_last_two_rules(self):
        self.assertEqual(sr.region_of("prompt_box_body", "x\n────\ntyped\n────\nfoot\n"), "typed\n")
        self.assertEqual(sr.region_of("prompt_box_body", "x\n"), "")

    def test_above_the_box(self):
        self.assertEqual(sr.region_of("last_non_empty_above_prompt_box", "a\nb\n\n────\ntyped\n────\n"), "b")

    def test_the_title_region_isnt_the_screen(self):
        self.assertEqual(sr.region_of("osc_title", "screen", "title"), "title")

    def test_prompt_markers(self):
        self.assertEqual(sr.region_of("after_last_prompt_marker", "a\n› x\nb\n"), "b\n")
        self.assertEqual(sr.region_of("whole_recent_without_current_prompt_marker", "a\n› x\n"), "")
        self.assertEqual(sr.region_of("whole_recent_without_current_prompt_marker", "› x\n• done\n"), "› x\n• done\n")

    def test_an_unknown_region_is_empty(self):
        self.assertEqual(sr.region_of("nonsense", "a"), "")


class FakeRules:
    def __init__(self, *answers):
        self.answers = list(answers)

    def knows(self, agent):
        return agent != "aider"

    def detect(self, agent, screen, title=""):
        return self.answers.pop(0)


def seen(state, **kw):
    return sr.Detection(state, **kw)


class Steadying(unittest.TestCase):
    def run_through(self, *answers):
        states = d.ScreenStates(FakeRules(*answers))
        return [states.update("p:1", "codex", "") for _ in answers]

    def test_a_moment_without_work_on_screen_isnt_finishing(self):
        self.assertEqual(self.run_through(seen("working"), seen("idle"), seen("working")), ["working", "working", "working"])

    def test_staying_idle_is_finishing(self):
        self.assertEqual(self.run_through(seen("working"), seen("idle"), seen("idle")), ["working", "working", "done"])

    def test_plainly_ready_is_believed_at_once(self):
        self.assertEqual(self.run_through(seen("working"), seen("idle", visible_idle=True)), ["working", "done"])

    def test_a_prompt_is_believed_at_once(self):
        self.assertEqual(self.run_through(seen("working"), seen("blocked")), ["working", "blocked"])

    def test_idle_from_the_start_is_just_idle(self):
        self.assertEqual(self.run_through(seen("idle"), seen("idle")), ["idle", "idle"])

    def test_a_viewer_over_it_says_nothing(self):
        self.assertEqual(self.run_through(seen("working"), seen("unknown", skip_state_update=True)), ["working", "working"])

    def test_an_agent_without_rules(self):
        states = d.ScreenStates(FakeRules())
        self.assertEqual(states.update("p:1", "aider", "x"), "unknown")

    def test_ended_ones_are_forgotten(self):
        states = d.ScreenStates(FakeRules(seen("working")))
        states.update("p:1", "codex", "")
        states.forget_others(set())
        self.assertEqual((states.last, states.ran), ({}, set()))


TMUX = {"TMUX": "/tmp/tmux-1/default,5,0", "TMUX_PANE": "%3"}
CODEX_ASKING = "\n".join(["• Running a thing", "", "  $ touch a", "", "Would you like to run the following command?",
                          "› 1. Yes, proceed (y)", "  2. No (esc)", "Press enter to confirm or esc to cancel"]) + "\n"


class InTmuxHere(unittest.TestCase):
    def source(self, env=TMUX, screen="nothing\n", title="", agent_line="/x/codex"):
        self.screen, self.title = screen, title
        self.captured = []

        def capture(socket, pane):
            self.captured.append((socket, pane))
            return (self.title, self.screen) if self.screen is not None else None
        lines = ""
        if env.get("TMUX_PANE"):
            lines = f"@@tmux {env['TMUX'].split(',')[0]} /dev/t {env['TMUX_PANE']}\n"
        return cs.RunningAgents(pet=Pet(None), scan=lambda: [(7, 7, 7, "t", agent_line)], env_of=lambda p: env, cwd_of=lambda p: "/w/api",
                                capture=capture, rules=RULES, panes=lambda: lines, parent_check=lambda pid: False)

    def poll(self, source):
        source.last_poll = 0
        with mock.patch.object(source, "_follow"):
            return source.poll()[0]

    def test_it_asks_the_right_server_for_the_right_pane(self):
        self.poll(self.source())
        self.assertEqual(self.captured, [("/tmp/tmux-1/default", "%3")])

    def test_working(self):
        row = self.poll(self.source(screen="x\n", title="⠋ Thinking"))
        self.assertEqual(row["s"], "working")

    def test_finishing_counts_for_the_pet_once_its_sure(self):
        source = self.source(title="⠋ Thinking")
        self.poll(source)
        before = source.pet_stats.total
        self.title = ""
        self.assertEqual(self.poll(source)["s"], "working")  # one look isn't enough
        self.assertEqual(self.poll(source)["s"], "done")
        self.assertEqual(source.pet_stats.total, before + 1)

    def test_a_prompt_is_shown_but_cant_be_answered(self):
        source = self.source(screen=CODEX_ASKING)
        row = self.poll(source)
        if row["s"] != "blocked":
            self.fail(f"not blocked: {row}")
        self.assertEqual(row["ask"], "terminal")
        self.assertIn("terminal", row["note"])
        self.assertFalse(source.act("allow", "p:7", row["rev"])[0])

    def test_a_pane_that_cant_be_read_is_unknown(self):
        row = self.poll(self.source(screen=None))
        self.assertEqual((row["s"], row["d"]), ("idle", "state unknown"))

    def test_an_unsafe_place_is_never_asked_for(self):
        for bad in ("/tmp/a b", "-x", "relative"):
            self.assertFalse(d.tmux_place(d.Found(1, "codex", "t", tmux_socket=bad, tmux_pane="%3")), bad)
        for bad in ("%3;rm", "3", "%", ""):
            self.assertFalse(d.tmux_place(d.Found(1, "codex", "t", tmux_socket="/tmp/s", tmux_pane=bad)), bad)

    def test_an_agent_without_rules_isnt_read(self):
        source = self.source(agent_line="/x/aider")
        self.assertEqual(self.poll(source)["d"], "state unknown")
        self.assertEqual(self.captured, [])

    def test_it_looks_at_the_screen_more_often_than_at_all_processes(self):
        source = self.source()
        self.poll(source)
        self.poll(source)
        self.assertEqual(len(self.captured), 2)


def enc(text):
    return base64.b64encode(text.encode()).decode()


if __name__ == "__main__":
    unittest.main()
