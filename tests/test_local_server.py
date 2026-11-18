"""The server the tests start: nothing a run waits for but what it tests."""
import socket
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler
from unittest import mock

from local_server import LocalServer


class Hello(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"hi")

    def log_message(self, *_):
        pass


class TheTestsServer(unittest.TestCase):
    def test_it_looks_no_name_up_as_it_starts(self):
        """http.server's own asks for the machine's full name: half a minute, where nothing answers that."""
        with mock.patch.object(socket, "getfqdn", side_effect=AssertionError("a name was looked up")):
            server = LocalServer(("127.0.0.1", 0), Hello).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.assertEqual((server.server_name, server.server_port), server.server_address[:2])
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/") as reply:
            self.assertEqual(reply.read(), b"hi")


if __name__ == "__main__":
    unittest.main()
