"""Strict validation of deployment request manifests.

Everything in a manifest is untrusted repository content. Validation is
allow-list only: unknown keys, wrong types, oversize values and anything that
does not match an exact pattern are refused. The validated result is a plain,
frozen structure that the rest of the Deployer consumes; raw manifest text is
never shown to the owner except the bounded, sanitized ``note``/``title``.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from . import SCHEMA
from .policy import (
    PolicyError,
    check_backup_path,
    check_package_target,
    check_repo_path,
)

MAX_MANIFEST_BYTES = 32 * 1024
MAX_FILES = 8
MAX_RETIRE = 8
MAX_CHECKS = 32
MAX_PRECONDITIONS = 8
MAX_UNCHANGED = 16

RE_REQUEST_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")
RE_SHA1 = re.compile(r"^[0-9a-f]{40}$")
RE_SHA256 = re.compile(r"^[0-9a-f]{64}$")
RE_ENTITY = re.compile(r"^[a-z_]{2,32}\.[a-z0-9_]{1,96}$")
RE_ATTRIBUTE = re.compile(r"^[a-z0-9_]{1,64}$")
RE_STATE = re.compile(r"^[A-Za-z0-9 _.:\-]{0,64}$")
RE_LOG_TOKEN = re.compile(r"^[a-z0-9_.]{3,40}$")
RE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
REQUESTERS = ("claude", "chatgpt", "owner")
LOG_LEVELS = ("WARNING", "ERROR", "CRITICAL")


class ManifestError(ValueError):
    """Manifest refused. ``code`` is a stable machine-readable reason."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Source:
    kind: str  # "blob" | "recipe"
    path: str  # repo path of the blob, or of the recipe JSON
    baseline: str | None = None  # recipe baseline repo path


@dataclass(frozen=True)
class FileOp:
    target: str
    mode: str  # "create" | "replace"
    sha256: str
    source: Source
    backup_as: str | None = None
    expect_current_sha256: str | None = None


@dataclass(frozen=True)
class RetireOp:
    path: str
    rename_to: str
    expect_current_sha256: str | None = None


@dataclass(frozen=True)
class StateCheck:
    entity_id: str
    attribute: str | None = None
    equals: str | None = None
    one_of: tuple[str, ...] | None = None
    number: float | None = None


@dataclass(frozen=True)
class Manifest:
    request_id: str
    title: str
    requested_by: str
    source_ref: str
    source_commit: str
    tracking_issue: int
    note: str
    files: tuple[FileOp, ...]
    retire: tuple[RetireOp, ...]
    preconditions: tuple[StateCheck, ...]
    expect_unchanged: tuple[str, ...]
    post_checks: tuple[StateCheck, ...]
    log_tokens: tuple[str, ...]
    log_fail_levels: tuple[str, ...]
    settle_seconds: int
    digest: str = field(default="")
    summary: str = field(default="")   # 0.3.3: one-line "what changes", AI-supplied, shown labelled

    def touched_paths(self) -> list[str]:
        paths: list[str] = []
        for op in self.files:
            paths.append(op.target)
            if op.backup_as:
                paths.append(op.backup_as)
        for rop in self.retire:
            paths.extend((rop.path, rop.rename_to))
        return paths


def sanitize_text(value: str, limit: int) -> str:
    """Printable ASCII only, collapsed whitespace, bounded length.

    Used for the two free-text fields. The result is always labelled as
    AI-supplied wherever it is displayed.
    """
    cleaned = "".join(ch if 32 <= ord(ch) < 127 else " " for ch in value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned[:limit]


def _obj(value: Any, where: str, required: set[str], optional: set[str]) -> dict:
    if not isinstance(value, dict):
        raise ManifestError("TYPE", f"{where} must be an object")
    keys = set(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        raise ManifestError("MISSING_KEY", f"{where}: {sorted(missing)[0]}")
    if unknown:
        raise ManifestError("UNKNOWN_KEY", f"{where}: {sorted(unknown)[0][:40]}")
    return value


def _str(value: Any, where: str, pattern: re.Pattern | None = None, limit: int = 256) -> str:
    if not isinstance(value, str):
        raise ManifestError("TYPE", f"{where} must be a string")
    if len(value) > limit:
        raise ManifestError("TOO_LONG", where)
    if pattern is not None and not pattern.fullmatch(value):
        raise ManifestError("PATTERN", where)
    return value


def _list(value: Any, where: str, limit: int) -> list:
    if not isinstance(value, list):
        raise ManifestError("TYPE", f"{where} must be a list")
    if len(value) > limit:
        raise ManifestError("TOO_MANY", where)
    return value


def _policy(fn, value: str, where: str) -> str:
    try:
        return fn(value)
    except PolicyError as err:
        raise ManifestError(err.code, where) from err


def _state_check(raw: Any, where: str) -> StateCheck:
    obj = _obj(raw, where, {"entity_id"}, {"attribute", "equals", "one_of", "number"})
    entity = _str(obj["entity_id"], f"{where}.entity_id", RE_ENTITY, 129)
    attribute = None
    if "attribute" in obj:
        attribute = _str(obj["attribute"], f"{where}.attribute", RE_ATTRIBUTE, 64)
    kinds = [k for k in ("equals", "one_of", "number") if k in obj]
    if len(kinds) != 1:
        raise ManifestError("CHECK_KIND", f"{where} needs exactly one of equals/one_of/number")
    equals = one_of = number = None
    if "equals" in obj:
        equals = _str(obj["equals"], f"{where}.equals", RE_STATE, 64)
    elif "one_of" in obj:
        items = _list(obj["one_of"], f"{where}.one_of", 16)
        if not items:
            raise ManifestError("EMPTY", f"{where}.one_of")
        one_of = tuple(_str(i, f"{where}.one_of", RE_STATE, 64) for i in items)
    else:
        num = obj["number"]
        if isinstance(num, bool) or not isinstance(num, (int, float)):
            raise ManifestError("TYPE", f"{where}.number")
        if num != num or abs(num) > 1e9:
            raise ManifestError("RANGE", f"{where}.number")
        number = float(num)
    return StateCheck(entity, attribute, equals, one_of, number)


def _source(raw: Any, where: str) -> Source:
    obj = _obj(raw, where, {"kind", "path"}, {"baseline"})
    kind = obj["kind"]
    if kind not in ("blob", "recipe"):
        raise ManifestError("SOURCE_KIND", where)
    path = _policy(check_repo_path, _str(obj["path"], f"{where}.path", None, 200), f"{where}.path")
    baseline = None
    if kind == "recipe":
        if not path.endswith(".json"):
            raise ManifestError("SOURCE_RECIPE", f"{where}.path")
        if "baseline" not in obj:
            raise ManifestError("MISSING_KEY", f"{where}.baseline")
        baseline = _policy(
            check_repo_path, _str(obj["baseline"], f"{where}.baseline", None, 200), f"{where}.baseline"
        )
    elif "baseline" in obj:
        raise ManifestError("UNKNOWN_KEY", f"{where}.baseline")
    return Source(kind, path, baseline)


def parse(raw_bytes: bytes) -> Manifest:
    """Parse and validate. Raises ManifestError on any deviation."""
    import hashlib

    if len(raw_bytes) > MAX_MANIFEST_BYTES:
        raise ManifestError("TOO_LARGE")
    try:
        text = raw_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as err:
        raise ManifestError("ENCODING") from err

    def _no_dupes(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ManifestError("DUPLICATE_KEY", str(key)[:40])
            out[key] = value
        return out

    def _no_constants(name):
        raise ManifestError("JSON_CONSTANT", name)

    try:
        data = json.loads(text, object_pairs_hook=_no_dupes, parse_constant=_no_constants)
    except ManifestError:
        raise
    except (ValueError, RecursionError) as err:
        raise ManifestError("JSON") from err

    top = _obj(
        data,
        "manifest",
        {"schema", "request_id", "title", "requested_by", "source_ref", "source_commit",
         "tracking_issue", "files", "post_checks"},
        {"note", "retire", "preconditions", "expect_unchanged", "log_scan", "settle_seconds", "summary"},
    )
    if top["schema"] != SCHEMA:
        raise ManifestError("SCHEMA_VERSION")
    request_id = _str(top["request_id"], "request_id", RE_REQUEST_ID, 64)
    title = sanitize_text(_str(top["title"], "title", None, 200), 80)
    if not title:
        raise ManifestError("EMPTY", "title")
    requested_by = top["requested_by"]
    if requested_by not in REQUESTERS:
        raise ManifestError("REQUESTER")
    source_ref = _str(top["source_ref"], "source_ref", RE_REF, 100)
    if ".." in source_ref or source_ref.endswith("/") or "//" in source_ref:
        raise ManifestError("PATTERN", "source_ref")
    source_commit = _str(top["source_commit"], "source_commit", RE_SHA1, 40)
    issue = top["tracking_issue"]
    if isinstance(issue, bool) or not isinstance(issue, int) or not 1 <= issue <= 10_000_000:
        raise ManifestError("RANGE", "tracking_issue")
    note = sanitize_text(_str(top.get("note", ""), "note", None, 2000), 300)
    summary = sanitize_text(_str(top.get("summary", ""), "summary", None, 600), 160)

    files: list[FileOp] = []
    for i, raw in enumerate(_list(top["files"], "files", MAX_FILES)):
        where = f"files[{i}]"
        obj = _obj(raw, where, {"target", "mode", "sha256", "source"},
                   {"backup_as", "expect_current_sha256"})
        target = _policy(check_package_target, _str(obj["target"], f"{where}.target", None, 200),
                         f"{where}.target")
        mode = obj["mode"]
        if mode not in ("create", "replace"):
            raise ManifestError("MODE", where)
        sha = _str(obj["sha256"], f"{where}.sha256", RE_SHA256, 64)
        source = _source(obj["source"], f"{where}.source")
        backup_as = expect = None
        if mode == "replace":
            if "backup_as" not in obj:
                raise ManifestError("MISSING_KEY", f"{where}.backup_as")
            backup_as = _policy(check_backup_path,
                                _str(obj["backup_as"], f"{where}.backup_as", None, 200),
                                f"{where}.backup_as")
            if not backup_as.startswith(target + "."):
                raise ManifestError("BACKUP_NAME", f"{where}.backup_as must extend the target name")
            if "expect_current_sha256" in obj:
                expect = _str(obj["expect_current_sha256"], f"{where}.expect_current_sha256",
                              RE_SHA256, 64)
        elif "backup_as" in obj or "expect_current_sha256" in obj:
            raise ManifestError("UNKNOWN_KEY", f"{where}: create takes no backup")
        files.append(FileOp(target, mode, sha, source, backup_as, expect))
    if not files:
        raise ManifestError("EMPTY", "files")

    retire: list[RetireOp] = []
    for i, raw in enumerate(_list(top.get("retire", []), "retire", MAX_RETIRE)):
        where = f"retire[{i}]"
        obj = _obj(raw, where, {"path", "rename_to"}, {"expect_current_sha256"})
        path = _policy(check_package_target, _str(obj["path"], f"{where}.path", None, 200),
                       f"{where}.path")
        rename_to = _policy(check_backup_path, _str(obj["rename_to"], f"{where}.rename_to", None, 200),
                            f"{where}.rename_to")
        if not rename_to.startswith(path + "."):
            raise ManifestError("BACKUP_NAME", f"{where}.rename_to must extend the path")
        expect = None
        if "expect_current_sha256" in obj:
            expect = _str(obj["expect_current_sha256"], f"{where}.expect_current_sha256", RE_SHA256, 64)
        retire.append(RetireOp(path, rename_to, expect))

    preconditions = tuple(
        _state_check(raw, f"preconditions[{i}]")
        for i, raw in enumerate(_list(top.get("preconditions", []), "preconditions", MAX_PRECONDITIONS))
    )
    unchanged = tuple(
        _str(raw, f"expect_unchanged[{i}]", RE_ENTITY, 129)
        for i, raw in enumerate(_list(top.get("expect_unchanged", []), "expect_unchanged", MAX_UNCHANGED))
    )
    post_checks = tuple(
        _state_check(raw, f"post_checks[{i}]")
        for i, raw in enumerate(_list(top["post_checks"], "post_checks", MAX_CHECKS))
    )
    if not post_checks:
        raise ManifestError("EMPTY", "post_checks")

    log_tokens: tuple[str, ...] = ()
    log_levels: tuple[str, ...] = ("ERROR", "CRITICAL")
    if "log_scan" in top:
        scan = _obj(top["log_scan"], "log_scan", {"tokens"}, {"fail_levels"})
        log_tokens = tuple(
            _str(t, "log_scan.tokens", RE_LOG_TOKEN, 40) for t in _list(scan["tokens"], "log_scan.tokens", 8)
        )
        if "fail_levels" in scan:
            levels = _list(scan["fail_levels"], "log_scan.fail_levels", 3)
            if not levels or any(lv not in LOG_LEVELS for lv in levels):
                raise ManifestError("LOG_LEVEL")
            if "ERROR" not in levels or "CRITICAL" not in levels:
                # Errors can never be waived.
                raise ManifestError("LOG_LEVEL", "ERROR and CRITICAL are mandatory")
            log_levels = tuple(sorted(set(levels)))

    settle = top.get("settle_seconds", 300)
    if isinstance(settle, bool) or not isinstance(settle, int) or not 60 <= settle <= 900:
        raise ManifestError("RANGE", "settle_seconds")

    manifest = Manifest(
        request_id, title, requested_by, source_ref, source_commit, issue, note,
        tuple(files), tuple(retire), preconditions, unchanged, post_checks,
        log_tokens, log_levels, settle,
        digest=hashlib.sha256(raw_bytes).hexdigest(), summary=summary,
    )
    _cross_checks(manifest)
    return manifest


def _cross_checks(m: Manifest) -> None:
    paths = m.touched_paths()
    if len(paths) != len(set(paths)):
        raise ManifestError("PATH_COLLISION")
    targets = {op.target for op in m.files}
    for rop in m.retire:
        if rop.path in targets:
            raise ManifestError("PATH_COLLISION", "retire path is also a target")
