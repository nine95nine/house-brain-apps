"""Inactive descriptor I/O preparation; no issuer, Journal adapter or activation.

The caller must supply a fresh effect-specific guard. Directory observations and
this primitive confer no authority. All writes stay relative to the selected
descriptor; the common lease excludes cooperating writers only. Failure holds
this instance and preserves partial files for separately authorized recovery.
"""
from __future__ import annotations

import os
import secrets
import stat

from ..history import FILES, MAX_BYTES, MAX_EVENTS, _json
from ..root_bindings import RootBindings
from ..writer import WriterLease


class BoundFileError(RuntimeError):
    """Fixed codes only; never expose private file bytes or paths."""


def _signature(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_uid,
            value.st_gid, value.st_nlink, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns)


class _BoundDirectory:
    """Shared observation/lease/phase and descriptor-read mechanics, no grant."""

    def __init__(self, roots, lease, role, parts, guard, modes, limit):
        if (type(roots) is not RootBindings or type(lease) is not WriterLease
                or not callable(guard)):
            raise BoundFileError('BOUND_INPUT')
        self._roots, self._lease, self._guard = roots, lease, guard
        self._role, self._parts = role, parts
        self._modes, self._limit = modes, limit
        self._fd = None
        self._held = False
        self._evidence = roots.evidence(fence=lease)
        self._session = lease.session
        self._dependencies = (roots, lease, guard, role, parts, modes, limit, self._evidence)
        self._pin = None
        try:
            self._check()
            self._fd = roots.open_directory(self._role, self._parts, fence=lease)
            self._pin = self._directory_pin(self._fd)
            self._check()
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _directory_pin(fd):
        value = os.fstat(fd)
        return (value.st_dev, value.st_ino, value.st_uid, value.st_gid,
                stat.S_IMODE(value.st_mode))

    def _check(self):
        if self._held:
            raise BoundFileError('BOUND_HELD')
        try:
            def pinned():
                roots, lease, guard, role, parts, modes, limit, evidence = self._dependencies
                if (self._roots is not roots or self._lease is not lease or self._guard is not guard
                        or lease.session != self._session
                        or (self._role, self._parts, self._modes, self._limit, self._evidence)
                        != (role, parts, modes, limit, evidence)):
                    raise BoundFileError('BOUND_DEPENDENCY_DRIFT')
            pinned()
            self._lease.check()
            if self._lease.root != self._roots.path('writer', fence=self._lease):
                raise BoundFileError('BOUND_WRITER_ROOT')
            self._guard()
            pinned()
            # The phase callback may revoke/replace roots or the held lock.
            self._lease.check()
            if self._roots.evidence(fence=self._lease) != self._evidence:
                raise BoundFileError('BOUND_ROOT_DRIFT')
            if self._pin is not None:
                visible = self._roots.open_directory(self._role, self._parts, fence=self._lease)
                try:
                    if (self._directory_pin(visible) != self._pin
                            or self._directory_pin(self._fd) != self._pin):
                        raise BoundFileError('BOUND_SELECTED_DRIFT')
                finally:
                    os.close(visible)
        except Exception:  # noqa: BLE001 - phase callbacks fail closed to fixed codes
            self._held = True
            raise BoundFileError('BOUND_GUARD_LOST') from None

    def _read(self, name):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                         dir_fd=self._fd)
        except FileNotFoundError:
            return None, None
        try:
            before = os.fstat(fd)
            directory = os.fstat(self._fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or (before.st_uid, before.st_gid) != (directory.st_uid, directory.st_gid)
                    or stat.S_IMODE(before.st_mode) not in self._modes
                    or before.st_size > self._limit):
                raise BoundFileError('BOUND_FILE_POLICY')
            chunks, size = [], 0
            while True:
                raw = os.read(fd, min(65536, self._limit + 1 - size))
                if not raw:
                    break
                chunks.append(raw)
                size += len(raw)
                if size > self._limit:
                    raise BoundFileError('BOUND_FILE_LIMIT')
            after = os.fstat(fd)
            visible = os.stat(name, dir_fd=self._fd, follow_symlinks=False)
            if (_signature(before) != _signature(after)
                    or _signature(before) != _signature(visible) or size != before.st_size):
                raise BoundFileError('BOUND_FILE_DRIFT')
            return b''.join(chunks), _signature(before)
        finally:
            os.close(fd)

    def read(self, name):
        self._name(name)
        try:
            self._check()
            raw, pin = self._read(name)
            self._check()
            after, after_pin = self._read(name)
            if raw != after or pin != after_pin:
                raise BoundFileError('BOUND_FILE_DRIFT')
            return raw
        except Exception:  # noqa: BLE001 - phase callbacks fail closed to fixed codes
            self._held = True
            raise BoundFileError('BOUND_READ_FAILED') from None

    def close(self):
        self._held = True
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self):
        self._check()
        return self

    def __exit__(self, *_exc):
        self.close()


class BoundStateFiles(_BoundDirectory):
    """Bounded descriptor reads, state replacement, audit append and clearing.

    Selector publication remains unsupported here. A guard is required even
    for reads; it must authenticate the selected generation, scope and phase,
    not merely return a root digest. No runtime construction is enabled yet.
    """

    def __init__(self, roots: RootBindings, lease: WriterLease, working: str, guard):
        if (type(working) is not str or len(working) != 66 or not working.startswith('w-')
                or any(c not in '0123456789abcdef' for c in working[2:])):
            raise BoundFileError('BOUND_INPUT')
        super().__init__(roots, lease, 'working', (working, 'journal'), guard, (0o600,), MAX_BYTES)

    @staticmethod
    def _name(name):
        if type(name) is not str or name not in FILES:
            raise BoundFileError('BOUND_FILE_NAME')

    def replace(self, name: str, raw: bytes, *, expected: bytes | None):
        """Publish bytes only if the prior selected state is still exact.

        Successful return includes file sync, directory sync, guard recheck and
        exact readback. Failure after rename can mean publication occurred;
        callers must hold, inspect and recover, never blindly retry or roll back.
        MAX_BYTES bounds one operation, never permits pruning audit/history.
        """
        self._name(name)
        if (name == 'audit.jsonl' or type(raw) is not bytes or len(raw) > MAX_BYTES
                or (expected is not None and (type(expected) is not bytes or len(expected) > MAX_BYTES))):
            raise BoundFileError('BOUND_REPLACEMENT_INPUT')
        fd = None
        try:
            self._check()
            before, pin = self._read(name)
            if before != expected:
                raise BoundFileError('BOUND_EXPECTED_STATE')
            temporary = '.bound-state-' + secrets.token_hex(16)
            self._check()
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self._fd)
            offset = 0
            while offset < len(raw):
                self._check()
                written = os.write(fd, raw[offset:])
                if written <= 0:
                    raise BoundFileError('BOUND_SHORT_WRITE')
                offset += written
            self._check()
            os.fsync(fd)
            written_pin = _signature(os.fstat(fd))
            self._check()
            current, current_pin = self._read(name)
            staged, staged_pin = self._read(temporary)
            if (current != expected or current_pin != pin
                    or staged != raw or staged_pin != written_pin):
                raise BoundFileError('BOUND_PUBLICATION_DRIFT')
            # No callback between the final target/staging comparison and the
            # descriptor rename. Nonparticipants can still race a leaf name;
            # shared-writer enrollment is a separate prerequisite.
            os.replace(temporary, name, src_dir_fd=self._fd, dst_dir_fd=self._fd)
            self._check()
            os.fsync(self._fd)
            self._check()
            published, published_pin = self._read(name)
            # Rename may change ctime, but must retain the exact staged inode.
            if published != raw or published_pin[:2] != written_pin[:2]:
                raise BoundFileError('BOUND_READBACK')
        except Exception:  # noqa: BLE001 - phase callbacks fail closed to fixed codes
            self._held = True
            raise BoundFileError('BOUND_PUBLICATION_FAILED') from None
        finally:
            if fd is not None:
                os.close(fd)

    def append_audit(self, line: bytes, *, expected: bytes | None):
        """Append one already-redacted JSON record, preserving every prior byte.

        The caller owns event semantics/redaction and durable effect intent. A
        partial append or post-write refusal is ambiguous and holds; no trimming
        or replay follows. At either cap, refuse before changing audit history.
        """
        if (type(line) is not bytes or not 1 < len(line) <= 65536
                or not line.endswith(b'\n') or b'\n' in line[:-1]
                or (expected is not None and type(expected) is not bytes)):
            raise BoundFileError('BOUND_AUDIT_INPUT')
        try:
            if type(_json(line[:-1])) is not dict:
                raise BoundFileError('BOUND_AUDIT_INPUT')
        except ValueError:
            raise BoundFileError('BOUND_AUDIT_INPUT') from None
        fd = None
        try:
            self._check()
            before, pin = self._read('audit.jsonl')
            if before != expected:
                raise BoundFileError('BOUND_EXPECTED_STATE')
            prefix = before or b''
            if (prefix and not prefix.endswith(b'\n')):
                raise BoundFileError('BOUND_AUDIT_PARTIAL')
            if len(prefix) + len(line) > MAX_BYTES or prefix.count(b'\n') >= MAX_EVENTS:
                raise BoundFileError('BOUND_AUDIT_CAP')
            self._check()
            current, current_pin = self._read('audit.jsonl')
            if current != before or current_pin != pin:
                raise BoundFileError('BOUND_AUDIT_DRIFT')
            flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            if before is None:
                flags |= os.O_CREAT | os.O_EXCL
            fd = os.open('audit.jsonl', flags, 0o600, dir_fd=self._fd)
            opened = _signature(os.fstat(fd))
            # Existing append must retain exactly the read inode/stat. Newly
            # created audit is subsequently checked by the same read policy.
            if pin is not None and opened != pin:
                raise BoundFileError('BOUND_AUDIT_DRIFT')
            current, current_pin = self._read('audit.jsonl')
            if current != prefix or current_pin != opened:
                raise BoundFileError('BOUND_AUDIT_DRIFT')
            write_pin = opened
            offset = 0
            while offset < len(line):
                self._check()
                held = _signature(os.fstat(fd))
                visible = _signature(os.stat('audit.jsonl', dir_fd=self._fd, follow_symlinks=False))
                if held != write_pin or visible != held or held[6] != len(prefix) + offset:
                    raise BoundFileError('BOUND_AUDIT_DRIFT')
                written = os.write(fd, line[offset:])
                if written <= 0:
                    raise BoundFileError('BOUND_SHORT_WRITE')
                offset += written
                write_pin = _signature(os.fstat(fd))
            self._check()
            os.fsync(fd)
            self._check()
            os.fsync(self._fd)
            self._check()
            published, published_pin = self._read('audit.jsonl')
            if published != prefix + line or published_pin[:2] != opened[:2]:
                raise BoundFileError('BOUND_AUDIT_READBACK')
        except Exception:  # noqa: BLE001 - phase callbacks fail closed to fixed codes
            self._held = True
            raise BoundFileError('BOUND_AUDIT_FAILED') from None
        finally:
            if fd is not None:
                os.close(fd)

    def clear(self, name: str, *, expected: bytes | None):
        """Clear only a selected transaction/freeze after exact prior-state check.

        This is not history retention or an authority to clear pending intent.
        The phase guard must authorize the lifecycle transition separately.
        """
        if (type(name) is not str or name not in ('txn.json', 'FROZEN.json')
                or (expected is not None and type(expected) is not bytes)):
            raise BoundFileError('BOUND_CLEAR_INPUT')
        try:
            self._check()
            before, pin = self._read(name)
            if before != expected:
                raise BoundFileError('BOUND_EXPECTED_STATE')
            self._check()
            current, current_pin = self._read(name)
            if current != before or current_pin != pin:
                raise BoundFileError('BOUND_CLEAR_DRIFT')
            if before is not None:
                os.unlink(name, dir_fd=self._fd)
                self._check()
                os.fsync(self._fd)
            self._check()
            if self._read(name) != (None, None):
                raise BoundFileError('BOUND_CLEAR_READBACK')
        except Exception:  # noqa: BLE001 - phase callbacks fail closed to fixed codes
            self._held = True
            raise BoundFileError('BOUND_CLEAR_FAILED') from None
