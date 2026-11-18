"""A server a test starts on this machine, for what it tests to ask."""
import socket
import socketserver
import threading
from http.server import ThreadingHTTPServer


class LocalServer(ThreadingHTTPServer):
    """Started at once, and stopped as soon as it's asked to.

    http.server asks for the machine's full name as it binds, by looking its address up. Where nothing answers
    that, the first server a run starts waits half a minute for it. Nothing here wants the name."""

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def start(self):
        # It looks for being told to stop every 10 ms, not every half second: shutdown() waits for that look.
        threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        return self
