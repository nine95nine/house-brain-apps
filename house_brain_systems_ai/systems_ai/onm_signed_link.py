"""A6.0 signed House link envelope (design ONM_A6_INVESTIGATOR_DESIGN_R1 §4, owner-approved 2026-11-14).

HMAC-SHA256 over a canonical header plus the exact body, with a separate ``onm_key`` that is never sent. Same
construction as R12 (``source_attestation.py``: a domain tag, a canonical header, a body digest, a constant-time
compare, an expiry and an increasing sequence), but this is the production profile: a real key, real body sizes and
the exact 64-hex B1 ticket. R12 stays the test-only fixture it was built as.

Offline library code. No transport, no key storage and no authority: a valid signature proves only that the sender
held the key, never that what it says is true.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re

SCHEMA = "house_brain_onm_signed_link.v1"
DOMAIN = b"HB-ONM-LINK-v1\x00"
METHODS = ("GET", "POST")
PATHS = ("/link/v1/cases", "/link/v1/repair")
ROLES = ("onm", "systems_ai")
MAX_BODY = 256 * 1024
MAX_SKEW_MS = 120000
MAX_TIME = 4102444800000
MAX_SEQ = 2**63 - 1
MIN_KEY = 32
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
HEADER_KEYS = frozenset(("schema", "role", "method", "path", "ticket", "seq", "ts_ms", "body_sha256", "mac_hex"))


class LinkHold(ValueError):
    """The message is refused. Never a reason to retry faster or to trust the body."""


def _hex(value) -> bool:
    return type(value) is str and HEX64.fullmatch(value) is not None


def _int(value, low, high) -> bool:
    return type(value) is int and low <= value <= high


def _key(key) -> bytes:
    if type(key) is not bytes or len(key) < MIN_KEY:
        raise LinkHold("LINK_KEY_CONTRACT")
    return key


def _canonical(header: dict) -> bytes:
    fields = {k: header[k] for k in sorted(HEADER_KEYS - {"mac_hex"})}
    return json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _mac(key: bytes, header: dict) -> str:
    return hmac.new(key, DOMAIN + _canonical(header), hashlib.sha256).hexdigest()


def sign(key: bytes, *, role: str, method: str, path: str, ticket: str, seq: int, ts_ms: int, body: bytes) -> dict:
    """Header for one message. The body is sent as the exact bytes given here."""
    key = _key(key)
    if (role not in ROLES or method not in METHODS or path not in PATHS or not _hex(ticket)
            or not _int(seq, 1, MAX_SEQ) or not _int(ts_ms, 1, MAX_TIME)
            or type(body) is not bytes or len(body) > MAX_BODY):
        raise LinkHold("LINK_HEADER_CONTRACT")
    header = {"schema": SCHEMA, "role": role, "method": method, "path": path, "ticket": ticket, "seq": seq,
              "ts_ms": ts_ms, "body_sha256": hashlib.sha256(body).hexdigest()}
    header["mac_hex"] = _mac(key, header)
    return header


def verify(key: bytes, header, body, *, role: str, method: str, path: str, now_ms: int, last_seq: int,
           ticket: str | None = None) -> int:
    """Check one message and return its sequence number. Every check is a hold:

    the contract, the MAC (constant time), the body digest, the clock window, a strictly increasing sequence
    (replays and reorders are refused), and, when given, the exact ticket the caller expects back.
    """
    key = _key(key)
    if (type(header) is not dict or set(header) != HEADER_KEYS or header["schema"] != SCHEMA
            or header["role"] != role or header["method"] != method or header["path"] != path
            or not _hex(header["ticket"]) or not _int(header["seq"], 1, MAX_SEQ)
            or not _int(header["ts_ms"], 1, MAX_TIME) or not _hex(header["body_sha256"])
            or not _hex(header["mac_hex"]) or type(body) is not bytes or len(body) > MAX_BODY):
        raise LinkHold("LINK_HEADER_CONTRACT")
    if not hmac.compare_digest(_mac(key, header), header["mac_hex"]):
        raise LinkHold("LINK_SIGNATURE_MISMATCH")
    if not hmac.compare_digest(hashlib.sha256(body).hexdigest(), header["body_sha256"]):
        raise LinkHold("LINK_BODY_DIGEST_MISMATCH")
    if not _int(now_ms, 1, MAX_TIME) or abs(now_ms - header["ts_ms"]) > MAX_SKEW_MS:
        raise LinkHold("LINK_CLOCK_WINDOW_HOLD")
    if not _int(last_seq, 0, MAX_SEQ) or header["seq"] <= last_seq:
        raise LinkHold("LINK_SEQUENCE_REPLAY_HOLD")
    if ticket is not None and header["ticket"] != ticket:
        raise LinkHold("LINK_TICKET_MISMATCH")
    return header["seq"]
