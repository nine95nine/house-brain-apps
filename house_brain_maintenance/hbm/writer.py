"""0.9.1 Shared writer lock, Maintenance half (#985 enrollment plan R1, step E2; owner pop-up 2026-10-12).

The Deployer and this App exclude each other through one retained-inode flock:
``/share/house_brain_writer_v1/.house_brain_writer_v1.lock`` (0600, empty, one link; the folder is 0700). The
Deployer created both once, with the owner's approval (enrollment E3, 2026-10-12). This App never creates, repairs,
replaces or removes either: ``WriterLease`` below is a byte-identical copy of the Deployer's (``hbd/writer.py``; a test
pins it) and is always used with ``create=False``.

What this App holds the lock around: every step that changes the Deployer App itself (an update, an undo or
restore of it, and the local -> store switch-over and the old Deployer's removal). If the Deployer holds it,
``WRITER_BUSY``: this App writes nothing and says so. While the lock is not enrolled (no folder; for example a new
install), nothing changes from 0.9.0: there is no other writer to exclude. Any other lock fault refuses.

At start the lock's identity (device, inode) is logged, so both Apps can be shown to share one inode (step E6).
Cost: no network, no new files.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import re
import stat
import threading

LOCK_NAME = ".house_brain_writer_v1.lock"
WRITER_ROOT = "/share/house_brain_writer_v1"
RE_DEPLOYER = re.compile(r"(?:local|[0-9a-f]{8})_house_brain_deployer")


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


def is_deployer(slug) -> bool:
    return isinstance(slug, str) and RE_DEPLOYER.fullmatch(slug) is not None


class SharedWriter:
    """The consumer: never creates the lock; holds it around Deployer-changing steps."""

    def __init__(self, root: str = WRITER_ROOT) -> None:
        self.root = root

    def enrolled(self) -> bool:
        return os.path.lexists(self.root)

    def identity(self) -> dict:
        """Read-only facts for the start log (step E6). Never opens the lock for writing."""
        if not self.enrolled():
            return {"state": "not_enrolled"}
        try:
            st = os.stat(os.path.join(self.root, LOCK_NAME), follow_symlinks=False)
        except FileNotFoundError:
            return {"state": "lock_missing"}
        except OSError as err:
            return {"state": "unreadable", "error": type(err).__name__}
        facts = {"dev": f"{os.major(st.st_dev)}.{os.minor(st.st_dev)}", "ino": st.st_ino,
                 "mode": f"{stat.S_IMODE(st.st_mode):04o}", "size": st.st_size, "nlink": st.st_nlink,
                 "owner": f"{st.st_uid}:{st.st_gid}"}
        return {"state": "enrolled" if WriterLease._valid_file(st) else "unsafe", **facts}

    @contextlib.contextmanager
    def hold(self):
        """Hold the lock for the whole block (yields the lease, or None while not enrolled). Raises WriterError
        (``WRITER_BUSY``, ``WRITER_NOT_ENROLLED`` for a folder without its lock, ...) before anything is written."""
        if not self.enrolled():
            yield None
            return
        lease = WriterLease(self.root, create=False)
        lease.__enter__()
        try:
            yield lease
        finally:
            lease.close()
