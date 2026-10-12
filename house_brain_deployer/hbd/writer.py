"""Retained-inode writer lease; staged Service binding, not enabled at startup.

Participants must hold this lease for the whole operation and call check before
every mutation. Advisory locks do not constrain nonparticipants. Never unlink
the control file, even after a crash. No heartbeat expiry grants ownership.
"""
from __future__ import annotations

import fcntl
import os
import stat
import threading

LOCK_NAME = ".house_brain_writer_v1.lock"


class WriterError(RuntimeError):
    pass


class WriterLease:
    def __init__(self, root: str, *, create: bool = True) -> None:
        # #985 enrollment plan R1: the installed (bound) path passes create=False, so a missing lock
        # refuses (WRITER_NOT_ENROLLED) instead of being created at runtime. enroll_writer() creates it.
        self.root = os.path.abspath(root)
        self._create = create is True
        self._directory: int | None = None
        self._file: int | None = None
        self._owner: tuple[int, int] | None = None
        self.session = 0

    @staticmethod
    def _valid_file(value: os.stat_result) -> bool:
        return (stat.S_ISREG(value.st_mode) and value.st_nlink == 1
                and value.st_size == 0 and stat.S_IMODE(value.st_mode) == 0o600)

    def check(self) -> None:
        if self._directory is None or self._file is None:
            raise WriterError("WRITER_NOT_HELD")
        if self._owner != (os.getpid(), threading.get_ident()):
            raise WriterError('WRITER_OWNER')
        try:
            directory = os.fstat(self._directory)
            visible_root = os.stat(self.root, follow_symlinks=False)
            held = os.fstat(self._file)
            visible = os.stat(LOCK_NAME, dir_fd=self._directory, follow_symlinks=False)
            safe = (os.path.realpath(self.root) == self.root
                    and stat.S_ISDIR(visible_root.st_mode)
                    and (directory.st_dev, directory.st_ino)
                    == (visible_root.st_dev, visible_root.st_ino)
                    and self._valid_file(held) and self._valid_file(visible)
                    and (held.st_uid, held.st_gid) == (directory.st_uid, directory.st_gid)
                    and (visible.st_uid, visible.st_gid) == (held.st_uid, held.st_gid)
                    and (held.st_dev, held.st_ino) == (visible.st_dev, visible.st_ino))
        except OSError as err:
            raise WriterError("WRITER_IDENTITY_LOST") from err
        if not safe:
            raise WriterError("WRITER_IDENTITY_LOST")

    def __enter__(self) -> WriterLease:
        if self._directory is not None or self._file is not None:
            raise WriterError("WRITER_ALREADY_HELD")
        try:
            if os.path.realpath(self.root) != self.root:
                raise WriterError("WRITER_ROOT_UNSAFE")
            self._directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_CREAT if self._create else 0)
            try:
                self._file = os.open(LOCK_NAME, flags, 0o600, dir_fd=self._directory)
            except FileNotFoundError as err:
                raise WriterError("WRITER_NOT_ENROLLED") from err
            if not self._valid_file(os.fstat(self._file)):
                raise WriterError("WRITER_FILE_UNSAFE")
            try:
                fcntl.flock(self._file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as err:
                raise WriterError("WRITER_BUSY") from err
            self._owner = (os.getpid(), threading.get_ident())
            self.session += 1
            self.check()
            os.fsync(self._directory)
            return self
        except OSError as err:
            self.close()
            raise WriterError("WRITER_UNAVAILABLE") from err
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        # Closing the fd releases flock. Retaining the inode is mandatory.
        if self._file is not None:
            os.close(self._file)
            self._file = None
        if self._directory is not None:
            os.close(self._directory)
            self._directory = None
        self._owner = None

    def __exit__(self, *_exc: object) -> None:
        self.close()


def enroll_writer(root: str) -> dict:
    """One-time enrollment (plan R1 step E3): create ONLY an absent 0700 directory and an absent 0600
    empty lock. Anything already present must match exactly; nothing is ever chmod-ed, repaired,
    replaced or removed. Returns the dev/ino/mode evidence for the read-back."""
    root = os.path.abspath(root)
    parent = os.path.dirname(root)
    if os.path.realpath(parent) != parent or os.path.basename(root) in ("", ".", ".."):
        raise WriterError("ENROLL_ROOT_UNSAFE")
    parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    created = []
    try:
        name = os.path.basename(root)
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
            created.append("directory")
        except FileExistsError:
            pass
        directory = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            dst = os.fstat(directory)
            if (not stat.S_ISDIR(dst.st_mode) or stat.S_IMODE(dst.st_mode) != 0o700
                    or (dst.st_uid, dst.st_gid) != (os.geteuid(), os.getegid())):
                raise WriterError("ENROLL_DIRECTORY_MISMATCH")
            # Refuse a foreign or partial directory before creating anything in it (leave it for inspection).
            if set(os.listdir(directory)) - {LOCK_NAME}:
                raise WriterError("ENROLL_FOREIGN_ENTRY")
            try:
                fd = os.open(LOCK_NAME, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600,
                             dir_fd=directory)
                os.close(fd)
                created.append("lock")
            except FileExistsError:
                pass
            lst = os.stat(LOCK_NAME, dir_fd=directory, follow_symlinks=False)
            if not WriterLease._valid_file(lst) or (lst.st_uid, lst.st_gid) != (dst.st_uid, dst.st_gid):
                raise WriterError("ENROLL_LOCK_MISMATCH")
            if set(os.listdir(directory)) - {LOCK_NAME}:
                raise WriterError("ENROLL_FOREIGN_ENTRY")
            os.fsync(directory)
            os.fsync(parent_fd)
            return {"schema": "hbd.writer_enrollment.v1", "created": created,
                    "directory": {"dev": dst.st_dev, "ino": dst.st_ino, "mode": "0700",
                                  "uid": dst.st_uid, "gid": dst.st_gid},
                    "lock": {"dev": lst.st_dev, "ino": lst.st_ino, "mode": "0600", "size": lst.st_size,
                             "nlink": lst.st_nlink}}
        finally:
            os.close(directory)
    except OSError as err:
        raise WriterError("ENROLL_FAILED") from err
    finally:
        os.close(parent_fd)
