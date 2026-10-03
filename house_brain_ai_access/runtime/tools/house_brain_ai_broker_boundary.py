"""Read-only Broker boundary with injected transport and verified-grant provider.

No default network adapter, token lookup, listener, OAuth implementation, or MCP
server exists here. Trusted wiring must supply a cancellation-cooperative async
transport and a grant provider backed by independently verified authentication.
Neither component may be constructed from tool arguments. One client is owned by
one event loop. Timeouts are cooperative, not a process-level hard-kill guarantee.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import re
from types import MappingProxyType
from typing import Any, Final, Protocol

from tools.build_house_brain_ai_snapshot_catalog import SNAPSHOT_SCOPES, build_catalog
from tools.house_brain_ai_snapshot_handlers import IMPLEMENTED_TOOLS, SnapshotError, call_snapshot_tool

SNAPSHOT_URL: Final = "https://house-brain-maintenance-broker.nine95nine.workers.dev/v1/snapshot"
MAX_RESPONSE_BYTES: Final = 65536  # 32 KiB snapshot plus bounded relay envelope.
MAX_CHUNKS: Final = 4096
MAX_CONCURRENT_READS: Final = 4
READ_TIMEOUT_SECONDS: Final = 5.0
TOOL_SCOPES: Final = MappingProxyType({n: s for n, s in SNAPSHOT_SCOPES if n in IMPLEMENTED_TOOLS})
ALL_SCOPES: Final = frozenset(s for _, s in SNAPSHOT_SCOPES)
ID_RE: Final = re.compile(r"[A-Za-z0-9_.:-]{1,96}")


class BoundaryError(ValueError):
    """Stable error reason, never a URL, credential, header, body or traceback."""


@dataclass(frozen=True, slots=True)
class ReadGrant:
    """Trusted auth-adapter result, NOT a JWT or a claim this module authenticates.

    revision must change when the authorization lease/principal changes. The
    provider is called again after I/O; revoked/expired/swapped leases disclose no
    result. Raw tokens are not part of this object or the tool interface.
    """
    principal_id: str
    revision: str
    expires_at: datetime
    scopes: frozenset[str] = frozenset()
    enabled: bool = False


@dataclass(frozen=True, slots=True)
class ReadRequest:
    url: str = SNAPSHOT_URL
    method: str = "GET"
    accept: str = "application/json"
    follow_redirects: bool = False
    max_response_bytes: int = MAX_RESPONSE_BYTES
    timeout_seconds: float = READ_TIMEOUT_SECONDS


@dataclass(frozen=True, slots=True)
class ReadReply:
    status: int
    url: str
    headers: tuple[tuple[str, str], ...]
    chunks: AsyncIterator[bytes]


class BrokerTransport(Protocol):
    def open(self, request: ReadRequest) -> AbstractAsyncContextManager[ReadReply]:
        """Stream exactly one fixed GET; close on success, refusal and cancellation.

        A real adapter must enforce TLS verification, credential custody, no
        redirects, no automatic retries, bounded headers/chunks and cooperative
        cancellation. Its streaming implementation needs separate qualification.
        """
        ...


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _time(value: Any) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise BoundaryError("CLOCK_INVALID")
    try:
        return value.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise BoundaryError("CLOCK_INVALID") from None


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise BoundaryError("JSON_DUPLICATE_KEY")
        result[key] = value
    return result


def _constant(_: str) -> Any:
    raise BoundaryError("JSON_NONFINITE")


def _float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise BoundaryError("JSON_NONFINITE")
    return result


def _integer(value: str) -> int:
    if len(value) > 17:
        raise BoundaryError("JSON_INTEGER_RANGE")
    result = int(value)
    if abs(result) > 9007199254740991:
        raise BoundaryError("JSON_INTEGER_RANGE")
    return result


def decode_snapshot(raw: bytes) -> dict[str, Any]:
    """Bounded UTF-8 JSON, including duplicate, depth/node and surrogate checks."""
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_RESPONSE_BYTES:
        raise BoundaryError("BODY_SIZE_INVALID")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_constant=_constant, parse_float=_float, parse_int=_integer)
    except BoundaryError:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise BoundaryError("JSON_INVALID") from None
    if type(value) is not dict:
        raise BoundaryError("JSON_ROOT_INVALID")
    stack = [(value, 0)]
    nodes = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if depth > 24 or nodes > 8192:
            raise BoundaryError("JSON_COMPLEXITY_LIMIT")
        if type(item) is dict:
            stack.extend((x, depth + 1) for pair in item.items() for x in pair)
        elif type(item) is list:
            stack.extend((x, depth + 1) for x in item)
        elif type(item) is str:
            try:
                item.encode("utf-8")
            except UnicodeError:
                raise BoundaryError("JSON_INVALID_UNICODE") from None
    return value


def _headers(reply: ReadReply) -> int | None:
    if type(reply) is not ReadReply or reply.url != SNAPSHOT_URL:
        raise BoundaryError("RESPONSE_ORIGIN_REJECTED")
    if type(reply.status) is not int:
        raise BoundaryError("HTTP_STATUS_INVALID")
    codes = {401: "BROKER_UNAUTHORIZED", 403: "BROKER_FORBIDDEN", 404: "BROKER_NO_SNAPSHOT",
             429: "BROKER_RATE_LIMITED"}
    if reply.status != 200:
        reason = codes.get(reply.status, "BROKER_UNAVAILABLE" if 500 <= reply.status <= 599 else
                           "REDIRECT_REJECTED" if 300 <= reply.status <= 399 else "HTTP_STATUS_REJECTED")
        raise BoundaryError(reason)
    if type(reply.headers) is not tuple or len(reply.headers) > 64:
        raise BoundaryError("HEADERS_INVALID")
    headers: dict[str, str] = {}
    total = 0
    for pair in reply.headers:
        if type(pair) is not tuple or len(pair) != 2 or any(type(x) is not str for x in pair):
            raise BoundaryError("HEADERS_INVALID")
        name, value = pair
        total += len(name) + len(value)
        if total > 8192 or re.fullmatch(r"[A-Za-z0-9-]{1,64}", name) is None or len(value) > 2048:
            raise BoundaryError("HEADERS_INVALID")
        if any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise BoundaryError("HEADERS_INVALID")
        key = name.lower()
        if key in headers:
            raise BoundaryError("DUPLICATE_HEADER")
        headers[key] = value.strip()
    content_type = headers.get("content-type", "").lower().replace(" ", "")
    if content_type not in ("application/json", "application/json;charset=utf-8"):
        raise BoundaryError("CONTENT_TYPE_REJECTED")
    if headers.get("content-encoding", "identity").lower() != "identity":
        raise BoundaryError("CONTENT_ENCODING_REJECTED")
    length = headers.get("content-length")
    if length is None:
        return None
    if re.fullmatch(r"[0-9]{1,6}", length) is None or int(length) > MAX_RESPONSE_BYTES:
        raise BoundaryError("CONTENT_LENGTH_INVALID")
    return int(length)


class SnapshotReadBoundary:
    def __init__(self, *, transport: BrokerTransport | None = None,
                 grants: Callable[[], ReadGrant | None] = lambda: None,
                 clock: Callable[[], datetime] = _utcnow) -> None:
        self._transport = transport
        self._grants = grants
        self._clock = clock
        self._active = 0
        self._loop: asyncio.AbstractEventLoop | None = None

    def _grant(self, tool: str | None = None) -> ReadGrant:
        try:
            grant = self._grants()
            current = _time(self._clock())
            if type(grant) is not ReadGrant or grant.enabled is not True:
                raise BoundaryError("AUTHORIZATION_REQUIRED")
            if (type(grant.principal_id) is not str or ID_RE.fullmatch(grant.principal_id) is None or
                    type(grant.revision) is not str or ID_RE.fullmatch(grant.revision) is None):
                raise BoundaryError("AUTHORIZATION_INVALID")
            if (type(grant.scopes) is not frozenset or any(type(s) is not str for s in grant.scopes) or
                    not grant.scopes <= ALL_SCOPES):
                raise BoundaryError("AUTHORIZATION_INVALID")
            if _time(grant.expires_at) <= current:
                raise BoundaryError("AUTHORIZATION_EXPIRED")
            if tool is not None and TOOL_SCOPES[tool] not in grant.scopes:
                raise BoundaryError("SCOPE_DENIED")
            return grant
        except BoundaryError:
            raise
        except Exception:
            raise BoundaryError("AUTHORIZATION_UNAVAILABLE") from None

    def catalog(self, contract: bytes) -> dict[str, Any]:
        grant = self._grant()
        return build_catalog(contract, implemented_tools=IMPLEMENTED_TOOLS, granted_scopes=grant.scopes)

    async def call(self, tool: str, arguments: Any) -> dict[str, Any]:
        if type(tool) is not str or tool not in TOOL_SCOPES:
            raise BoundaryError("TOOL_NOT_IMPLEMENTED")
        if type(arguments) is not dict or arguments:
            raise BoundaryError("ARGUMENTS_INVALID")
        grant = self._grant(tool)
        if self._transport is None:
            raise BoundaryError("TRANSPORT_UNAVAILABLE")
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise BoundaryError("EVENT_LOOP_MISMATCH")
        self._loop = loop
        # No await between admission and increment: no race within the owned loop.
        if self._active >= MAX_CONCURRENT_READS:
            raise BoundaryError("READ_CAPACITY_EXCEEDED")
        self._active += 1
        deadline = loop.time() + READ_TIMEOUT_SECONDS
        try:
            raw = bytearray()
            chunks = 0
            complete = False
            async with asyncio.timeout_at(deadline) as timeout:
                async with self._transport.open(ReadRequest()) as reply:
                    length = _headers(reply)
                    async for chunk in reply.chunks:
                        chunks += 1
                        if type(chunk) is not bytes or chunks > MAX_CHUNKS:
                            raise BoundaryError("STREAM_INVALID")
                        if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise BoundaryError("BODY_SIZE_INVALID")
                        raw.extend(chunk)
                    if length is not None and length != len(raw):
                        raise BoundaryError("CONTENT_LENGTH_MISMATCH")
                    complete = True
                if not complete:
                    raise BoundaryError("STREAM_INCOMPLETE")
                value = decode_snapshot(bytes(raw))
            if timeout.expired() or loop.time() > deadline:
                raise BoundaryError("BROKER_TIMEOUT")
            # Recheck revocation/expiry/scope and principal BEFORE exposing output.
            final = self._grant(tool)
            if (final.principal_id, final.revision) != (grant.principal_id, grant.revision):
                raise BoundaryError("AUTHORIZATION_CHANGED")
            return call_snapshot_tool(tool, value, {}, now=_time(self._clock()))
        except BoundaryError:
            raise
        except SnapshotError as exc:
            raise BoundaryError(str(exc)) from None
        except TimeoutError:
            raise BoundaryError("BROKER_TIMEOUT") from None
        except Exception:
            raise BoundaryError("BROKER_IO_FAILED") from None
        finally:
            self._active -= 1
        # CancelledError is a BaseException: deliberately not swallowed/retried.
