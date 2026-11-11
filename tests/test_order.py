"""How long an agent has been as it is, and which comes first. herdr here is a list in memory."""
import sys
import time
import unittest
from pathlib import Path

DAEMON = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(DAEMON))
from activity import Event  # noqa: E402
from petstate import Pet  # noqa: E402
from sources import HerdrSource, row_order  # noqa: E402


class Logs:
    """What Activity answers, from a table."""

    def __init__(self):
        self.table = {}

    def sync(self, sessions, wanted):
        pass

    def events(self, pane):
        return self.table.get(pane, [])

    def now(self, pane):
        return ""

    def last_said(self, pane):
        return ""


class Source(HerdrSource):
    def __init__(self):
        super().__init__(pet=Pet(None))
        self.activity = Logs()
        self.listed = []

    def _herdr(self, *args):
        return {"result": {"agents": self.listed}}

    def look(self):
        self.last_poll = 0.0
        return {row["id"]: row for row in self.poll()}


def agent(pane, status):
    return {"pane_id": pane, "agent": "claude", "agent_status": status, "cwd": "/work/shop"}


class Ages(unittest.TestCase):
    def test_an_agent_found_at_rest_has_been_so_since_its_log_ended(self):
        source = Source()
        source.listed = [agent("p1", "idle")]
        source.activity.table["p1"] = [Event(time.time() - 3600, 0, "done with it", "said")]
        self.assertAlmostEqual(source.look()["p1"]["age"], 3600, delta=2)

    def test_a_log_read_a_moment_later_still_counts(self):
        source = Source()
        source.listed = [agent("p1", "idle")]
        self.assertLess(source.look()["p1"]["age"], 2)  # nothing read yet
        source.activity.table["p1"] = [Event(time.time() - 600, 0, "done with it", "said")]
        self.assertAlmostEqual(source.look()["p1"]["age"], 600, delta=2)

    def test_an_agent_seen_stopping_counts_from_then(self):
        source = Source()
        source.listed = [agent("p1", "working")]
        source.activity.table["p1"] = [Event(time.time() - 900, 0, "Read cart.ts", "tool")]
        source.look()
        source.listed = [agent("p1", "idle")]
        self.assertLess(source.look()["p1"]["age"], 2)

    def test_an_agent_found_working_counts_from_when_it_was_found(self):
        source = Source()
        source.listed = [agent("p1", "working")]
        source.activity.table["p1"] = [Event(time.time() - 900, 0, "Read cart.ts", "tool")]
        self.assertLess(source.look()["p1"]["age"], 2)

    def test_a_log_from_the_future_isnt_believed(self):
        source = Source()
        source.listed = [agent("p1", "idle")]
        source.activity.table["p1"] = [Event(time.time() + 500, 0, "odd clock", "said")]
        self.assertGreaterEqual(source.look()["p1"]["age"], 0)


class Order(unittest.TestCase):
    def test_what_needs_you_leads_and_equals_go_by_recency(self):
        rows = [{"id": "a", "s": "idle", "age": 400}, {"id": "b", "s": "working", "age": 50}, {"id": "c", "s": "idle", "age": 20},
                {"id": "d", "s": "blocked", "age": 900}, {"id": "e", "s": "working", "age": 5}, {"id": "f", "s": "done", "age": 60}]
        self.assertEqual([row["id"] for row in sorted(rows, key=row_order)], ["d", "f", "e", "b", "c", "a"])

    def test_a_row_without_an_age_still_sorts(self):
        self.assertEqual(row_order({"s": "error"}), (1, 0))


if __name__ == "__main__":
    unittest.main()
