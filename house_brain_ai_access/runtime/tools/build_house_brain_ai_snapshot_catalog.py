#!/usr/bin/env python3
"""Source-pinned, deny-by-default catalog projection for the #212 snapshot lane.

This is offline adapter support, NOT an MCP server or authenticator. Inputs named
``implemented_tools`` and ``granted_scopes`` must come from trusted application
wiring and a separately verified principal, never a request body. Tool listing is
not call authorization: a future dispatcher must recheck scope on every call.
No source pin override, dynamic tool import, network access, or HA mutation exists.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Final

SOURCE_SHA256: Final = "7dc9f759518de757259edc88fc36ee7a9c36d2f75bb50c75e5171cd0c447c510"
SOURCE_GIT_BLOB: Final = "fcdbad0853f774e7e57489d9300f31ab81d7eacd"
PROFILE: Final = "house_brain_ai_snapshot_catalog.v1"
MAX_SOURCE_BYTES: Final = 65536
# Deliberately independent of the source's nine declarations. Expanding this set
# requires new producer/schema/handler evidence and review, not a new permission.
SNAPSHOT_SCOPES: Final = (
    ("get_app_identity", "engineering.read.artifact"),
    ("get_backup_readiness", "engineering.read.backup"),
    ("get_gateway_readiness", "engineering.read.gateway"),
    ("get_house_brain_status", "engineering.read.status"),
    ("get_platform_versions", "engineering.read.platform"),
)


class CatalogError(ValueError):
    """Only constant, non-sensitive reason codes may leave this boundary."""


def canonical_bytes(value: Any) -> bytes:
    """Python sorted-key compact UTF-8 JSON; not an RFC 8785/JCS claim."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _checked_set(value: object, allowed: frozenset[str], code: str) -> frozenset[str]:
    # Reject strings, generators, subclasses, and arbitrary iterable execution.
    if type(value) is not frozenset or not all(type(item) is str for item in value):
        raise CatalogError(code)
    if not value <= allowed:
        raise CatalogError(code)
    return value


def build_catalog(
    raw: bytes, *, implemented_tools: frozenset[str] = frozenset(),
    granted_scopes: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Project only source-pinned snapshot tools both implemented AND scoped.

    Default result is empty. Neither the nine-tool declaration nor five-tool
    profile alone proves handler availability. Exact source-byte drift blocks the
    entire projection. Returned dictionaries are detached from subsequent calls.
    """
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_SOURCE_BYTES:
        raise CatalogError("SOURCE_BYTES_INVALID")
    if hashlib.sha256(raw).hexdigest() != SOURCE_SHA256:
        raise CatalogError("SOURCE_IDENTITY_MISMATCH")
    names = frozenset(name for name, _ in SNAPSHOT_SCOPES)
    scopes = frozenset(scope for _, scope in SNAPSHOT_SCOPES)
    implemented = _checked_set(implemented_tools, names, "IMPLEMENTATION_SET_REJECTED")
    granted = _checked_set(granted_scopes, scopes, "SCOPE_SET_REJECTED")
    # Parsing occurs only after byte identity is proven. Requalification is
    # required for *any* input-file change, including formatting-only changes.
    source = json.loads(raw)
    by_name = {tool["name"]: tool for tool in source["tools"]}
    selected: list[dict[str, Any]] = []
    for name, scope in SNAPSHOT_SCOPES:
        tool = by_name[name]
        if tool["scope"] != scope or tool["mutation"] is not False or tool["physical_control"] is not False:
            raise CatalogError("SOURCE_POLICY_MISMATCH")
        if name not in implemented or scope not in granted:
            continue
        selected.append({
            "name": name,
            "description": tool["description"],
            "inputSchema": tool["input_schema"],
            "outputSchema": tool["output_schema"],
            "annotations": {"readOnlyHint": True, "destructiveHint": False,
                            "idempotentHint": True, "openWorldHint": False},
        })
    identity = {
        "schema": PROFILE,
        "source_file_sha256": SOURCE_SHA256,
        "protocol_baseline": source["protocol"]["baseline"],
        "implemented_tools": sorted(implemented),
        "granted_scopes": sorted(granted),
        "tools": selected,
        "control_authority": "NONE",
    }
    return {
        **identity,
        "effective_catalog_sha256": hashlib.sha256(canonical_bytes(identity)).hexdigest(),
        "identity_algorithm": "SHA256_PYTHON_SORTED_KEYS_COMPACT_UTF8_JSON_V1",
        "source_git_blob": SOURCE_GIT_BLOB,
        "source_declared_catalog_sha256": source["tool_catalog_sha256"],
        "source_declared_catalog_digest_status": "UNVERIFIED_CANONICALIZATION_PRESERVED",
        "declared_tool_count": source["tool_count"],
        "profile_tool_count": len(SNAPSHOT_SCOPES),
        "effective_tool_count": len(selected),
        "status": "OFFLINE_ADAPTER_CANDIDATE_NOT_COMMISSIONED",
        "authentication_performed": False,
        "runtime_acceptance_proven": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("contract", nargs="?", type=Path,
                        default=Path("contracts/house_brain_ai_read_plane.v2.json"))
    parser.add_argument("--implemented-tool", action="append", default=[])
    parser.add_argument("--granted-scope", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        # Bounded regular-file read: directories, symlinks, devices, and FIFOs are
        # rejected. CLI paths are local operator inputs, not network parameters.
        import os
        import stat
        flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW
        with os.fdopen(os.open(args.contract, flags), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise CatalogError("SOURCE_FILE_TYPE_REJECTED")
            raw = stream.read(MAX_SOURCE_BYTES + 1)
        result = build_catalog(raw, implemented_tools=frozenset(args.implemented_tool),
                               granted_scopes=frozenset(args.granted_scope))
    except CatalogError as exc:
        print(json.dumps({"ok": False, "reason": str(exc), "control_authority": "NONE"}))
        return 2
    except OSError:
        # Do not reflect paths, hostnames, credentials or exception text.
        print(json.dumps({"ok": False, "reason": "SOURCE_READ_FAILED", "control_authority": "NONE"}))
        return 2
    print(canonical_bytes(result).decode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
