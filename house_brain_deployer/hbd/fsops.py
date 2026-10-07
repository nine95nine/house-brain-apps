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
        _fsync_dir(self.root)

    def unlink_if(self, path: str, expected_sha: str | None) -> None:
        current = file_sha(path)
        if current is None:
            return
        if expected_sha is not None and current != expected_sha:
            raise FsError("UNEXPECTED_CONTENT", basename(path))
        os.unlink(path)
        _fsync_dir(self.root)

    def replace_with(self, stage: str, target: str) -> None:
        os.replace(stage, target)
        _fsync_dir(self.root)
