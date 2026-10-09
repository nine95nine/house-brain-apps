"""Fixed HTTP migration repair. No caller-supplied path, YAML or UI mutation.

Only the literal Tailscale proxy block is supported. All unsupported syntax
fails closed. The rest of configuration.yaml is preserved byte for byte.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import stat

from .manifest import FileOp, Manifest, ManifestError, Source, _obj, _str, RE_REQUEST_ID, RE_SHA256, REQUESTERS

SCHEMA = "house_brain_http_cleanup_request.v1"
TARGET = "/config/configuration.yaml"
RE_BACKUP = re.compile(r"^/config/configuration\.yaml\.hbd_http_[a-z0-9][a-z0-9-]{2,63}\.bak$")
MAX_BYTES = 64 * 1024
REPAIR_ID = "yaml_still_present_after_migration"


class CleanupError(ValueError):
    pass


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fingerprint(value: dict) -> str:
    return sha(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())


def is_inspect(raw: bytes) -> bool:
    try:
        value = json.loads(raw)
        return isinstance(value, dict) and value.get("schema") == SCHEMA and value.get("operation") == "inspect"
    except (ValueError, UnicodeDecodeError, RecursionError):
        return False


def request(data: dict, digest: str) -> Manifest:
    operation = data.get("operation")
    if operation not in ("inspect", "remove"):
        raise ManifestError("HTTP_OPERATION")
    required = {"schema", "operation", "request_id", "requested_by", "tracking_issue"}
    if operation == "remove":
        required |= {"expect_current_sha256", "candidate_sha256", "stable_http_sha256"}
    obj = _obj(data, "http cleanup", required, set())
    rid = _str(obj["request_id"], "request_id", RE_REQUEST_ID, 64)
    who = obj["requested_by"]
    if who not in REQUESTERS:
        raise ManifestError("REQUESTER")
    issue = obj["tracking_issue"]
    if isinstance(issue, bool) or not isinstance(issue, int) or not 1 <= issue <= 10_000_000:
        raise ManifestError("RANGE", "tracking_issue")
    files: tuple[FileOp, ...] = ()
    stable = ""
    if operation == "remove":
        old = _str(obj["expect_current_sha256"], "expect_current_sha256", RE_SHA256, 64)
        new = _str(obj["candidate_sha256"], "candidate_sha256", RE_SHA256, 64)
        stable = _str(obj["stable_http_sha256"], "stable_http_sha256", RE_SHA256, 64)
        if old == new:
            raise ManifestError("HTTP_NO_CHANGE")
        files = (FileOp(TARGET, "replace", new, Source("http_cleanup", ""),
                        TARGET + ".hbd_http_" + rid + ".bak", old),)
    return Manifest(rid, "Remove migrated HTTP YAML" if files else "Inspect migrated HTTP YAML", who,
                    "main", "0" * 40, issue, "", files, (), (), (), (), (), ("ERROR", "CRITICAL"), 60,
                    digest=digest, summary="Preserve confirmed HTTP UI settings and all other configuration",
                    http_operation=operation, stable_http_sha256=stable)


def read_configuration(packages) -> bytes:
    path = packages.local(TARGET)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_BYTES
                or info.st_uid != os.geteuid() or info.st_gid != os.getegid()):
            raise CleanupError("HTTP_CONFIGURATION_FILE_UNSAFE")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise CleanupError("HTTP_CONFIGURATION_TOO_LARGE")
        return data
    finally:
        os.close(fd)


def remove_block(data: bytes) -> tuple[bytes, dict]:
    """Conservative literal grammar; not a generic YAML parser or patch API."""
    if len(data) > MAX_BYTES or data.startswith(b"\xef\xbb\xbf") or b"\x00" in data or b"\t" in data:
        raise CleanupError("HTTP_CONFIGURATION_ENCODING")
    try:
        text = data.decode("utf-8", "strict")
    except UnicodeDecodeError as err:
        raise CleanupError("HTTP_CONFIGURATION_ENCODING") from err
    lines = text.splitlines(keepends=True)
    roots = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or line.startswith(" "):
            continue
        match = re.match(r"^([a-zA-Z0-9_]+):(?:\s|$)", line)
        if not match:
            raise CleanupError("HTTP_ROOT_SYNTAX_UNSUPPORTED")
        roots.append((i, match.group(1)))
    keys = [key for _, key in roots]
    if len(keys) != len(set(keys)):
        raise CleanupError("HTTP_DUPLICATE_ROOT_KEY")
    positions = [i for i, key in roots if key == "http"]
    if len(positions) != 1:
        raise CleanupError("HTTP_BLOCK_NOT_FOUND")
    start = positions[0]
    end = next((i for i, _ in roots if i > start), len(lines))
    if lines[start].strip() != "http:":
        raise CleanupError("HTTP_BLOCK_SYNTAX_UNSUPPORTED")
    # Trailing comments and whitespace belong to the next section; preserve them.
    while end > start + 1 and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")):
        end -= 1
    active = [line.rstrip("\r\n") for line in lines[start + 1:end]
              if line.strip() and not line.lstrip().startswith("#")]
    expected = ["  use_x_forwarded_for: true", "  trusted_proxies:", "    - 127.0.0.1"]
    if active != expected:
        raise CleanupError("HTTP_BLOCK_UNSUPPORTED_REQUIRES_REVIEW")
    # Reject aliases into removed content, anchored/merged or multiline root mappings.
    if any(re.search(r"(?:^|\s)[&*][A-Za-z0-9_]|^\s*<<:", line)
           for line in lines if not line.lstrip().startswith("#")):
        raise CleanupError("HTTP_ALIAS_UNSUPPORTED")
    candidate = "".join(lines[:start] + lines[end:]).encode("utf-8")
    return candidate, {"use_x_forwarded_for": True, "trusted_proxies": ["127.0.0.1/32"]}


def stable_configuration(ha) -> dict:
    view = ha.http_configuration()
    if not isinstance(view, dict) or view.get("active_config_type") != "stable" or view.get("pending") is not None:
        raise CleanupError("HTTP_SETTINGS_NOT_CONFIRMED_STABLE")
    if view.get("revert_at") is not None or not isinstance(view.get("stable"), dict):
        raise CleanupError("HTTP_SETTINGS_NOT_CONFIRMED_STABLE")
    stable = view["stable"]
    if stable.get("error") is not None or stable.get("error_message") is not None:
        raise CleanupError("HTTP_STABLE_SETTINGS_ERROR")
    # Fingerprint every setting, including TLS paths, but never export them.
    return {key: value for key, value in stable.items() if key not in ("created_at", "error", "error_message")}


def inspect(ha, packages, require_issue: bool = True) -> tuple[bytes, dict]:
    data = read_configuration(packages)
    candidate, expected = remove_block(data)
    stable = stable_configuration(ha)
    try:
        proxies = [str(ipaddress.ip_network(p)) for p in stable.get("trusted_proxies", [])]
    except (ValueError, TypeError) as err:
        raise CleanupError("HTTP_PROXY_SETTINGS_INVALID") from err
    if stable.get("use_x_forwarded_for") is not True or proxies != expected["trusted_proxies"]:
        raise CleanupError("HTTP_UI_PROXY_MISMATCH_USE_NETWORK_SETTINGS")
    issues = ha.http_repair_ids()
    if require_issue and REPAIR_ID not in issues:
        raise CleanupError("HTTP_MIGRATION_REPAIR_NOT_PRESENT")
    if "deprecated_yaml_import_error" in issues:
        raise CleanupError("HTTP_MIGRATION_FAILED")
    return candidate, {"expect_current_sha256": sha(data), "candidate_sha256": sha(candidate),
                       "stable_http_sha256": fingerprint(stable), "http_proxy_matches": True}
