"""Read-only local directory custody, independent of a common parent.

No grant, issuer, writer lease, installed-host attestation or activation is
provided here. A trusted producer must separately authenticate these observations.
Mount IDs are local to the observing mount namespace, not cross-App identities.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
import stat
import threading


class RootBindingError(RuntimeError):
    """A requested root cannot be pinned or its retained identity has changed."""


@dataclass(frozen=True)
class RootSpec:
    role: str
    path: str
    uid: int
    gid: int
    mode: int


@dataclass(frozen=True)
class RootIdentity:
    device: int
    inode: int
    uid: int
    gid: int
    mode: int
    mount: int


def _directory(path: str) -> int:
    # Walk every component relative to the held parent. O_NOFOLLOW on only the
    # leaf would still traverse a symlink in an ancestor.
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open('/', flags)
    try:
        for part in path.split('/')[1:]:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _identity(fd: int) -> RootIdentity:
    value = os.fstat(fd)
    if not stat.S_ISDIR(value.st_mode):
        raise RootBindingError('ROOT_NOT_DIRECTORY')
    with open(f'/proc/self/fdinfo/{fd}', encoding='ascii') as source:
        raw = source.read(4097)
    rows = [line.split(':', 1)[1].strip() for line in raw.splitlines()
            if line.startswith('mnt_id:')]
    if len(raw) > 4096 or len(rows) != 1 or not rows[0].isascii() or not rows[0].isdigit():
        raise RootBindingError('ROOT_MOUNT_UNAVAILABLE')
    return RootIdentity(value.st_dev, value.st_ino, value.st_uid, value.st_gid,
                        stat.S_IMODE(value.st_mode), int(rows[0]))


def _specs(specs: tuple[RootSpec, ...]) -> None:
    roles = {'data', 'working', 'uses', 'packages', 'writer'}
    if (type(specs) is not tuple or len(specs) != len(roles)
            or any(type(item) is not RootSpec or type(item.role) is not str for item in specs)
            or {item.role for item in specs} != roles):
        raise RootBindingError('ROOT_ROLES')
    for item in specs:
        path = item.path
        if (type(path) is not str or not path.startswith('/') or path == '/'
                or len(path) > 4096 or len(path.split('/')) > 64 or '\x00' in path
                or any(part in ('', '.', '..') for part in path.split('/')[1:])):
            raise RootBindingError('ROOT_PATH')
        if (any(type(v) is not int or v < 0 for v in (item.uid, item.gid, item.mode))
                or (item.role not in ('packages', 'data') and item.mode != 0o700)
                # Owner pop-up 2026-10-12 "Accept 0755 as observed": the installed App's /data is
                # 0755 (native self-report #454 6117189087). Exact observed mode only; never chmod-ed;
                # every child opened under it must still be 0700 (open_directory).
                or (item.role == 'data' and item.mode not in (0o700, 0o755))
                or (item.role == 'packages' and item.mode not in (0o700, 0o750, 0o755))):
            raise RootBindingError('ROOT_POLICY')
    # Nested state roots may be children of data. Packages and writer must be
    # separate from all other roles; working/uses must not contain one another.
    for i, left in enumerate(specs):
        for right in specs[i + 1:]:
            common = os.path.commonpath((left.path, right.path))
            nested_data = ((left.role == 'data' and common == left.path and right.role in {'working', 'uses'})
                           or (right.role == 'data' and common == right.path and left.role in {'working', 'uses'}))
            if left.path == right.path or (common in (left.path, right.path) and not nested_data):
                raise RootBindingError('ROOT_OVERLAP')


class RootBindings:
    """Retain explicit root descriptors; drift latches a hold until close/reopen.

    Observations belong to the opening process. Another thread must supply its
    own checked common WriterLease; bare cross-thread access still refuses.
    This is an observation guard, not atomic isolation. Mutators use descriptors
    operations under a separately checked common WriterLease; check-then-use of
    absolute paths alone cannot prevent replacement races.
    """

    def __init__(self, specs: tuple[RootSpec, ...]):
        _specs(specs)
        self._specs = specs
        self._fds: list[int] = []
        self._pins: list[RootIdentity] = []
        self._owner = (os.getpid(), threading.get_ident())
        self._held = False
        self._closed = False
        try:
            for item in specs:
                fd = _directory(item.path)
                self._fds.append(fd)
                identity = _identity(fd)
                if (identity.uid, identity.gid, identity.mode) != (item.uid, item.gid, item.mode):
                    raise RootBindingError('ROOT_POLICY_MISMATCH')
                if any((p.device, p.inode) == (identity.device, identity.inode) for p in self._pins):
                    raise RootBindingError('ROOT_ALIAS')
                self._pins.append(identity)
            self.check()
        except OSError as error:
            self.close()
            raise RootBindingError('ROOT_UNAVAILABLE') from error
        except BaseException:
            self.close()
            raise

    def check(self, *, fence=None) -> None:
        if os.getpid() != self._owner[0]:
            raise RootBindingError('ROOT_OWNER')
        if threading.get_ident() != self._owner[1]:
            from .writer import WriterLease
            if type(fence) is not WriterLease:
                raise RootBindingError('ROOT_OWNER')
            fence.check()
            if fence.root != next(s.path for s in self._specs if s.role == 'writer'):
                raise RootBindingError('ROOT_OWNER')
        if self._closed or self._held:
            raise RootBindingError('ROOT_HELD')
        try:
            for item, fd, pin in zip(self._specs, self._fds, self._pins, strict=True):
                if _identity(fd) != pin:
                    raise RootBindingError('ROOT_IDENTITY_LOST')
                visible = _directory(item.path)
                try:
                    if _identity(visible) != pin:
                        raise RootBindingError('ROOT_IDENTITY_LOST')
                finally:
                    os.close(visible)
        except (OSError, RootBindingError) as error:
            self._held = True
            raise RootBindingError('ROOT_IDENTITY_LOST') from error

    def path(self, role: str, *, fence=None) -> str:
        self.check(fence=fence)
        if type(role) is not str:
            raise RootBindingError('ROOT_ROLE')
        for spec in self._specs:
            if spec.role == role:
                return spec.path
        raise RootBindingError('ROOT_ROLE')

    def open_directory(self, role: str, parts: tuple[str, ...] = (), *, fence=None) -> int:
        """Return a caller-owned read-only descriptor below the retained root.

        This does not grant mutation permission. Descendants must remain private
        and have the root's explicit owner; package descendants are forbidden.
        """
        self.path(role, fence=fence)
        if (type(parts) is not tuple or len(parts) > 4
                or (role == 'packages' and parts)
                or any(type(p) is not str or not p or p in ('.', '..')
                       or '/' in p or '\x00' in p or len(p) > 255 for p in parts)):
            raise RootBindingError('ROOT_CHILD_PATH')
        index = next(i for i, item in enumerate(self._specs) if item.role == role)
        spec = self._specs[index]
        fd = os.dup(self._fds[index])
        try:
            for part in parts:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=fd)
                os.close(fd)
                fd = child
                identity = _identity(fd)
                if (identity.uid, identity.gid, identity.mode, identity.mount) != (
                        spec.uid, spec.gid, 0o700, self._pins[index].mount):
                    raise RootBindingError('ROOT_CHILD_POLICY')
            self.check(fence=fence)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def evidence(self, *, fence=None) -> bytes:
        self.check(fence=fence)
        return json.dumps({'schema': 'hbd.local_root_bindings.v1',
                           'authority': 'none',
                           'roots': [{'spec': asdict(s), 'identity': asdict(p)}
                                     for s, p in zip(self._specs, self._pins, strict=True)]},
                          sort_keys=True, separators=(',', ':')).encode('utf-8')

    def close(self) -> None:
        self._closed = True
        while self._fds:
            os.close(self._fds.pop())

    def __enter__(self) -> RootBindings:
        self.check()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()
