"""A session's host, for tests: what it would send is put where the reader finds it, and what the reader sent
is kept to be looked at. Nothing runs and nothing waits. The frames are tests/fixtures/host_frames.json's."""
import json
import struct
from pathlib import Path

FRAMES = json.loads((Path(__file__).parent / "fixtures" / "host_frames.json").read_text())


def frame(kind: bytes, payload: bytes = b"") -> bytes:
    return kind + struct.pack(">I", len(payload)) + payload


def says(kind: bytes, what) -> bytes:
    return frame(kind, json.dumps(what).encode())


def written(direction: str, name: str) -> bytes:
    return bytes.fromhex(FRAMES[direction][name]["hex"])


def frames_in(data: bytes) -> list[tuple[bytes, bytes]]:
    out = []
    while data:
        size = struct.unpack(">I", data[1:5])[0]
        out.append((data[:1], data[5:5 + size]))
        data = data[5 + size:]
    return out


class FakeHost:
    """The reader's end of a host's socket. `answers` are handed over one to a q, as they're asked for; with
    `late`, an answer isn't there to read until `arrives` says so. Waiting on it takes no time: `waited` is told
    how long a wait would have been, for the test's clock to move by."""

    def __init__(self, *answers: bytes, late: bool = False, waited=None):
        self.answers, self.late, self.waited = list(answers), late, waited
        self.sent = b""          # everything the reader wrote
        self.waiting = b""       # what the reader hasn't read yet
        self.held = b""          # answers that aren't there yet
        self.waits: list[float] = []
        self.closed = self.gone = False
        self.timeout = None

    def settimeout(self, seconds):
        self.timeout = seconds

    def sendall(self, data: bytes):
        if self.gone:
            raise BrokenPipeError
        self.sent += data
        for kind, _ in frames_in(data):
            if kind == b"h":
                self.waiting += written("from_host", "hello_back")
            elif kind == b"q" and self.answers:
                if self.late:
                    self.held += self.answers.pop(0)
                else:
                    self.waiting += self.answers.pop(0)

    def recv(self, most: int) -> bytes:
        self.waits.append(self.timeout)
        if self.gone:
            return b""
        if not self.waiting:
            if self.waited and self.timeout:
                self.waited(self.timeout)
            raise TimeoutError
        data, self.waiting = self.waiting[:most], self.waiting[most:]
        return data

    def arrives(self):
        self.waiting, self.held = self.waiting + self.held, b""

    def close(self):
        self.closed = True

    def kinds_sent(self) -> list[bytes]:
        return [kind for kind, _ in frames_in(self.sent)]
