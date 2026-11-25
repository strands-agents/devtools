"""A local stand-in for the AgentCore command endpoint whose path forgets idle connections.

HTTP/1.1 keep-alive, one thread per connection. A connection that sat idle longer than
``idle_limit`` is treated like a flow a NAT or the server already dropped: the next request on it is
answered with a TCP reset, without being read or run. ``commands`` lists every command that was run
(so a dropped request provably never ran), ``dropped`` counts the requests reset that way.
127.0.0.1 only.
"""

from __future__ import annotations

import binascii
import json
import select
import socket
import struct
import threading
import time
from typing import Any


def _hdr(name: str, value: str) -> bytes:
    n, v = name.encode(), value.encode()
    return bytes([len(n)]) + n + b"\x07" + struct.pack(">H", len(v)) + v


def frame(headers: dict[str, str], payload: bytes) -> bytes:
    h = b"".join(_hdr(k, v) for k, v in headers.items())
    prelude = struct.pack(">II", 12 + len(h) + len(payload) + 4, len(h))
    msg = prelude + struct.pack(">I", binascii.crc32(prelude) & 0xFFFFFFFF) + h + payload
    return msg + struct.pack(">I", binascii.crc32(msg) & 0xFFFFFFFF)


def chunk(payload: dict[str, Any]) -> bytes:
    return frame(
        {":message-type": "event", ":event-type": "chunk", ":content-type": "application/json"},
        json.dumps(payload).encode(),
    )


OK_BODY = chunk({"contentDelta": {"stdout": "ok\n"}}) + chunk(
    {"contentStop": {"exitCode": 0, "status": "COMPLETED"}}
)


class IdleDroppingServer:
    def __init__(self, idle_limit: float, *, delay: float = 0.0) -> None:
        self.idle_limit = idle_limit
        self.delay = delay
        self.commands: list[str] = []
        self.dropped = 0
        self.connections = 0
        self._lock = threading.Lock()
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(128)
        self.url = f"http://127.0.0.1:{self._sock.getsockname()[1]}"
        self._closed = False
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self) -> None:
        self._closed = True
        self._sock.close()

    def _accept(self) -> None:
        while not self._closed:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with self._lock:
                self.connections += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        last = time.monotonic()
        buf = b""
        try:
            while True:
                if not buf:
                    readable, _, _ = select.select([conn], [], [], 30)
                    if not readable:
                        return
                    if not conn.recv(1, socket.MSG_PEEK):
                        return  # the client closed it
                    if time.monotonic() - last > self.idle_limit:
                        # The flow was forgotten while idle: reset, never read the request.
                        with self._lock:
                            self.dropped += 1
                        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                        return
                while b"\r\n\r\n" not in buf:
                    data = conn.recv(65536)
                    if not data:
                        return
                    buf += data
                head, _, buf = buf.partition(b"\r\n\r\n")
                length = 0
                for line in head.split(b"\r\n")[1:]:
                    key, _, value = line.partition(b":")
                    if key.strip().lower() == b"content-length":
                        length = int(value.strip())
                while len(buf) < length:
                    data = conn.recv(65536)
                    if not data:
                        return
                    buf += data
                body, buf = buf[:length], buf[length:]
                with self._lock:
                    self.commands.append(json.loads(body)["command"])
                if self.delay:
                    time.sleep(self.delay)
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/vnd.amazon.eventstream\r\n"
                    + f"Content-Length: {len(OK_BODY)}\r\n\r\n".encode()
                    + OK_BODY
                )
                last = time.monotonic()
        except OSError:
            return
        finally:
            conn.close()
