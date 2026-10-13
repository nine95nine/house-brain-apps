#!/usr/bin/env python3
"""Repository Secret Boundary R1.

Deterministically scans *tracked files whose worktree bytes exactly match the
stage-zero Git index* for a small set of high-confidence secret patterns.
Findings expose metadata only; matched values are never returned, printed, hashed,
nor sent elsewhere.

Scope intentionally excludes Git history, runtime secret stores, untracked
files, remote validity checks, and entropy-based detection.
"""

from __future__ import annotations

import argparse
import bisect
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Sequence

SCHEMA_VERSION = "repository_secret_boundary.v1"
MAX_TEXT_FILE_BYTES = 5 * 1024 * 1024
BINARY_PROBE_BYTES = 8192

EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_INTERNAL_ERROR = 2


class SecretBoundaryError(RuntimeError):
    """Fail-closed scanner/repository failure."""


@dataclass(frozen=True, order=True)
class Finding:
    path: str
    line: int
    column: int
    rule_id: str
    category: str
    match_length: int

    def render(self) -> str:
        return (
            f"{self.rule_id} {self.category} path={self.path!r} "
            f"line={self.line} column={self.column} match_length={self.match_length}"
        )


@dataclass(frozen=True)
class Rule:
    rule_id: str
    category: str
    pattern: re.Pattern[str]
    value_group: int | None = None
    placeholder_aware: bool = False


_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?:(?:RSA|EC|DSA|OPENSSH|PGP|ENCRYPTED) )?PRIVATE KEY(?: BLOCK)?-----"
)
_GITHUB_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{30,255}|github_pat_[A-Za-z0-9_]{40,255})(?![A-Za-z0-9_])"
)
_AWS_ACCESS_KEY_ID = re.compile(
    r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"
)
_CREDENTIAL_NAME = (
    r"(?:password|passwd|api[_-]?key|client[_-]?secret|secret[_-]?key|"
    r"aws[_-]?secret[_-]?access[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token)"
)
_CREDENTIAL_ASSIGNMENT = re.compile(
    rf"(?im)(?:^\s*|[{{,;]\s*)[\"']?{_CREDENTIAL_NAME}[\"']?"
    r"\s*(?::|=)\s*([\"'])([^\n\r\"']{8,512})\1"
)
_UNQUOTED_CREDENTIAL_ASSIGNMENT = re.compile(
    rf"(?im)^\s*[\"']?{_CREDENTIAL_NAME}[\"']?"
    r"\s*(?::|=)\s*([^\s#\"',;]{12,256})(?:\s*(?:#.*)?)?$"
)
_PYTHON_SYMBOLIC_REFERENCE = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$"
)

_BASIC_AUTH_URL = re.compile(
    r"https?://[^\s/:@]{1,128}:([^\s/@]{8,256})@[^\s/]+",
    re.IGNORECASE,
)

RULES: tuple[Rule, ...] = (
    Rule("RSB001_PRIVATE_KEY_BLOCK", "PRIVATE_KEY_BLOCK", _PRIVATE_KEY),
    Rule("RSB002_GITHUB_TOKEN", "GITHUB_TOKEN", _GITHUB_TOKEN, placeholder_aware=True),
    Rule("RSB003_AWS_ACCESS_KEY_ID", "AWS_ACCESS_KEY_ID", _AWS_ACCESS_KEY_ID, placeholder_aware=True),
    Rule(
        "RSB004_CREDENTIAL_ASSIGNMENT",
        "CREDENTIAL_ASSIGNMENT",
        _CREDENTIAL_ASSIGNMENT,
        value_group=2,
        placeholder_aware=True,
    ),
    Rule(
        "RSB004_CREDENTIAL_ASSIGNMENT",
        "CREDENTIAL_ASSIGNMENT",
        _UNQUOTED_CREDENTIAL_ASSIGNMENT,
        value_group=1,
        placeholder_aware=True,
    ),
    Rule(
        "RSB005_BASIC_AUTH_URL",
        "BASIC_AUTH_URL",
        _BASIC_AUTH_URL,
        value_group=1,
        placeholder_aware=True,
    ),
)

# Exact or structural placeholder forms only. Deliberately avoid broad substring
# tests such as "test" or "example" that could hide a real credential.
_EXACT_PLACEHOLDERS = frozenset(
    {
        "redacted",
        "placeholder",
        "changeme",
        "change_me",
        "your_token_here",
        "your-token-here",
        "your_password_here",
        "your-password-here",
        "not-a-real-secret",
        "not_a_real_secret",
        "dummy_value",
        "dummy-value",
    }
)
_TEMPLATE_PATTERNS = (
    re.compile(r"^<[^<>]+>$"),
    re.compile(r"^\$\{[^{}]+\}$"),
    re.compile(r"^\{\{[^{}]+\}\}$"),
    re.compile(r"^\{[A-Za-z_][A-Za-z0-9_.-]*\}$"),
    re.compile(r"^!secret\s+\S+$", re.IGNORECASE),
)
_REPEATED_CHAR = re.compile(r"^(.)\1{7,}$")


def _provider_token_placeholder(value: str) -> bool:
    """Recognize obvious provider-token examples without broad substring bypasses."""
    if value.startswith("github_pat_"):
        tail = value[len("github_pat_"):]
    elif re.match(r"^gh[pousr]_", value):
        tail = value[4:]
    elif value.startswith(("AKIA", "ASIA")):
        tail = value[4:]
    else:
        return False
    compact = tail.replace("_", "")
    return bool(compact) and (_REPEATED_CHAR.fullmatch(compact) is not None)


def _is_python_symbolic_reference(path: str, value: str) -> bool:
    """Identify symbolic Python RHS references without suppressing literals."""
    return (
        PurePosixPath(path).suffix.lower() == ".py"
        and _PYTHON_SYMBOLIC_REFERENCE.fullmatch(value.strip()) is not None
    )


def _looks_like_placeholder(value: str) -> bool:
    stripped = value.strip()
    lowered = stripped.lower()
    if lowered in _EXACT_PLACEHOLDERS:
        return True
    if any(pattern.fullmatch(stripped) for pattern in _TEMPLATE_PATTERNS):
        return True
    if _REPEATED_CHAR.fullmatch(stripped):
        return True
    if _provider_token_placeholder(stripped):
        return True
    return False


def _run_git(root: Path, args: Sequence[str]) -> bytes:
    command = ["git", "-C", str(root), *args]
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SecretBoundaryError(f"git command failed to execute: {type(exc).__name__}") from exc
    if completed.returncode != 0:
        raise SecretBoundaryError(f"git command failed with exit={completed.returncode}")
    return completed.stdout


def _assert_index_flags_scannable(root: Path) -> None:
    """Reject index flags that make worktree/index comparison non-authoritative.

    ``git diff`` intentionally trusts assume-unchanged and skip-worktree state in
    cases where R1 must instead prove the scanned worktree bytes correspond to
    the stage-zero index. R1 therefore fails closed on either flag rather than
    mutating repository index metadata behind the caller's back.
    """
    raw = _run_git(root, ["ls-files", "-v", "-z"])
    for record in raw.split(b"\0"):
        if not record:
            continue
        if len(record) < 3 or record[1:2] != b" ":
            raise SecretBoundaryError("malformed git index flag entry")
        tag = record[:1]
        try:
            tracked_path = record[2:].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SecretBoundaryError("non-UTF-8 tracked path in git index flag listing") from exc

        # `ls-files -v` emits S for skip-worktree. If assume-unchanged is also
        # set, the status tag becomes lowercase, so `s` represents both flags.
        if tag in (b"S", b"s"):
            raise SecretBoundaryError(
                f"tracked path has skip-worktree flag outside R1 custody: {tracked_path!r}"
            )
        if tag.isalpha() and tag.islower():
            raise SecretBoundaryError(
                f"tracked path has assume-unchanged flag outside R1 custody: {tracked_path!r}"
            )


def _assert_worktree_matches_index(root: Path) -> None:
    """Require tracked worktree content to equal the stage-zero Git index.

    R1 reads worktree bytes for bounded binary probing and symlink safety. A PASS
    is therefore valid only when those bytes represent the exact staged source.
    Untracked files remain intentionally outside R1 scope.
    """
    command = ["git", "-C", str(root), "diff", "--quiet", "--no-ext-diff", "--"]
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise SecretBoundaryError(
            f"git index/worktree comparison failed to execute: {type(exc).__name__}"
        ) from exc
    if completed.returncode == 1:
        raise SecretBoundaryError("tracked worktree differs from stage-zero index")
    if completed.returncode != 0:
        raise SecretBoundaryError(
            f"git index/worktree comparison failed with exit={completed.returncode}"
        )


def _repository_root(start: Path) -> Path:
    raw = _run_git(start, ["rev-parse", "--show-toplevel"])
    try:
        resolved = Path(raw.decode("utf-8").strip()).resolve(strict=True)
    except (UnicodeDecodeError, OSError) as exc:
        raise SecretBoundaryError("unable to resolve repository root") from exc
    if not resolved.is_dir():
        raise SecretBoundaryError("repository root is not a directory")
    return resolved


def _tracked_entries(root: Path) -> tuple[tuple[str, str], ...]:
    """Return sorted (mode, path) entries from the Git index.

    Stage-zero only is required. Unmerged entries fail closed because the scan
    surface would otherwise be ambiguous.
    """
    raw = _run_git(root, ["ls-files", "-s", "-z"])
    entries: dict[str, str] = {}
    if not raw:
        return ()
    for record in raw.split(b"\0"):
        if not record:
            continue
        try:
            metadata, path_bytes = record.split(b"\t", 1)
            mode_b, _object_b, stage_b = metadata.split(b" ", 2)
            path = path_bytes.decode("utf-8")
            mode = mode_b.decode("ascii")
            stage = stage_b.decode("ascii")
        except (ValueError, UnicodeDecodeError) as exc:
            raise SecretBoundaryError("malformed git index entry") from exc
        if stage != "0":
            raise SecretBoundaryError("unmerged git index entry present")
        if path in entries and entries[path] != mode:
            raise SecretBoundaryError("duplicate tracked path with conflicting mode")
        entries[path] = mode
    return tuple(sorted((mode, path) for path, mode in entries.items()))


def _safe_worktree_path(root: Path, tracked_path: str) -> Path:
    posix = PurePosixPath(tracked_path)
    if posix.is_absolute() or ".." in posix.parts or not posix.parts:
        raise SecretBoundaryError("unsafe tracked path")
    candidate = root.joinpath(*posix.parts)
    try:
        parent = candidate.parent.resolve(strict=True)
    except OSError as exc:
        raise SecretBoundaryError(f"tracked path parent unavailable: {tracked_path!r}") from exc
    if root != parent and root not in parent.parents:
        raise SecretBoundaryError(f"tracked path escapes repository: {tracked_path!r}")
    return candidate


def _read_text_entry(root: Path, mode: str, tracked_path: str) -> str | None:
    # Gitlinks/submodules are not recursively trusted. R1 fails closed instead
    # of silently issuing a repository PASS with an unscanned tracked subtree.
    if mode == "160000":
        raise SecretBoundaryError(f"tracked submodule outside R1 scan scope: {tracked_path!r}")

    path = _safe_worktree_path(root, tracked_path)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise SecretBoundaryError(f"tracked path unavailable: {tracked_path!r}") from exc

    if stat.S_ISLNK(metadata.st_mode) or mode == "120000":
        try:
            return os.readlink(path)
        except OSError as exc:
            raise SecretBoundaryError(f"unable to read tracked symlink: {tracked_path!r}") from exc

    if not stat.S_ISREG(metadata.st_mode):
        raise SecretBoundaryError(f"tracked path is not regular file: {tracked_path!r}")

    if metadata.st_size > MAX_TEXT_FILE_BYTES:
        # Probe first. Large binaries are safe to skip; large text is an
        # explicit coverage failure rather than a silent omission.
        try:
            with path.open("rb") as handle:
                probe = handle.read(BINARY_PROBE_BYTES)
        except OSError as exc:
            raise SecretBoundaryError(f"unable to probe tracked path: {tracked_path!r}") from exc
        if b"\0" in probe:
            return None
        raise SecretBoundaryError(f"tracked text file exceeds scan limit: {tracked_path!r}")

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SecretBoundaryError(f"unable to read tracked path: {tracked_path!r}") from exc
    if b"\0" in raw[:BINARY_PROBE_BYTES]:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        # Non-UTF-8 tracked content is treated as binary for this R1 text
        # scanner. This is explicit, deterministic, and avoids lossy decoding.
        return None


def _newline_offsets(text: str) -> tuple[int, ...]:
    return (-1, *(i for i, char in enumerate(text) if char == "\n"))


def _line_column(newlines: Sequence[int], offset: int) -> tuple[int, int]:
    index = bisect.bisect_right(newlines, offset - 1) - 1
    last_newline = newlines[index]
    return index + 1, offset - last_newline


def scan_text(path: str, text: str) -> tuple[Finding, ...]:
    findings: list[Finding] = []
    seen: set[tuple[str, int, int]] = set()
    newlines = _newline_offsets(text)
    for rule in RULES:
        for match in rule.pattern.finditer(text):
            value = match.group(rule.value_group) if rule.value_group is not None else match.group(0)
            # An unquoted Python identifier/attribute RHS is executable source
            # reference, not a hard-coded credential literal. Keep this exception
            # confined to the unquoted-assignment rule and .py files only.
            if (
                rule.pattern is _UNQUOTED_CREDENTIAL_ASSIGNMENT
                and _is_python_symbolic_reference(path, value)
            ):
                continue
            if rule.placeholder_aware and _looks_like_placeholder(value):
                continue
            start = match.start(rule.value_group) if rule.value_group is not None else match.start()
            line, column = _line_column(newlines, start)
            key = (rule.rule_id, start, len(value))
            if key in seen:
                continue
            seen.add(key)
            findings.append(
                Finding(
                    path=path,
                    line=line,
                    column=column,
                    rule_id=rule.rule_id,
                    category=rule.category,
                    match_length=len(value),
                )
            )
    return tuple(sorted(findings))


def scan_repository(start: Path) -> tuple[Finding, ...]:
    root = _repository_root(start)
    _assert_index_flags_scannable(root)
    _assert_worktree_matches_index(root)
    findings: list[Finding] = []
    for mode, tracked_path in _tracked_entries(root):
        text = _read_text_entry(root, mode, tracked_path)
        if text is None:
            continue
        findings.extend(scan_text(tracked_path, text))
    # Close the read window: a tracked file or relevant index flag changing
    # during the scan cannot silently earn a PASS against different source.
    _assert_worktree_matches_index(root)
    _assert_index_flags_scannable(root)
    return tuple(sorted(findings))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scan tracked repository text for high-confidence secret patterns.")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Path inside target Git worktree (default: cwd).")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        findings = scan_repository(args.root)
    except SecretBoundaryError as exc:
        # Exception text is intentionally scanner/repository metadata only.
        print(f"RSB INTERNAL ERROR: {exc}", file=sys.stderr)
        return EXIT_INTERNAL_ERROR

    if findings:
        print(f"REPOSITORY SECRET BOUNDARY: FAIL findings={len(findings)} schema={SCHEMA_VERSION}")
        for finding in findings:
            print(finding.render())
        return EXIT_FINDINGS

    print(f"REPOSITORY SECRET BOUNDARY: PASS findings=0 schema={SCHEMA_VERSION}")
    return EXIT_CLEAN


if __name__ == "__main__":
    raise SystemExit(main())
