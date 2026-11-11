"""Asking the server what models cost: only when something wants it, only what a table can be. Against a server in the test."""
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import plugins
import prices

TABLE = {"fetched_at": "2026-11-11T06:00:00Z", "currency": "USD", "per": 1_000_000, "models": [
    {"provider": "anthropic", "model": "claude-sonnet-4-5", "input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75, "source": "litellm"},
    {"provider": "openai", "model": "gpt-4", "input": 30, "output": 60, "source": "litellm"},
]}


class Server:
    """Answers /v1/model-insights/prices with `table`, and 304 to its own ETag."""

    def __init__(self):
        self.table = TABLE
        self.status = 200
        self.asked: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.asked.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}})
                body = json.dumps(outer.table).encode() if not isinstance(outer.table, bytes) else outer.table
                etag = f'"{len(body)}-{hash(body) & 0xffff}"'
                if outer.status != 200:
                    self.send_response(outer.status)
                    self.end_headers()
                elif self.headers.get("If-None-Match") == etag:
                    self.send_response(304)
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.send_header("ETag", etag)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            def log_message(self, *_):
                pass

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.http.server_port}/v1/model-insights/prices"

    def close(self):
        self.http.shutdown()
        self.http.server_close()


class PricesTest(unittest.TestCase):
    def setUp(self):
        self.server = Server()
        self.addCleanup(self.server.close)
        self.home = Path(tempfile.mkdtemp())
        self.on = True
        self.now = 1_800_000_000.0
        self.logged: list = []

    def prices(self, **kw) -> prices.Prices:
        kw.setdefault("url", self.server.url)
        return prices.Prices(lambda: self.on, home=self.home, log=lambda *a: self.logged.append(a), build={}, clock=lambda: self.now, **kw)

    def wanted(self, p: prices.Prices):
        """Someone wants the table, and whatever asking that started is done."""
        table = p.table()
        p.wait()
        return table

    def test_the_first_time_theres_none_and_then_theres_the_servers(self):
        p = self.prices()
        self.assertIsNone(self.wanted(p))
        table = p.table()
        self.assertEqual(table["fetched_at"], "2026-11-11T06:00:00Z")
        self.assertEqual(table["models"][0], {"provider": "anthropic", "model": "claude-sonnet-4-5", "input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75})
        self.assertEqual(table["models"][1], {"provider": "openai", "model": "gpt-4", "input": 30, "output": 60})

    def test_nothing_is_asked_until_something_wants_the_table(self):
        self.prices()
        self.assertEqual(self.server.asked, [])

    def test_nothing_about_this_mac_is_sent(self):
        self.wanted(self.prices())
        asked = self.server.asked[0]
        self.assertEqual(asked["path"], "/v1/model-insights/prices")  # no query: no install ID, no model
        self.assertEqual(sorted(asked["headers"]), ["accept-encoding", "connection", "host", "user-agent"])
        self.assertEqual(asked["headers"]["user-agent"], "ttycat-daemon")

    def test_with_the_update_check_off_nothing_is_asked(self):
        self.on = False
        p = self.prices()
        self.assertIsNone(self.wanted(p))
        self.assertEqual(self.server.asked, [])
        # What was fetched while it was on is still there to use.
        self.on = True
        self.wanted(p)
        self.on = False
        self.now += 10 * prices.EVERY_S
        self.assertIsNotNone(self.wanted(self.prices()))
        self.assertEqual(len(self.server.asked), 1)

    def test_its_asked_for_once_a_day_however_often_its_wanted(self):
        p = self.prices()
        for _ in range(5):
            self.wanted(p)
            self.now += 3600
        self.assertEqual(len(self.server.asked), 1)
        self.now += prices.EVERY_S
        self.wanted(p)
        self.assertEqual(len(self.server.asked), 2)

    def test_the_day_is_remembered_across_a_restart(self):
        self.wanted(self.prices())
        self.now += 3600
        again = self.prices()
        self.assertIsNotNone(self.wanted(again))  # from the file, at once
        self.assertEqual(len(self.server.asked), 1)

    def test_a_table_that_hasnt_changed_isnt_sent_again(self):
        p = self.prices()
        self.wanted(p)
        self.now += prices.EVERY_S
        self.wanted(p)
        self.assertIn("if-none-match", self.server.asked[1]["headers"])
        self.assertEqual(p.table()["models"][1]["model"], "gpt-4")
        self.now += prices.EVERY_S - 60
        self.wanted(p)
        self.assertEqual(len(self.server.asked), 2)  # the 304 counted as asked

    def test_a_new_table_replaces_the_old(self):
        p = self.prices()
        self.wanted(p)
        self.server.table = {**TABLE, "models": [{"provider": "openai", "model": "gpt-4", "input": 30, "output": 61}]}
        self.now += prices.EVERY_S
        self.wanted(p)
        self.assertEqual(p.table()["models"], [{"provider": "openai", "model": "gpt-4", "input": 30, "output": 61}])

    def test_an_answer_that_isnt_a_table_changes_nothing_and_is_tried_again_in_an_hour(self):
        p = self.prices()
        self.wanted(p)
        for bad in (b"<html>busy</html>", {"error": "try again later"}, {**TABLE, "currency": "EUR"}, {**TABLE, "per": 1000},
                    {**TABLE, "models": []}, {**TABLE, "models": "none"}, b"x" * (prices.MAX_ANSWER + 1)):
            self.server.table = bad
            self.now += prices.EVERY_S
            self.wanted(p)
            self.assertEqual(p.table()["models"][0]["model"], "claude-sonnet-4-5", bad if isinstance(bad, dict) else bad[:20])
            self.assertTrue(p.info()["error"])
        asked = len(self.server.asked)
        self.now += prices.AGAIN_S - 60
        self.wanted(p)
        self.assertEqual(len(self.server.asked), asked)
        self.now += 60
        self.server.table = TABLE
        self.wanted(p)
        self.assertEqual(len(self.server.asked), asked + 1)
        self.assertEqual(p.info()["error"], "")

    def test_a_server_thats_down_changes_nothing(self):
        p = self.prices()
        self.wanted(p)
        self.server.status = 503
        self.now += prices.EVERY_S
        self.assertIsNotNone(self.wanted(p))
        self.assertIsNotNone(p.table())
        self.assertIn("503", p.info()["error"])

    def test_only_names_and_prices_get_through(self):
        table = prices.read_table({**TABLE, "note": "<script>", "models": [
            {"provider": "a", "model": "ok", "input": 1, "output": 2, "cache_read": "free", "extra": {"x": 1}},
            {"provider": "a", "model": "negative", "input": -1, "output": 2},
            {"provider": "a", "model": "absurd", "input": 1, "output": 1e9},
            {"provider": "a", "model": "not-a-number", "input": "1", "output": 2},
            {"provider": "a", "model": "nan", "input": float("nan"), "output": 2},
            {"provider": "a", "model": "true", "input": True, "output": 2},
            {"provider": "a", "model": "x" * 200, "input": 1, "output": 2},
            {"provider": "a", "model": "line\nbreak", "input": 1, "output": 2},
            {"provider": "", "model": "nobody's", "input": 1, "output": 2},
            {"provider": "a", "input": 1, "output": 2},
            "not a model",
        ]})
        self.assertEqual(table, {"fetched_at": "2026-11-11T06:00:00Z", "models": [{"provider": "a", "model": "ok", "input": 1, "output": 2}]})

    def test_a_release_asks_the_server_it_was_built_for_and_never_over_plain_http(self):
        dev = prices.Prices(lambda: True, home=self.home, build={"release": True, "updates": "https://api.dev.ttycat.sh/v1/updates"})
        self.assertEqual(dev.url, "https://api.dev.ttycat.sh/v1/model-insights/prices")
        self.assertEqual(prices.Prices(lambda: True, home=self.home, build={"release": True}).url, prices.PRICES_URL)
        plain = prices.Prices(lambda: True, home=self.home, log=lambda *a: None, clock=lambda: self.now,
                              build={"release": True, "updates": self.server.url})
        self.assertIsNone(self.wanted(plain))
        self.assertIn("isn't https", plain.info()["error"])
        self.assertEqual(self.server.asked, [])

    def test_a_file_someone_damaged_is_as_good_as_none(self):
        (self.home / "prices.json").write_text('{"etag": "x", "asked": "soon", "table": {"models": 7}}')
        p = self.prices()
        self.assertIsNone(self.wanted(p))
        self.assertIsNotNone(p.table())


class PluginTest(unittest.TestCase):
    def test_an_extension_has_no_prices_until_the_daemon_gives_it_them(self):
        api = plugins.Api()
        self.assertEqual(api.version, 2)
        self.assertIsNone(api.prices())


if __name__ == "__main__":
    unittest.main()
