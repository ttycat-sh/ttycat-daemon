"""Reading each day's tokens from what agents record."""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from token_days import TokenDays  # noqa: E402

DAY = "2026-03-10"
NOON = time.mktime(time.strptime(DAY + " 12", "%Y-%m-%d %H"))


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))


def claude_line(mid, ts=NOON, i=100, o=50, cache_read=999_999, cache_write=999, model="claude-x"):
    usage = {"input_tokens": i, "output_tokens": o, "cache_read_input_tokens": cache_read, "cache_creation_input_tokens": cache_write}
    return {"type": "assistant", "timestamp": iso(ts), "requestId": "r", "message": {"id": mid, "model": model, "usage": usage}}


class Tokens(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def write(self, rel, lines, mode="w"):
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, mode) as f:
            f.write("".join(json.dumps(x) + "\n" for x in lines))
        return path

    def scan(self, since=DAY):
        return TokenDays(since, home=str(self.home)).scan(NOON)

    def test_in_and_out_count_and_cache_does_not(self):
        self.write(".claude/projects/p/a.jsonl", [claude_line("m1")])
        self.assertEqual(self.scan(), {DAY: 150})

    def test_a_reply_written_twice_or_copied_counts_once(self):
        self.write(".claude/projects/p/a.jsonl", [claude_line("m1", o=10), claude_line("m1", o=50)])
        self.write(".claude/projects/p/resumed.jsonl", [claude_line("m1", o=50)])
        self.assertEqual(self.scan(), {DAY: 150})

    def test_scanning_again_only_adds_what_is_new(self):
        path = self.write(".claude/projects/p/a.jsonl", [claude_line("m1")])
        self.assertEqual(self.scan(), {DAY: 150})
        self.assertEqual(self.scan(), {DAY: 150})
        self.write(".claude/projects/p/a.jsonl", [claude_line("m2")], mode="a")
        self.assertEqual(self.scan(), {DAY: 300})
        self.assertTrue(path.exists())

    def test_deleted_logs_do_not_shrink_a_day(self):
        path = self.write(".claude/projects/p/a.jsonl", [claude_line("m1")])
        self.assertEqual(self.scan(), {DAY: 150})
        path.unlink()
        self.assertEqual(self.scan(), {DAY: 150})

    def test_days_before_the_first_that_matters_are_left_out(self):
        self.write(".claude/projects/p/a.jsonl", [claude_line("m1", ts=NOON - 86400), claude_line("m2")])
        self.assertEqual(self.scan(), {DAY: 150})

    def test_old_logs_are_not_opened(self):
        path = self.write(".claude/projects/p/old.jsonl", [claude_line("m1")])
        os.utime(path, (NOON - 3 * 86400, NOON - 3 * 86400))
        scan = TokenDays(DAY, home=str(self.home))
        scan.scan(NOON)
        self.assertNotIn(str(path), scan.c["files"])

    def test_a_line_still_being_written_waits(self):
        path = self.write(".claude/projects/p/a.jsonl", [claude_line("m1")])
        with open(path, "a") as f:
            f.write(json.dumps(claude_line("m2"))[:40])
        self.assertEqual(self.scan(), {DAY: 150})

    def test_codex_keeps_a_running_total(self):
        def count(i, cached, o):
            info = {"total_token_usage": {"input_tokens": i, "cached_input_tokens": cached, "output_tokens": o}}
            return {"type": "event_msg", "timestamp": iso(NOON), "payload": {"type": "token_count", "info": info}}
        self.write(".codex/sessions/2026/rollout-a.jsonl", [
            {"type": "session_meta", "timestamp": iso(NOON), "payload": {"id": "s1"}},
            count(100, 40, 10), count(100, 40, 10), count(300, 100, 30)])
        self.assertEqual(self.scan(), {DAY: 230})  # (300 - 100) in, 30 out

    def test_gemini_and_pi(self):
        self.write(".pi/agent/sessions/x/a.jsonl", [
            {"type": "message", "id": "1", "timestamp": iso(NOON),
             "message": {"role": "assistant", "usage": {"input": 5, "output": 6, "cacheRead": 700}}}])
        path = self.home / ".gemini/tmp/x/chats/c.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"sessionId": "g", "messages": [
            {"id": "1", "timestamp": iso(NOON), "tokens": {"input": 30, "cached": 10, "output": 4, "thoughts": 1}}]}))
        self.assertEqual(self.scan(), {DAY: 11 + 25})

    def test_nonsense_counts_for_nothing(self):
        path = self.home / ".claude/projects/p/a.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text('not json "usage"\n{"type":"assistant","usage":1}\n')
        self.assertEqual(self.scan(), {})

    def test_nothing_is_kept_but_numbers_and_positions(self):
        self.write(".claude/projects/secret-project/a.jsonl", [claude_line("m1")])
        self.scan()
        kept = json.loads((self.home / ".ttycat" / "token-days.json").read_text())
        self.assertEqual(kept["days"], {DAY: 150})
        self.assertNotIn("claude-x", json.dumps(kept))


if __name__ == "__main__":
    unittest.main()
