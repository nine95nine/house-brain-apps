"""Inactive private component-reader prototype; not trusted capture or export.

Caller keeps component bytes on the Pi. No I/O, decryption, activation, restore
or import. Restrictive USTAR subset; unknown real backup formats fail closed.
Metadata/hash consistency is not fresh stopped-state, completeness or custody.
"""
from dataclasses import dataclass, field
import hashlib
import json
import re
import zlib

FILES = ("ledger.json", "installs.json", "audit.jsonl", "FROZEN.json", "txn.json")
MAX_INPUT = 16 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
MAX_HISTORY = 8 * 1024 * 1024
MAX_MEMBERS = 4096
MAX_METADATA = 128 * 1024
SLUG = "local_house_brain_deployer"


class Refusal(ValueError):
    """Fixed reason codes only, never private archive names or parser text."""


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise Refusal("METADATA_DUPLICATE")
        result[key] = value
    return result


def _metadata(raw):
    if len(raw) > MAX_METADATA:
        raise Refusal("METADATA_LIMIT")
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(Refusal("METADATA_NUMBER")))
        pending, count = [(value, 0)], 0
        while pending:
            item, depth = pending.pop()
            count += 1
            if depth > 64 or count > 32768:
                raise Refusal("METADATA_LIMIT")
            if isinstance(item, dict):
                pending.extend((v, depth + 1) for v in item.values())
            elif isinstance(item, list):
                pending.extend((v, depth + 1) for v in item)
        _canonical(value)
    except Refusal:
        raise
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise Refusal("METADATA_INVALID") from None
    if (not isinstance(value, dict) or value.get("version") != "0.3.6"
            or value.get("state") != "stopped"
            or not isinstance(value.get("user"), dict) or not isinstance(value.get("system"), dict)):
        raise Refusal("METADATA_IDENTITY")


def _text(raw):
    before, _, after = raw.partition(b"\0")
    if any(after):
        raise Refusal("HEADER_TEXT")
    try:
        return before.decode("utf-8")
    except UnicodeError:
        raise Refusal("HEADER_TEXT") from None


def _octal(raw):
    value = raw.strip(b" \0")
    if not value or not re.fullmatch(b"[0-7]+", value):
        raise Refusal("HEADER_NUMBER")
    return int(value, 8)


def _name(header):
    path, prefix = _text(header[:100]), _text(header[345:500])
    path = prefix + "/" + path if prefix else path
    if path.startswith("./"):
        path = path[2:]
    path = path[:-1] if path.endswith("/") else path
    if path in ("", "."):
        return "."
    if (path.startswith("/") or "\\" in path or len(path.encode()) > 512
            or any(ord(c) < 32 or ord(c) == 127 for c in path)
            or any(part in ("", ".", "..") for part in path.split("/"))):
        raise Refusal("MEMBER_PATH")
    return path


def _expanded(raw):
    if not raw.startswith(b"\x1f\x8b"):
        if len(raw) > MAX_EXPANDED:
            raise Refusal("EXPANDED_LIMIT")
        return raw
    try:
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        # Output allocation has an explicit cap even for a tiny compressed bomb.
        output = decoder.decompress(raw, MAX_EXPANDED + 1)
        if len(output) > MAX_EXPANDED or decoder.unconsumed_tail:
            raise Refusal("EXPANDED_LIMIT")
        if not decoder.eof or decoder.unused_data:
            raise Refusal("GZIP_BOUNDARY")
        return output
    except zlib.error:
        raise Refusal("GZIP_INVALID") from None


@dataclass(frozen=True)
class PrivateSnapshot:
    _files: tuple = field(repr=False)
    _component_sha256: str = field(repr=False)

    def private_files(self):
        return dict(self._files)

    def receipt(self):
        files = {name: _digest(dict(self._files)[name]) if name in dict(self._files) else None for name in FILES}
        identity = {"component_sha256": self._component_sha256, "source_slug": SLUG,
                    "snapshot_version": "0.3.6", "files": files}
        return {"schema": "hbm.legacy_snapshot.preparation.v1", **identity,
                "snapshot_sha256": _digest(_canonical(identity)),
                "history_bytes": sum(len(raw) for _, raw in self._files),
                "provenance": "UNATTESTED_INPUT", "verified": False, "stopped_verified": False,
                "backup_verified": False, "undo_verified": False, "applied": False,
                "native_execution": False, "uninstall_allowed": False}


def read_component(raw: bytes, *, component_sha256: str, expected_files: dict) -> PrivateSnapshot:
    """Read one already-selected, unencrypted private component, memory-only.

    expected_files declares all five SHA256/None presences. It is not a trusted
    inventory. No full-backup traversal or recovery-backup selection is supplied.
    History shape/conflict/chronology remain the Deployer staging/import gates.
    """
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_INPUT:
        raise Refusal("INPUT_LIMIT")
    if (not isinstance(component_sha256, str) or not re.fullmatch("[0-9a-f]{64}", component_sha256)
            or _digest(raw) != component_sha256):
        raise Refusal("COMPONENT_DIGEST")
    if (type(expected_files) is not dict or set(expected_files) != set(FILES)
            or any(v is not None and (not isinstance(v, str) or not re.fullmatch("[0-9a-f]{64}", v))
                   for v in expected_files.values())):
        raise Refusal("INVENTORY_SHAPE")
    expected_files = dict(expected_files)
    archive = _expanded(raw)
    if len(archive) % 512:
        raise Refusal("TAR_ALIGNMENT")
    offset, members, history_bytes = 0, 0, 0
    seen, files, metadata = {}, {}, None
    while offset + 512 <= len(archive):
        header = archive[offset:offset + 512]
        offset += 512
        if not any(header):
            if offset + 512 > len(archive) or any(archive[offset:]):
                raise Refusal("TAR_END")
            break
        members += 1
        if members > MAX_MEMBERS:
            raise Refusal("MEMBER_LIMIT")
        if header[257:265] != b"ustar\x0000":
            raise Refusal("TAR_FORMAT")
        check = sum(header[:148]) + 8 * 32 + sum(header[156:])
        if _octal(header[148:156]) != check:
            raise Refusal("HEADER_CHECKSUM")
        kind = header[156:157]
        if kind not in (b"0", b"\0", b"5") or any(header[157:257]):
            raise Refusal("MEMBER_TYPE")
        name, size = _name(header), _octal(header[124:136])
        if name in seen:
            raise Refusal("MEMBER_DUPLICATE")
        # Never admit a regular-file parent or a file that shadows existing children.
        parts = name.split("/")
        if (any(seen.get("/".join(parts[:i])) in (b"0", b"\0") for i in range(1, len(parts)))
                or (kind != b"5" and any(p.startswith(name + "/") for p in seen))):
            raise Refusal("MEMBER_HIERARCHY")
        seen[name] = kind
        if name == "." and kind != b"5":
            raise Refusal("MEMBER_PATH")
        if kind == b"5" and size:
            raise Refusal("DIRECTORY_PAYLOAD")
        padded = (size + 511) // 512 * 512
        if size > MAX_EXPANDED or offset + padded > len(archive):
            raise Refusal("MEMBER_SIZE")
        if name == "addon.json":
            if kind == b"5":
                raise Refusal("METADATA_INVALID")
            metadata = archive[offset:offset + min(size, MAX_METADATA + 1)]
        if name.startswith("data/") and name[5:] in FILES:
            if kind == b"5":
                raise Refusal("HISTORY_TYPE")
            history_bytes += size
            if history_bytes > MAX_HISTORY:
                raise Refusal("HISTORY_LIMIT")
            files[name[5:]] = archive[offset:offset + size]
        offset += padded
    else:
        raise Refusal("TAR_END")
    if metadata is None:
        raise Refusal("METADATA_MISSING")
    _metadata(metadata)
    for name in FILES:
        actual = _digest(files[name]) if name in files else None
        if actual != expected_files[name]:
            raise Refusal("INVENTORY_MISMATCH")
    return PrivateSnapshot(tuple(sorted(files.items())), component_sha256)
