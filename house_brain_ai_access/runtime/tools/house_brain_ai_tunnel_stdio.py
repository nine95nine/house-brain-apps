#!/usr/bin/env python3
"""Private, read-only Secure MCP Tunnel child; no inbound network listener.

The upstream tunnel association is the private developer perimeter, not a public
OAuth verifier. Credentials never come from RPC arguments. The accepted catalog,
handlers and Broker authorization boundary remain separate sources of truth.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import os
import re
import ssl
import stat
import sys
from typing import Final

from tools.build_house_brain_ai_snapshot_catalog import CatalogError, build_catalog
from tools.house_brain_ai_broker_boundary import (
    MAX_CHUNKS, MAX_CONCURRENT_READS, MAX_RESPONSE_BYTES, ReadGrant,
    ReadReply, ReadRequest, SnapshotReadBoundary, SNAPSHOT_URL,
)
from tools.house_brain_ai_mcp_core import HouseBrainMcpCore, McpError, decode_rpc, response_id
from tools.ai_kill_switch_enforcement import guard as kill_switch_guard
from tools.ai_kill_switch_store import KillSwitchStore

BROKER_HOST: Final = "house-brain-maintenance-broker.nine95nine.workers.dev"
BROKER_PATH: Final = "/v1/snapshot"
MAX_HEADER_BYTES: Final = 8192
MAX_LINE_BYTES: Final = 32768
CLOSE_TIMEOUT_SECONDS: Final = 0.25
OUTPUT_TIMEOUT_SECONDS: Final = 1.0
ALLOWED_SCOPES: Final = frozenset({
    "engineering.read.status", "engineering.read.platform", "engineering.read.backup",
})
KILL_SWITCH_SURFACE: Final = "house_brain_ai_access"
# The App has no Request Guard runtime yet (#20/#58); these are the interlock inputs for an
# enabled, healthy read surface. The kill switch can only remove permission from them.
INTERLOCK_PACKET: Final = {"schema_version": "house_brain_ai_safety_interlock.input.v1", "mode": "ENABLED",
                           "dependency_health": "HEALTHY", "request_guard_action": "ALLOW",
                           "owner_ack_required": False}
ID_RE: Final = re.compile(r"[A-Za-z0-9_.:-]{1,96}")
KEY_RE: Final = re.compile(r"[A-Za-z0-9._~+/-]{20,4096}={0,2}", re.ASCII)
HEADER_RE: Final = re.compile(rb"[A-Za-z0-9-]{1,64}")
CHUNK_RE: Final = re.compile(rb"[0-9A-Fa-f]{1,8}\r\n")


class RuntimeConfigError(ValueError):
    """Only constant reason codes; no credentials, paths or remote values."""


async def _close(writer):
    writer.close()
    try:
        async with asyncio.timeout(CLOSE_TIMEOUT_SECONDS):
            await writer.wait_closed()
    except (TimeoutError, OSError, asyncio.CancelledError) as exc:
        # SSL close_notify is not allowed to extend cleanup indefinitely.
        transport = getattr(writer, "transport", None)
        if transport is not None:
            transport.abort()
        if isinstance(exc, asyncio.CancelledError):
            raise


class AsyncioHttpsBrokerTransport:
    def __init__(self, read_key: str):
        if (type(read_key) is not str or not 20 <= len(read_key) <= 4096
                or KEY_RE.fullmatch(read_key) is None):
            raise RuntimeConfigError("BROKER_READ_KEY_INVALID")
        self._key = read_key
        self._ssl = ssl.create_default_context()
        # Preserve stricter platform defaults; never allow an implicit lower floor.
        if self._ssl.minimum_version < ssl.TLSVersion.TLSv1_2:
            self._ssl.minimum_version = ssl.TLSVersion.TLSv1_2

    @asynccontextmanager
    async def open(self, request: ReadRequest):
        if type(request) is not ReadRequest or request != ReadRequest():
            raise RuntimeConfigError("BROKER_REQUEST_INVALID")
        writer = None
        try:
            reader, writer = await asyncio.open_connection(
                BROKER_HOST, 443, ssl=self._ssl, server_hostname=BROKER_HOST,
                limit=MAX_HEADER_BYTES,
            )
            wire = (
                f"GET {BROKER_PATH} HTTP/1.1\r\nHost: {BROKER_HOST}\r\n"
                f"Accept: application/json\r\nAuthorization: Bearer {self._key}\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            writer.write(wire)
            await writer.drain()
            head = await reader.readuntil(b"\r\n\r\n")
            if len(head) > MAX_HEADER_BYTES:
                raise RuntimeConfigError("BROKER_HEADERS_TOO_LARGE")
            lines = head[:-4].split(b"\r\n")
            if re.fullmatch(rb"HTTP/1\.1 [1-5][0-9]{2} [\x20-\x7e]*", lines[0]) is None:
                raise RuntimeConfigError("BROKER_STATUS_INVALID")
            status = int(lines[0][9:12])
            if len(lines) > 65:
                raise RuntimeConfigError("BROKER_HEADERS_TOO_LARGE")
            headers = []
            seen = set()
            chunked = False
            content_length = None
            for raw in lines[1:]:
                name, separator, value = raw.partition(b":")
                value = value.strip(b" \t")
                if (not separator or HEADER_RE.fullmatch(name) is None
                        or len(value) > 2048 or any(c < 32 or c > 126 for c in value)):
                    raise RuntimeConfigError("BROKER_HEADER_INVALID")
                key = name.decode("ascii").lower()
                if key in seen:
                    raise RuntimeConfigError("BROKER_DUPLICATE_HEADER")
                seen.add(key)
                headers.append((name.decode("ascii"), value.decode("ascii")))
                if key == "transfer-encoding":
                    if value.lower() != b"chunked":
                        raise RuntimeConfigError("BROKER_ENCODING_INVALID")
                    chunked = True
                elif key == "content-length":
                    if re.fullmatch(rb"[0-9]{1,6}", value) is None:
                        raise RuntimeConfigError("BROKER_LENGTH_INVALID")
                    content_length = int(value)
                    if content_length > MAX_RESPONSE_BYTES:
                        raise RuntimeConfigError("BROKER_LENGTH_INVALID")
            if chunked and content_length is not None:
                raise RuntimeConfigError("BROKER_FRAMING_AMBIGUOUS")

            async def body():
                total = 0
                count = 0
                if chunked:
                    while True:
                        line = await reader.readline()
                        # Deliberately limited Broker profile: no chunk extensions/trailers.
                        if CHUNK_RE.fullmatch(line) is None:
                            raise RuntimeConfigError("BROKER_CHUNK_INVALID")
                        size = int(line[:-2], 16)
                        if size == 0:
                            if await reader.readline() != b"\r\n":
                                raise RuntimeConfigError("BROKER_TRAILER_REJECTED")
                            return
                        count += 1
                        if count > MAX_CHUNKS or size > MAX_RESPONSE_BYTES - total:
                            raise RuntimeConfigError("BROKER_BODY_TOO_LARGE")
                        # Bound the allocation BEFORE readexactly, not after yielding.
                        data = await reader.readexactly(size)
                        if await reader.readexactly(2) != b"\r\n":
                            raise RuntimeConfigError("BROKER_CHUNK_INVALID")
                        total += size
                        yield data
                else:
                    while content_length is None or total < content_length:
                        remaining = (MAX_RESPONSE_BYTES - total + 1 if content_length is None
                                     else content_length - total)
                        data = await reader.read(min(16384, remaining))
                        if not data:
                            if content_length is not None and total != content_length:
                                raise RuntimeConfigError("BROKER_BODY_TRUNCATED")
                            return
                        total += len(data)
                        count += 1
                        if total > MAX_RESPONSE_BYTES or count > MAX_CHUNKS:
                            raise RuntimeConfigError("BROKER_BODY_TOO_LARGE")
                        yield data
            yield ReadReply(status, SNAPSHOT_URL, tuple(headers), body())
        finally:
            if writer is not None:
                await _close(writer)


class TunnelGrantProvider:
    def __init__(self, principal: str, scopes: frozenset[str]):
        if type(principal) is not str or ID_RE.fullmatch(principal) is None:
            raise RuntimeConfigError("PRINCIPAL_INVALID")
        if type(scopes) is not frozenset or not scopes or not scopes <= ALLOWED_SCOPES:
            raise RuntimeConfigError("SCOPES_INVALID")
        self._principal = principal
        self._scopes = scopes

    def __call__(self):
        return ReadGrant(self._principal, "secure-mcp-tunnel-v1",
                         datetime.now(timezone.utc) + timedelta(seconds=30), self._scopes, True)


def _config():
    return (os.environ.get("MAINTENANCE_READ_KEY", ""),
            os.environ.get("HOUSE_BRAIN_TUNNEL_PRINCIPAL", ""),
            frozenset(x.strip() for x in os.environ.get("HOUSE_BRAIN_TUNNEL_SCOPES", "").split(",") if x.strip()))


def kill_switch_read_gate(view_dir: str, clock=lambda: datetime.now(timezone.utc)):
    """#56 READ gate over the wrapper-published, read-only store view. Empty path = FULL_STOP."""
    store = KillSwitchStore(view_dir) if view_dir else None

    def gate():
        if store is None:
            return {"decision": "BLOCK", "reason_codes": ["KILL_SWITCH_VIEW_NOT_CONFIGURED"]}
        now = clock().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return kill_switch_guard(store=store, now=now, surface_id=KILL_SWITCH_SURFACE,
                                 action_class="READ", interlock_packet=dict(INTERLOCK_PACKET))
    return gate


def build_core():
    read_key, principal, scopes = _config()
    contract_path = os.environ.get("HOUSE_BRAIN_AI_CONTRACT", "contracts/house_brain_ai_read_plane.v2.json")
    try:
        flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
        with os.fdopen(os.open(contract_path, flags), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise RuntimeConfigError("CONTRACT_FILE_TYPE_REJECTED")
            contract = stream.read(65537)
    except OSError:
        raise RuntimeConfigError("CONTRACT_READ_FAILED") from None
    if not 0 < len(contract) <= 65536:
        raise RuntimeConfigError("CONTRACT_SIZE_INVALID")
    try:
        build_catalog(contract)
    except CatalogError:
        raise RuntimeConfigError("CONTRACT_IDENTITY_INVALID") from None
    boundary = SnapshotReadBoundary(transport=AsyncioHttpsBrokerTransport(read_key),
                                    grants=TunnelGrantProvider(principal, scopes))
    return HouseBrainMcpCore(boundary=boundary, contract=contract, auth_profile="private_tunnel",
                             read_gate=kill_switch_read_gate(os.environ.get("HOUSE_BRAIN_AI_KILL_SWITCH_VIEW", "")))


def _error(rid, code, message):
    return {"jsonrpc": "2.0", "id": response_id(rid), "error": {"code": code, "message": message}}


async def _one(core, raw: bytes):
    request_id = None
    try:
        request = decode_rpc(raw)
        request_id = response_id(request.get("id"))
        return await core.dispatch(request)
    except McpError as exc:
        response = _error(request_id, exc.code, exc.message)
        response["error"] = exc.as_error()
        return response
    except Exception:
        return _error(request_id, -32603, "INTERNAL_ERROR")


async def _run_stdio(core, reader, send) -> int:
    """One bounded request table; keep reading so cancellation can interrupt I/O.

    EOF cancels outstanding reads. Notifications never receive responses. An
    oversized/unterminated frame terminates the stream instead of reinterpreting
    its remainder as another command. No session or caller-chosen destination.
    """
    active = {}
    failure = asyncio.get_running_loop().create_future()
    read_task = None

    async def answer(key, raw):
        try:
            await send(await _one(core, raw))
        except Exception:
            if not failure.done():
                failure.set_result(True)
        finally:
            if active.get(key) is asyncio.current_task():
                del active[key]

    try:
        while True:
            read_task = asyncio.create_task(reader.readline())
            done, _ = await asyncio.wait((read_task, failure), return_when=asyncio.FIRST_COMPLETED)
            if failure in done:
                return 2
            try:
                raw = read_task.result()
            except (ValueError, asyncio.LimitOverrunError):
                await send(_error(None, -32600, "REQUEST_SIZE_INVALID"))
                return 2
            if raw == b"":
                return 0
            if len(raw) > MAX_LINE_BYTES + 1 or not raw.endswith(b"\n"):
                await send(_error(None, -32600, "REQUEST_SIZE_INVALID"))
                return 2
            raw = raw[:-1]
            if raw.endswith(b"\r"):
                raw = raw[:-1]
            try:
                request = decode_rpc(raw)
            except McpError as exc:
                await send(_error(None, exc.code, exc.message))
                continue
            if "id" not in request:
                params = request.get("params")
                if (request.get("jsonrpc") == "2.0" and request.get("method") == "notifications/cancelled"
                        and type(params) is dict):
                    rid = response_id(params.get("requestId"))
                    task = active.get((type(rid), rid)) if rid is not None else None
                    if task is not None:
                        task.cancel()
                continue
            rid = response_id(request["id"])
            if rid is None:
                await send(_error(None, -32600, "INVALID_REQUEST"))
                continue
            key = (type(rid), rid)
            if key in active:
                await send(_error(None, -32600, "DUPLICATE_REQUEST_ID"))
                return 2
            if len(active) >= MAX_CONCURRENT_READS:
                await send(_error(rid, -32000, "READ_CAPACITY_EXCEEDED"))
                continue
            active[key] = asyncio.create_task(answer(key, raw))
    finally:
        tasks = list(active.values())
        if read_task is not None:
            tasks.append(read_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        failure.cancel()


class _PipeOutput(asyncio.Protocol):
    """Async write-pipe backpressure; a stopped consumer cannot pin shutdown."""
    def __init__(self):
        self.writable = asyncio.Event()
        self.writable.set()
        self.closed = False

    def pause_writing(self):
        self.writable.clear()

    def resume_writing(self):
        self.writable.set()

    def connection_lost(self, exc):
        self.closed = True
        self.writable.set()


async def _serve(core) -> int:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=MAX_LINE_BYTES + 1)
    incoming, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
    outgoing = None
    try:
        protocol = _PipeOutput()
        outgoing, _ = await loop.connect_write_pipe(lambda: protocol, sys.stdout.buffer)
        outgoing.set_write_buffer_limits(high=65536, low=16384)
        lock = asyncio.Lock()

        async def send(response):
            encoded = json.dumps(response, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode() + b"\n"
            if len(encoded) > MAX_RESPONSE_BYTES:
                raise RuntimeConfigError("RESPONSE_SIZE_INVALID")
            async with asyncio.timeout(OUTPUT_TIMEOUT_SECONDS):
                async with lock:
                    if protocol.closed:
                        raise BrokenPipeError
                    outgoing.write(encoded)
                    await protocol.writable.wait()
                    if protocol.closed:
                        raise BrokenPipeError
        return await _run_stdio(core, reader, send)
    finally:
        incoming.close()
        if outgoing is not None:
            outgoing.close()


def main() -> int:
    try:
        core = build_core()
        return asyncio.run(_serve(core))
    except RuntimeConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError):
        print("STDIO_UNAVAILABLE", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
