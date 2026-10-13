"""Inactive descriptor package effects; no Engine hookup or production authority.

All effects require held common-writer enrollment and a caller-supplied fresh
phase guard plus exact content expectations. HTTP/config-root paths are excluded.
Backup publication copies bytes exclusively instead of sharing a mutable inode.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat

from ..policy import MAX_FILE_BYTES, check_package_target, check_backup_path
from ..fsops import Packages
from .deployer_bound_files import BoundFileError, _BoundDirectory, _signature

_STAGE = re.compile(r'\.hbd_stage_([a-z0-9][a-z0-9-]{2,63}(?:#undo)?)_([0-9]{1,4})\.tmp(?:\.restore)?\Z')


def _sha(raw):
    return None if raw is None else hashlib.sha256(raw).hexdigest()


def _digest(value):
    if type(value) is not str or re.fullmatch(r'[0-9a-f]{64}', value) is None:
        raise BoundFileError('BOUND_PACKAGE_DIGEST')


class BoundPackageFiles(_BoundDirectory, Packages):
    """Descriptor effects prepared for explicit Engine integration after handback.

    This cannot replace Packages unchanged: Engine must use this file_sha and
    provide its planned source/stage/old-target hashes to each effect. No active
    runtime constructs this class. A sticky failure preserves partial files and
    prohibits all later operations through this instance; never auto-roll back.
    """

    def __init__(self, roots, lease, guard):
        super().__init__(roots, lease, 'packages', (), guard, (0o600, 0o644), MAX_FILE_BYTES)
        self.root = roots.path('packages', fence=lease)
        self.mutation_guard = lease.check

    @staticmethod
    def _name(name):
        if type(name) is not str or '/' in name or len(name) > 255:
            raise BoundFileError('BOUND_PACKAGE_PATH')
        if _STAGE.fullmatch(name):
            return
        try:
            check_package_target('/config/packages/' + name)
        except ValueError:
            try:
                check_backup_path('/config/packages/' + name)
            except ValueError:
                raise BoundFileError('BOUND_PACKAGE_PATH') from None

    def _leaf(self, path):
        if type(path) is not str:
            raise BoundFileError('BOUND_PACKAGE_PATH')
        prefix = (self.root + '/') if path.startswith(self.root + '/') else '/config/packages/'
        if not path.startswith(prefix):
            raise BoundFileError('BOUND_PACKAGE_PATH')
        name = path[len(prefix):]
        self._name(name)
        if prefix == '/config/packages/' and name.startswith('.'):
            raise BoundFileError('BOUND_PACKAGE_PATH')
        return name

    def local(self, owner_path):
        self._check()
        name = self._leaf(owner_path)
        return os.path.join(self.root, name)

    def stage_path(self, request_id, index):
        if (type(request_id) is not str or re.fullmatch(r'[a-z0-9][a-z0-9-]{2,63}(?:#undo)?', request_id) is None
                or type(index) is not int or not 0 <= index <= 9999):
            raise BoundFileError('BOUND_PACKAGE_STAGE')
        self._check()
        return os.path.join(self.root, f'.hbd_stage_{request_id}_{index}.tmp')

    def file_sha(self, path):
        return _sha(self.read(self._leaf(path)))

    def _check(self):
        super()._check()
        guard = self.mutation_guard
        if guard is not None:
            try:
                guard()
                if self.mutation_guard is not guard:
                    raise BoundFileError('BOUND_DEPENDENCY_DRIFT')
                super()._check()
            except Exception:  # noqa: BLE001 - recovery callback refusal holds descriptor effects
                self._held = True
                raise BoundFileError('BOUND_GUARD_LOST') from None

    def snapshot(self, owner_paths):
        return {path: self.file_sha(path) for path in owner_paths}

    def check_writer(self):
        self._check()

    def check_root(self):
        self._check()

    def read_bytes(self, path):
        raw = self.read(self._leaf(path))
        if raw is None:
            raise BoundFileError('BOUND_PACKAGE_SOURCE')
        return raw

    def space_named_files(self):
        self._check()
        names = sorted(name for name in os.listdir(self._fd) if ' ' in name)
        self._check()
        return names

    def _new(self, name, raw, mode):
        """Exclusive bounded publication; never truncates or cleans failed intent."""
        fd = None
        try:
            self._check()
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                         | os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=self._fd)
            pin = _signature(os.fstat(fd))
            offset = 0
            while offset < len(raw):
                self._check()
                if (_signature(os.fstat(fd)) != pin
                        or _signature(os.stat(name, dir_fd=self._fd, follow_symlinks=False)) != pin):
                    raise BoundFileError('BOUND_PACKAGE_STAGE_DRIFT')
                written = os.write(fd, raw[offset:])
                if written <= 0:
                    raise BoundFileError('BOUND_SHORT_WRITE')
                offset += written
                pin = _signature(os.fstat(fd))
            self._check()
            if (_signature(os.fstat(fd)) != pin
                    or _signature(os.stat(name, dir_fd=self._fd, follow_symlinks=False)) != pin):
                raise BoundFileError('BOUND_PACKAGE_STAGE_DRIFT')
            os.fchmod(fd, mode)  # Only the newly exclusive-created file.
            self._check()
            os.fsync(fd)
            self._check()
            os.fsync(self._fd)
            self._check()
            after, after_pin = self._read(name)
            if after != raw or after_pin[:2] != pin[:2] or stat.S_IMODE(after_pin[2]) != mode:
                raise BoundFileError('BOUND_PACKAGE_READBACK')
        finally:
            if fd is not None:
                os.close(fd)

    def write_stage(self, stage, data, sha):
        name = self._leaf(stage)
        _digest(sha)
        if (_STAGE.fullmatch(name) is None or type(data) is not bytes
                or len(data) > MAX_FILE_BYTES or _sha(data) != sha):
            raise BoundFileError('BOUND_PACKAGE_STAGE')
        try:
            self._check()
            current, current_pin = self._read(name)
            if current is not None:
                if current != data:
                    raise BoundFileError('BOUND_PACKAGE_STAGE_EXISTS')
                # An exact already-durable stage can be adopted only under the
                # caller's authenticated recovery/intent guard. Sync adoption
                # again without truncation; prior crash durability is unknown.
                self._check()
                if self._read(name) != (data, current_pin):
                    raise BoundFileError('BOUND_PACKAGE_STAGE_DRIFT')
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                             dir_fd=self._fd)
                try:
                    if _signature(os.fstat(fd)) != current_pin:
                        raise BoundFileError('BOUND_PACKAGE_STAGE_DRIFT')
                    self._check()
                    os.fsync(fd)
                    self._check()
                    os.fsync(self._fd)
                    self._check()
                    if self._read(name) != (data, current_pin):
                        raise BoundFileError('BOUND_PACKAGE_STAGE_DRIFT')
                finally:
                    os.close(fd)
                return
            self._new(name, data, 0o644)
        except Exception:  # noqa: BLE001 - phase callbacks fail closed to fixed codes
            self._held = True
            raise BoundFileError('BOUND_PACKAGE_WRITE_FAILED') from None

    def link_noclobber(self, src, dst, *, expected_sha):
        """API name preserves the intended no-clobber effect; actual copy, no link."""
        source, destination = self._leaf(src), self._leaf(dst)
        _digest(expected_sha)
        if source == destination:
            raise BoundFileError('BOUND_PACKAGE_PATH')
        try:
            self._check()
            raw, pin = self._read(source)
            if _sha(raw) != expected_sha:
                raise BoundFileError('BOUND_PACKAGE_SOURCE')
            self._check()
            if self._read(source) != (raw, pin):
                raise BoundFileError('BOUND_PACKAGE_SOURCE_DRIFT')
            self._new(destination, raw, stat.S_IMODE(pin[2]))
            self._check()
            if self._read(source) != (raw, pin):
                raise BoundFileError('BOUND_PACKAGE_SOURCE_DRIFT')
        except Exception:  # noqa: BLE001 - phase callbacks fail closed to fixed codes
            self._held = True
            raise BoundFileError('BOUND_PACKAGE_COPY_FAILED') from None

    def unlink_if(self, path, expected_sha):
        name = self._leaf(path)
        if expected_sha is None:
            if _STAGE.fullmatch(name) is None:
                raise BoundFileError('BOUND_PACKAGE_DIGEST')
        else:
            _digest(expected_sha)
        try:
            self._check()
            raw, pin = self._read(name)
            if raw is not None and expected_sha is not None and _sha(raw) != expected_sha:
                raise BoundFileError('BOUND_PACKAGE_SOURCE')
            self._check()
            if self._read(name) != (raw, pin):
                raise BoundFileError('BOUND_PACKAGE_SOURCE_DRIFT')
            if raw is not None:
                os.unlink(name, dir_fd=self._fd)
                self._check()
                os.fsync(self._fd)
            self._check()
            if self._read(name) != (None, None):
                raise BoundFileError('BOUND_PACKAGE_READBACK')
        except Exception:  # noqa: BLE001 - phase callbacks fail closed to fixed codes
            self._held = True
            raise BoundFileError('BOUND_PACKAGE_UNLINK_FAILED') from None

    def replace_with(self, stage, target, *, expected_stage, expected_target):
        source, destination = self._leaf(stage), self._leaf(target)
        _digest(expected_stage)
        _digest(expected_target)
        if _STAGE.fullmatch(source) is None or source == destination:
            raise BoundFileError('BOUND_PACKAGE_STAGE')
        try:
            check_package_target('/config/packages/' + destination)
        except ValueError:
            raise BoundFileError('BOUND_PACKAGE_PATH') from None
        try:
            self._check()
            raw, source_pin = self._read(source)
            old, target_pin = self._read(destination)
            if _sha(raw) != expected_stage or _sha(old) != expected_target:
                raise BoundFileError('BOUND_PACKAGE_SOURCE')
            self._check()
            if self._read(source) != (raw, source_pin) or self._read(destination) != (old, target_pin):
                raise BoundFileError('BOUND_PACKAGE_SOURCE_DRIFT')
            os.replace(source, destination, src_dir_fd=self._fd, dst_dir_fd=self._fd)
            self._check()
            os.fsync(self._fd)
            self._check()
            after, after_pin = self._read(destination)
            if (after != raw or after_pin[:2] != source_pin[:2]
                    or self._read(source) != (None, None)):
                raise BoundFileError('BOUND_PACKAGE_READBACK')
        except Exception:  # noqa: BLE001 - phase callbacks fail closed to fixed codes
            self._held = True
            raise BoundFileError('BOUND_PACKAGE_REPLACE_FAILED') from None
