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
            if key in DENIED_TOP_LEVEL_KEYS:
                errors.append(f"DENIED_KEY:{key}")
    if len(top_keys) != len(set(top_keys)):
        errors.append("DUPLICATE_TOP_LEVEL_KEY")

    for pattern in sensitive_patterns:
        rx = re.compile(pattern)
        if rx.search(text):
            flags.append(f"mentions {pattern}")
    if "!secret" in text:
        flags.append("uses !secret")
    return errors, flags
