"""Journaled, no-clobber file operations under the packages directory.

All paths arrive already validated by ``policy``; this module re-checks the
resolved location, refuses symlinks, never overwrites an existing file (hard
link + unlink gives a no-clobber rename) and records every step in the
journal *before* performing it so that recovery can always undo it.
"""
from __future__ import annotations

import errno
import hashlib
import os
import stat
from dataclasses import dataclass

from .policy import basename


class FsError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def file_sha(path: str) -> str | None:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if not os.path.isfile(path) or os.path.islink(path) or st.st_nlink < 1:
        raise FsError("NOT_REGULAR_FILE", basename(path))
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass
class Packages:
    """Maps owner-visible ``/config/packages/x`` paths into the container mount."""

    root: str  # e.g. /homeassistant/packages inside the App container

    def check_root(self) -> None:
        if os.path.islink(self.root) or not os.path.isdir(self.root):
            raise FsError("PACKAGES_ROOT")

    def local(self, owner_path: str) -> str:
        from .http_cleanup import TARGET, RE_BACKUP
        if owner_path == TARGET or RE_BACKUP.fullmatch(owner_path):
            config_root = os.path.dirname(self.root)
            if os.path.islink(config_root) or os.path.realpath(config_root) != os.path.abspath(config_root):
                raise FsError("CONFIG_ROOT_UNSAFE")
            local = os.path.join(config_root, basename(owner_path))
            if os.path.islink(local):
                raise FsError("SYMLINK")
            return local
        name = basename(owner_path)
        if "/" in name or name in ("", ".", ".."):
            raise FsError("PATH")
        local = os.path.join(self.root, name)
        if os.path.dirname(os.path.realpath(local)) != os.path.realpath(self.root):
            raise FsError("PATH_ESCAPE", name)
        if os.path.islink(local):
            raise FsError("SYMLINK", name)
        return local

    def stage_path(self, request_id: str, index: int) -> str:
        # Hidden, non-.yaml: never loaded by the package include.
        return os.path.join(self.root, f".hbd_stage_{request_id}_{index}.tmp")

    def snapshot(self, owner_paths: list[str]) -> dict[str, str | None]:
        return {p: file_sha(self.local(p)) for p in owner_paths}

    def space_named_files(self) -> list[str]:
        return sorted(n for n in os.listdir(self.root) if " " in n)

    # -- primitive steps (each idempotent for recovery) ----------------------
    def write_stage(self, stage: str, data: bytes, sha: str) -> None:
        tmp = stage
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        if file_sha(tmp) != sha:
            os.unlink(tmp)
            raise FsError("STAGE_HASH")

    def link_noclobber(self, src: str, dst: str) -> None:
        from .http_cleanup import TARGET, read_configuration
        if src == os.path.join(os.path.dirname(self.root), basename(TARGET)):
            # Root-config backups must not share an inode with an editor's live file.
            # An in-place edit otherwise destroys both the live baseline and its .bak.
            info = os.stat(src, follow_symlinks=False)
            fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, stat.S_IMODE(info.st_mode))
            try:
                data = read_configuration(self)
                with os.fdopen(fd, "wb", closefd=False) as out:
                    out.write(data)
                    out.flush()
                os.fchmod(fd, stat.S_IMODE(info.st_mode))
                os.fsync(fd)
            finally:
                os.close(fd)
            _fsync_dir(os.path.dirname(dst))
            return
        try:
            os.link(src, dst)
        except FileExistsError:
            raise FsError("EXISTS", basename(dst)) from None
        except OSError as err:
            if err.errno not in (errno.EPERM, errno.EXDEV, errno.ENOTSUP, errno.EOPNOTSUPP):
                raise
            # Filesystems without hard links: exclusive-create copy.
            with open(src, "rb") as fh:
                data = fh.read()
            try:
                fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                raise FsError("EXISTS", basename(dst)) from None
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
        _fsync_dir(os.path.dirname(dst))

    def unlink_if(self, path: str, expected_sha: str | None) -> None:
        current = file_sha(path)
        if current is None:
            return
        if expected_sha is not None and current != expected_sha:
            raise FsError("UNEXPECTED_CONTENT", basename(path))
        os.unlink(path)
        _fsync_dir(os.path.dirname(path))

    def replace_with(self, stage: str, target: str) -> None:
        os.replace(stage, target)
        _fsync_dir(os.path.dirname(target))
        if os.path.dirname(stage) != os.path.dirname(target):
            _fsync_dir(os.path.dirname(stage))

