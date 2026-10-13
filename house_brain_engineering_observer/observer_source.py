"""Allowlisted local-App source-custody inspection for Engineering Observer v0.1."""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath
import re
import stat
from typing import Any

TARGET_ID = "aquarite_njspc"
TARGET_ROOT = Path("/addons/njspc_aquarite")
MAX_MANIFEST_ENTRIES = 128
MAX_HASH_FILE_BYTES = 4 * 1024 * 1024


def parse_manifest(path: Path) -> tuple[list[tuple[str, str]], str | None]:
    try:
        st = path.lstat()
        if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode) or st.st_size > 16384:
            return [], "manifest_unsafe"
        raw = path.read_bytes()
    except OSError:
        return [], "manifest_missing"
    manifest_sha = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return [], "manifest_not_utf8"
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in text.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"([0-9a-f]{64})  ([^\x00\r\n]+)", line)
        if not match:
            return [], "manifest_format_invalid"
        digest, rel = match.groups()
        pure = PurePosixPath(rel)
        if pure.is_absolute() or ".." in pure.parts or rel in seen:
            return [], "manifest_path_invalid"
        if len(pure.parts) != 1:
            # v0.1 deliberately allows only files at the approved target root.
            return [], "manifest_nested_path_not_allowed"
        seen.add(rel)
        entries.append((digest, rel))
        if len(entries) > MAX_MANIFEST_ENTRIES:
            return [], "manifest_too_many_entries"
    if not entries:
        return [], "manifest_empty"
    return entries, manifest_sha


def source_version(config_path: Path) -> str | None:
    try:
        st = config_path.lstat()
        if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode) or st.st_size > 65536:
            return None
        text = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    match = re.search(r'(?m)^version:\s*["\']?([0-9A-Za-z._-]+)["\']?\s*$', text)
    return match.group(1) if match else None


def inspect_target(root: Path = TARGET_ROOT) -> dict[str, Any]:
    result: dict[str, Any] = {
        "target_id": TARGET_ID,
        "source_present": False,
        "observed_source_version": None,
        "manifest_sha256": None,
        "manifest_entry_count": 0,
        "manifest_self_check": False,
        "missing_count": 0,
        "mismatch_count": 0,
        "extra_count": 0,
        "unsafe_file_type_count": 0,
        "inspection_error": None,
    }
    try:
        if not root.exists() or not root.is_dir() or root.is_symlink():
            result["inspection_error"] = "target_missing_or_unsafe"
            return result
        resolved_root = root.resolve(strict=True)
        if resolved_root != root:
            result["inspection_error"] = "target_root_resolution_changed"
            return result
    except OSError:
        result["inspection_error"] = "target_root_unavailable"
        return result

    result["source_present"] = True
    result["observed_source_version"] = source_version(root / "config.yaml")
    entries, manifest_or_error = parse_manifest(root / "SOURCE_MANIFEST.sha256")
    if not entries:
        result["inspection_error"] = manifest_or_error
        return result
    result["manifest_sha256"] = manifest_or_error
    result["manifest_entry_count"] = len(entries)

    expected = {name for _, name in entries}
    allowed_names = expected | {"SOURCE_MANIFEST.sha256"}
    missing = mismatch = unsafe = 0

    for digest, rel in entries:
        candidate = root / rel
        try:
            st = candidate.lstat()
            if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode):
                unsafe += 1
                continue
            if st.st_size > MAX_HASH_FILE_BYTES:
                unsafe += 1
                continue
            resolved = candidate.resolve(strict=True)
            if resolved.parent != resolved_root:
                unsafe += 1
                continue
            actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        except OSError:
            missing += 1
            continue
        if actual != digest:
            mismatch += 1

    extras = 0
    try:
        for item in root.iterdir():
            if item.name not in allowed_names:
                extras += 1
            try:
                st = item.lstat()
                if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
                    unsafe += 1
                if stat.S_ISLNK(st.st_mode):
                    unsafe += 1
            except OSError:
                unsafe += 1
    except OSError:
        result["inspection_error"] = "target_listing_unavailable"

    result["missing_count"] = missing
    result["mismatch_count"] = mismatch
    result["extra_count"] = extras
    result["unsafe_file_type_count"] = unsafe
    result["manifest_self_check"] = (
        missing == 0
        and mismatch == 0
        and extras == 0
        and unsafe == 0
        and result["inspection_error"] is None
    )
    return result
