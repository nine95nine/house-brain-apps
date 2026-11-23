"""0.3.52: prove that a dashboard is one D5 itself created, and what D5 last wrote to it.

No new state file is needed and none is trusted from a request. The proof is
the Deployer's own ledger plus the exact request file it acted on:

1. the ledger entry for ``provenance_request_id`` has an owning outcome
   (D5 created, replaced or transplanted it). A dry run (``#dry`` key) never counts;
2. the request file is read again from ``deploy/requests`` and its SHA-256
   equals the digest that ledger entry recorded (a later edit of the file is
   refused). For a Try-again re-ask the re-ask file AND its original are
   checked against their own ledger digests;
3. the original parses as a D5 create/replace/transplant for the SAME
   dashboard; its ``expect_candidate_sha256`` is the hash D5 wrote, which
   the runner proved by exact read-back before it recorded that outcome.

The caller then requires the live config hash to equal that hash, so any
owner edit in the UI (or any other writer) makes the dashboard "not D5's"
until the owner chooses otherwise. A D5 *patch* never makes a dashboard
D5-owned (a patch may target an owner dashboard).

The existing /home-center (0.3.50 create ``2026-11-22-home-center-r2-create-01``,
D5_DASHBOARD_CREATED, hash 3b17e874…c12d, #561 comment 7074420126) is covered
by rule 1-3 without any adoption step: 0.3.50 recorded the outcome and the
request digest, and the request file is still on ``deploy/requests``.
"""
from __future__ import annotations

import hashlib

from . import dashboard_d5
from .manifest import RE_REQUEST_ID
from .reask import is_reask, parse_reask

CREATED = "D5_DASHBOARD_CREATED"
REPLACED = "D5_DASHBOARD_REPLACED"
TRANSPLANTED = "D5_DASHBOARD_TRANSPLANTED"
PROMOTED = "D5_DASHBOARD_PROMOTED"   # 0.3.53: the target now holds the D5-owned source byte for byte
ADOPTED = "D5_DASHBOARD_ADOPTED"     # 0.3.53: the owner re-owned an edited D5 dashboard (no write)
OWNING_OUTCOMES = frozenset({CREATED, REPLACED, TRANSPLANTED, PROMOTED, ADOPTED})
OWNING_KINDS = frozenset({"create", "replace", "transplant", "promote", "adopt"})


def _refuse(reason: str):
    raise dashboard_d5.D5Refused(reason)


def _entry(ledger: dict, request_id: str) -> dict:
    if not isinstance(request_id, str) or not RE_REQUEST_ID.fullmatch(request_id):
        _refuse("PROVENANCE_REQUEST_ID")
    entry = ledger.get(request_id)
    if not isinstance(entry, dict):
        _refuse("PROVENANCE_NOT_IN_LEDGER")
    return entry


def _read_exact(read_request, request_id: str, digest) -> bytes:
    try:
        raw = read_request(request_id)
    except Exception as err:  # noqa: BLE001 - unreadable provenance is no provenance
        raise dashboard_d5.D5Refused("PROVENANCE_UNREADABLE") from err
    if not isinstance(raw, bytes) or not isinstance(digest, str) or hashlib.sha256(raw).hexdigest() != digest:
        _refuse("PROVENANCE_DIGEST")
    return raw


def resolve(ledger: dict, read_request, provenance_id: str, dashboard: str) -> str:
    """Return the config SHA-256 D5 last wrote to ``dashboard`` by ``provenance_id``; refuse otherwise."""
    entry = _entry(ledger, provenance_id)
    if entry.get("outcome") not in OWNING_OUTCOMES:
        _refuse("PROVENANCE_NOT_D5_OWNED")
    raw = _read_exact(read_request, provenance_id, entry.get("digest"))
    if is_reask(raw):
        try:
            original_id = parse_reask(raw).reask_of
        except Exception as err:  # noqa: BLE001
            raise dashboard_d5.D5Refused("PROVENANCE_REASK") from err
        original = _entry(ledger, original_id)
        raw = _read_exact(read_request, original_id, original.get("digest"))
    try:
        req = dashboard_d5.parse(raw)
    except dashboard_d5.D5Refused as err:
        raise dashboard_d5.D5Refused("PROVENANCE_NOT_D5") from err
    if req.kind not in OWNING_KINDS:
        _refuse("PROVENANCE_NOT_D5_OWNED")
    if req.dashboard != dashboard:
        _refuse("PROVENANCE_OTHER_DASHBOARD")
    return req.expect_candidate_sha256
