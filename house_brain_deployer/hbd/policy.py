"""Target-path allowlist, filename rules and package content policy.

Paths in manifests are expressed as the owner sees them (``/config/...``).
Only ``/config/packages/<slug>.yaml`` targets and ``<target>[.<tag>].bak``
backups are admissible. Slugs are lowercase ASCII letters, digits and ``_``
only, which also rejects the iOS "Save to Files" failure where ``_`` became a
space and Home Assistant silently skipped the package.
"""
from __future__ import annotations

import re

MAX_FILE_BYTES = 2 * 1024 * 1024

RE_TARGET = re.compile(r"^/config/packages/([a-z0-9][a-z0-9_]{0,99})\.yaml$")
RE_BACKUP = re.compile(r"^/config/packages/([a-z0-9][a-z0-9_]{0,99})\.yaml(\.[a-z0-9_]{1,32})?\.bak$")
RE_REPO_PATH = re.compile(r"^(production|tools|deploy)/[A-Za-z0-9._/-]{1,190}$")
DENIED_SLUGS = frozenset({"secrets", "configuration", "known_devices", "customize"})

# Top-level package keys that could run commands, reach the network with
# arbitrary requests, change authentication/HTTP exposure or destroy history.
DENIED_TOP_LEVEL_KEYS = frozenset({
    "shell_command", "command_line", "python_script", "pyscript", "rest_command",
    "http", "homeassistant", "panel_iframe", "panel_custom", "recorder",
    "frontend", "lovelace", "api", "websocket_api", "auth", "default_config",
})
RE_DENIED_TAG = re.compile(r"!(include\w*|env_var)\b")

# 0.3.31 (owner pop-ups 2026-10-19 "Teach the Deployer to do it", "Build as 0.3.31"): the one recorder shape a
# package may carry. The whole file is ``recorder: exclude: entities/entity_globs`` lists and nothing else, so it
# only stops new history rows (Home Assistant merges package dict/list keys into the root recorder config).
# purge_keep_days, include, db_url, auto_purge and every other recorder key stay refused: they could delete
# history. Block style only; every item is one entity id or one glob.
RE_RECORDER_ENTITY = re.compile(r"^[a-z][a-z0-9_]{0,40}\.[a-z0-9_]{1,200}$")
RE_RECORDER_GLOB = re.compile(r"^[a-z][a-z0-9_]{0,40}\.[a-z0-9_*]{1,200}$")
MAX_RECORDER_EXCLUDES = 200
RE_TOP_KEY = re.compile(r"^([A-Za-z0-9_]+)\s*:(\s|$)")

DEFAULT_SENSITIVE_PATTERNS = (
    r"_armed\b",
    r"\bdisarm",
    r"\balarm_control_panel\.",
    r"\block\.",
)


class PolicyError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


def check_package_target(path: str) -> str:
    m = RE_TARGET.fullmatch(path)
    if not m:
        raise PolicyError("TARGET_NOT_ALLOWED")
    if m.group(1) in DENIED_SLUGS:
        raise PolicyError("TARGET_DENIED")
    return path


def check_backup_path(path: str) -> str:
    m = RE_BACKUP.fullmatch(path)
    if not m:
        raise PolicyError("BACKUP_NOT_ALLOWED")
    if m.group(1) in DENIED_SLUGS:
        raise PolicyError("TARGET_DENIED")
    return path


def check_repo_path(path: str) -> str:
    if not RE_REPO_PATH.fullmatch(path):
        raise PolicyError("REPO_PATH")
    parts = path.split("/")
    if any(p in ("", ".", "..") or p.startswith(".") for p in parts):
        raise PolicyError("REPO_PATH")
    return path


def basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def scan_package(data: bytes, sensitive_patterns: tuple[str, ...] = DEFAULT_SENSITIVE_PATTERNS
                 ) -> tuple[list[str], list[str]]:
    """Return ``(errors, flags)`` for a candidate package file.

    Errors refuse the deployment. Flags are shown to the owner in the approval
    push (for example, the file mentions an arm/disarm helper) but do not by
    themselves refuse; the Deployer never calls any service on such entities.
    """
    errors: list[str] = []
    flags: list[str] = []
    if len(data) > MAX_FILE_BYTES:
        return ["FILE_TOO_LARGE"], flags
    if data.startswith(b"\xef\xbb\xbf"):
        errors.append("UTF8_BOM")
    if b"\x00" in data:
        return errors + ["NUL_BYTE"], flags
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return errors + ["NOT_UTF8"], flags
    if not text.strip():
        errors.append("EMPTY_FILE")

    top_keys: list[str] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if RE_DENIED_TAG.search(line):
            errors.append(f"DENIED_TAG@{lineno}")
        if line.startswith(("---", "...")):
            errors.append(f"MULTI_DOCUMENT@{lineno}")
            continue
        if line[0] not in " \t-":
            m = RE_TOP_KEY.match(line)  # prefix match by design
            if not m:
                errors.append(f"TOP_LEVEL_SYNTAX@{lineno}")
                continue
            key = m.group(1)
            top_keys.append(key)
            if key in DENIED_TOP_LEVEL_KEYS and not (key == "recorder" and _recorder_exclude(text) is not None):
                errors.append(f"DENIED_KEY:{key}")
    if len(top_keys) != len(set(top_keys)):
        errors.append("DUPLICATE_TOP_LEVEL_KEY")

    excluded = _recorder_exclude(text) if "recorder" in top_keys else None
    if excluded is not None:
        shown = ", ".join(excluded[:8]) + (f" +{len(excluded) - 8} more" if len(excluded) > 8 else "")
        flags.append(f"stops recording {len(excluded)} entities (deletes nothing): {shown}")

    for pattern in sensitive_patterns:
        rx = re.compile(pattern)
        if rx.search(text):
            flags.append(f"mentions {pattern}")
    if "!secret" in text:
        flags.append("uses !secret")
    return errors, flags


def _recorder_exclude(text: str) -> list[str] | None:
    """Return the excluded entity ids/globs when ``text`` is exactly the allowed recorder shape, else None.

    Allowed (comments and blank lines anywhere; any indentation that nests)::

        recorder:
          exclude:
            entities:
              - sensor.a
            entity_globs:
              - sensor.b_*

    Anything else (another top-level key, another recorder key, a value on a key line, flow style, a repeated
    section, an empty section, an inline comment, a tab, uneven list indentation, an item that is not exactly one
    entity id or glob) returns None, and ``recorder`` stays a denied key.
    """
    level, items, seen, counts = 0, [], set(), {}
    exclude_indent = section_indent = item_indent = -1
    section = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent == 0:
            if level or stripped != "recorder:":
                return None
            level = 1
        elif level == 1 and stripped == "exclude:":
            level, exclude_indent = 2, indent
        elif level >= 2 and stripped in ("entities:", "entity_globs:") and indent > exclude_indent:
            section = stripped[:-1]
            if section in counts or (level == 3 and indent != section_indent):
                return None
            level, section_indent, item_indent, counts[section] = 3, indent, -1, 0
        elif level == 3 and stripped.startswith("- ") and indent >= section_indent:
            if item_indent not in (-1, indent):
                return None
            item_indent = indent
            item = stripped[2:].strip()
            if section == "entities":
                ok = RE_RECORDER_ENTITY.fullmatch(item) is not None
            else:
                ok = RE_RECORDER_GLOB.fullmatch(item) is not None and "*" in item
            if not ok or item in seen:
                return None
            seen.add(item)
            items.append(item)
            counts[section] += 1
        else:
            return None
    if not items or len(items) > MAX_RECORDER_EXCLUDES or 0 in counts.values():
        return None
    return items
