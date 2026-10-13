"""Inactive local staging preparation. No App/runtime route or custody authority.

Accept only caller-held private directory descriptors. Read the fixed selected
component.tar (possibly gzip), never full backups or arbitrary paths. Directory
identity, capture provenance and exclusion of same-UID/privileged writers remain
caller/platform qualification gaps. Partial stages are retained, never reused.
"""
import hashlib
import json
import os
import re
import secrets
import stat

from .maintenance_legacy_snapshot import FILES, MAX_HISTORY, MAX_INPUT, PrivateSnapshot, Refusal, read_component

FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
MARKER = "COMMITTED.json"
MAX_MARKER = 4096


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _directory(fd):
    info = os.fstat(fd)
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.geteuid():
        raise Refusal("PRIVATE_DIRECTORY")


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _read(directory, name, limit):
    fd = os.open(name, FLAGS, dir_fd=directory)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_uid != os.geteuid()):
            raise Refusal("PRIVATE_FILE")
        if before.st_size > limit:
            raise Refusal("FILE_LIMIT")
        raw, offset = bytearray(), 0
        while True:
            chunk = os.pread(fd, min(65536, limit + 1 - offset), offset)
            if not chunk:
                break
            raw.extend(chunk)
            offset += len(chunk)
            if offset > limit:
                raise Refusal("FILE_LIMIT")
        after = os.fstat(fd)
        entry = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if _identity(before) != _identity(after) or _identity(after) != _identity(entry) or len(raw) != after.st_size:
            raise Refusal("FILE_CHANGED")
        return bytes(raw)
    finally:
        os.close(fd)


def capture_selected(directory, *, component_sha256, expected_files):
    """Stable bounded bytes, not authenticated source or real writer exclusion."""
    try:
        _directory(directory)
        return read_component(_read(directory, "component.tar", MAX_INPUT),
                              component_sha256=component_sha256, expected_files=expected_files)
    except OSError:
        raise Refusal("LOCAL_IO") from None


def _snapshot(value):
    if type(value) is not PrivateSnapshot or type(value._files) is not tuple:
        raise Refusal("SNAPSHOT_SHAPE")
    files = {}
    total = 0
    for pair in value._files:
        if (type(pair) is not tuple or len(pair) != 2 or type(pair[0]) is not str
                or pair[0] not in FILES or pair[0] in files or type(pair[1]) is not bytes):
            raise Refusal("SNAPSHOT_SHAPE")
        files[pair[0]] = pair[1]
        total += len(pair[1])
    if total > MAX_HISTORY:
        raise Refusal("SNAPSHOT_LIMIT")
    if type(value._component_sha256) is not str or not re.fullmatch("[0-9a-f]{64}", value._component_sha256):
        raise Refusal("SNAPSHOT_SHAPE")
    return PrivateSnapshot(tuple(sorted(files.items())), value._component_sha256)


def _write(directory, name, raw):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=directory)
    try:
        os.fchmod(fd, 0o600)
        offset = 0
        while offset < len(raw):
            count = os.write(fd, raw[offset:])
            if count <= 0:
                raise Refusal("WRITE_SHORT")
            offset += count
        os.fsync(fd)
    finally:
        os.close(fd)


def read_stage(directory, name):
    """Read a complete byte-consistent local stage; every authority flag is false.

    Marker presence cannot prove preceding fsync survived a power loss. No restart
    adoption, import eligibility, authenticated provenance or durability receipt.
    """
    if type(name) is not str or not re.fullmatch("stage-[0-9a-f]{32}", name):
        raise Refusal("STAGE_NAME")
    fd = None
    try:
        _directory(directory)
        fd = os.open(name, FLAGS | os.O_DIRECTORY, dir_fd=directory)
        _directory(fd)
        marker = _read(fd, MARKER, MAX_MARKER)
        # Exact canonical reconstruction rejects duplicates, unknown fields and
        # forged authority flags without carrying parser diagnostics to callers.
        try:
            receipt = json.loads(marker)
            digests = receipt["files"]
            component = receipt["component_sha256"]
        except (ValueError, TypeError, KeyError, RecursionError):
            raise Refusal("STAGE_MARKER") from None
        if type(digests) is not dict or set(digests) != set(FILES):
            raise Refusal("STAGE_MARKER")
        expected = {MARKER} | {key for key in FILES if digests[key] is not None}
        if set(os.listdir(fd)) != expected:
            raise Refusal("STAGE_INVENTORY")
        files, total = {}, 0
        for key in FILES:
            if digests[key] is not None:
                files[key] = _read(fd, key, MAX_HISTORY - total)
                total += len(files[key])
        snapshot = _snapshot(PrivateSnapshot(tuple(sorted(files.items())), component))
        if _canonical(snapshot.receipt()) != marker:
            raise Refusal("STAGE_SEAL")
        return snapshot
    except OSError:
        raise Refusal("LOCAL_IO") from None
    finally:
        if fd is not None:
            os.close(fd)


def stage_private(directory, snapshot):
    """Write a fresh private stage, sync it and independently read it back.

    No overwrite, deletion, rename, adoption/retry or shared live-history write.
    Returns an opaque stage basename only after successful fsync/readback. Failure
    leaves a quarantined directory; even a visible marker does not authorize use.
    """
    fd = None
    try:
        _directory(directory)
        snapshot = _snapshot(snapshot)
        name = "stage-" + secrets.token_hex(16)
        os.mkdir(name, 0o700, dir_fd=directory)
        fd = os.open(name, FLAGS | os.O_DIRECTORY, dir_fd=directory)
        os.fchmod(fd, 0o700)
        _directory(fd)
        os.fsync(directory)
        for key, raw in snapshot._files:
            _write(fd, key, raw)
        os.fsync(fd)
        _write(fd, MARKER, _canonical(snapshot.receipt()))
        os.fsync(fd)
        os.fsync(directory)
        checked = read_stage(directory, name)
        if checked.private_files() != snapshot.private_files() or checked.receipt() != snapshot.receipt():
            raise Refusal("STAGE_READBACK")
        return name
    except OSError:
        raise Refusal("LOCAL_IO") from None
    finally:
        if fd is not None:
            os.close(fd)
