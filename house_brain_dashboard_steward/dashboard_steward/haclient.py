"""Read-only Home Assistant Core WebSocket client (stdlib RFC 6455, same proven shape as Systems AI's CoreSocket).

Dashboard Steward has no hands. The only commands it can send are in ``ALLOWED`` with exactly those keys;
anything else (``lovelace/config/save``, ``call_service``, ``config/entity_registry/update``, ...) is refused
before a byte is written. Subscriptions are limited to the registry/dashboard change events. Events that
arrive while a command waits for its result are buffered (bounded), not lost.
"""
from __future__ import annotations

import base64
import collections
import json
import os
import socket
import struct
import time

MAX_WS_BYTES = 8 * 1024 * 1024
MAX_EVENT_BUFFER = 2000
# command type -> exact key set (besides "id")
ALLOWED = {
    "config/entity_registry/list": {"type"},
    "config/device_registry/list": {"type"},
    "config/area_registry/list": {"type"},
    "config/floor_registry/list": {"type"},
    "config/label_registry/list": {"type"},
    "lovelace/dashboards/list": {"type"},
    "lovelace/config": {"type", "url_path", "force"},
    "get_states": {"type"},
    "get_config": {"type"},
    "subscribe_events": {"type", "event_type"},
    "ping": {"type"},
}
EVENT_TYPES = frozenset({"entity_registry_updated", "device_registry_updated", "area_registry_updated",
                         "lovelace_updated", "lovelace_dashboards_updated"})


class HaError(Exception):
    def __init__(self, code, detail=""):
        self.code = code
        super().__init__(f"{code}: {detail}")


def check_command(msg: dict) -> None:
    t = msg.get("type")
    if t not in ALLOWED:
        raise HaError("COMMAND_REFUSED", str(t)[:60])
    if set(msg) != ALLOWED[t]:
        raise HaError("COMMAND_SHAPE_REFUSED", t)
    if t == "lovelace/config":
        if msg["force"] is not False or not (msg["url_path"] is None or isinstance(msg["url_path"], str)):
            raise HaError("COMMAND_SHAPE_REFUSED", t)
    if t == "subscribe_events" and msg["event_type"] not in EVENT_TYPES:
        raise HaError("SUBSCRIPTION_REFUSED", str(msg["event_type"])[:60])


class CoreSocket:
    """Real transport: ws://supervisor/core/websocket with the App's Supervisor token."""

    def __init__(self, token: str, host: str = "supervisor", port: int = 80, path: str = "/core/websocket",
                 connect=socket.create_connection):
        self.sock = connect((host, port), 10)
        self.buffer = b""
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = self._until(b"\r\n\r\n", 16384)
        if not head.startswith(b"HTTP/1.1 101"):
            raise HaError("WS_HANDSHAKE")
        if self.recv(20).get("type") != "auth_required":
            raise HaError("WS_HANDSHAKE")
        self.send({"type": "auth", "access_token": token}, _auth=True)
        if self.recv(20).get("type") != "auth_ok":
            raise HaError("WS_AUTH")

    def _read(self, n, deadline):
        while len(self.buffer) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HaError("WS_TIMEOUT")
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(65536)
            if not chunk:
                raise HaError("WS_CLOSED")
            self.buffer += chunk
        data, self.buffer = self.buffer[:n], self.buffer[n:]
        return data

    def _until(self, marker, limit):
        deadline = time.monotonic() + 10
        while marker not in self.buffer:
            if len(self.buffer) > limit:
                raise HaError("WS_HANDSHAKE")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HaError("WS_TIMEOUT")
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(4096)
            if not chunk:
                raise HaError("WS_CLOSED")
            self.buffer += chunk
        head, self.buffer = self.buffer.split(marker, 1)
        return head

    def send(self, message: dict, _auth: bool = False):
        if not _auth:
            check_command({k: v for k, v in message.items() if k != "id"})
        payload = json.dumps(message).encode()
        n = len(payload)
        if n < 126:
            header = bytes([0x81, 0x80 | n])
        elif n < 65536:
            header = bytes([0x81, 0x80 | 126]) + struct.pack("!H", n)
        else:
            header = bytes([0x81, 0x80 | 127]) + struct.pack("!Q", n)
        mask = os.urandom(4)
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def recv(self, timeout):
        deadline = time.monotonic() + timeout
        parts, total = [], 0
        while True:
            b0, b1 = self._read(2, deadline)
            opcode, length = b0 & 0x0F, b1 & 0x7F
            if b1 & 0x80:
                raise HaError("WS_MASKED_FROM_SERVER")
            if length == 126:
                length = struct.unpack("!H", self._read(2, deadline))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read(8, deadline))[0]
            total += length
            if total > MAX_WS_BYTES:
                raise HaError("WS_MESSAGE_TOO_LARGE")
            payload = self._read(length, deadline)
            if opcode == 0x8:
                raise HaError("WS_CLOSED")
            if opcode == 0x9:
                self._control(0xA, payload)
                total -= length
                continue
            if opcode == 0xA:
                total -= length
                continue
            if opcode not in (0x0, 0x1):
                raise HaError("WS_OPCODE")
            parts.append(payload)
            if b0 & 0x80:
                data = json.loads(b"".join(parts).decode("utf-8"))
                if not isinstance(data, dict):
                    raise HaError("WS_SHAPE")
                return data

    def _control(self, opcode, payload):
        mask = os.urandom(4)
        self.sock.sendall(bytes([0x80 | opcode, 0x80 | len(payload)]) + mask
                          + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class Session:
    """Commands + buffered events over any transport with send(dict)/recv(timeout)/close()."""

    def __init__(self, transport):
        self.t = transport
        self.next_id = 1
        self.events = collections.deque(maxlen=MAX_EVENT_BUFFER)
        self.events_dropped = 0
        self.subscriptions = {}

    def command(self, message: dict, timeout: float = 30):
        check_command(message)
        mid = self.next_id
        self.next_id += 1
        self.t.send({"id": mid, **message})
        deadline = time.monotonic() + timeout
        while True:
            data = self.t.recv(max(0.01, deadline - time.monotonic()))
            if data.get("type") == "event":
                self._buffer(data)
                continue
            if data.get("id") == mid and data.get("type") == "result":
                if data.get("success") is not True:
                    err = data.get("error") if isinstance(data.get("error"), dict) else {}
                    raise HaError("WS_COMMAND_FAILED", f"{message['type']} {err.get('code', '')}")
                return data.get("result")

    def _buffer(self, data):
        if len(self.events) == self.events.maxlen:
            self.events_dropped += 1
        ev = data.get("event") if isinstance(data.get("event"), dict) else {}
        self.events.append({"sub": data.get("id"), "event_type": ev.get("event_type"),
                            "data": ev.get("data") if isinstance(ev.get("data"), dict) else {}})

    def subscribe(self, event_type: str):
        mid = self.next_id
        self.command({"type": "subscribe_events", "event_type": event_type})
        self.subscriptions[mid] = event_type
        return mid

    def poll(self, timeout: float) -> list:
        """Wait up to ``timeout`` for events; return everything buffered."""
        deadline = time.monotonic() + timeout
        first = True
        # always read at least once, so a dropped connection is noticed even with a zero timeout
        while not self.events and (first or time.monotonic() < deadline):
            first = False
            try:
                data = self.t.recv(max(0.01, deadline - time.monotonic()))
            except HaError as e:
                if e.code == "WS_TIMEOUT":
                    break
                raise
            except socket.timeout:
                break
            if data.get("type") == "event":
                self._buffer(data)
        out = list(self.events)
        self.events.clear()
        return out

    # read-only convenience wrappers
    def registries(self) -> dict:
        return {"entities": self.command({"type": "config/entity_registry/list"}),
                "devices": self.command({"type": "config/device_registry/list"}),
                "areas": self.command({"type": "config/area_registry/list"}),
                "states": self.command({"type": "get_states"})}

    def dashboards(self) -> list:
        return self.command({"type": "lovelace/dashboards/list"}) or []

    def dashboard_config(self, url_path):
        return self.command({"type": "lovelace/config", "url_path": url_path, "force": False})

    def close(self):
        self.t.close()
