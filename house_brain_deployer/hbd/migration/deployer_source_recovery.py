"""Read exact selected Undo sources; no capture provenance or import authority.

Caller retains the common fence and chooses an already qualified mount. This
module never locks, writes, chmods, deletes, attests or reads unrelated files.
"""
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
import stat
import time

from .. import history
from ..writer import WriterLease
from .qualify_deployer_history import required_recovery, qualify, MAX_RECOVERY_BYTES

MAX_SECONDS = 5
FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class RecoveryError(ValueError): pass


def require(ok, code):
    if not ok: raise RecoveryError(code)


def identity(value):
    return (value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_nlink,
            value.st_size, value.st_mtime_ns, value.st_ctime_ns)


@dataclass(frozen=True)
class PreparedRecovery:
    _files: tuple = field(repr=False)
    inventory_sha256: str

    def private_files(self): return dict(self._files)

    def receipt(self):
        return {'schema':'hbd.source_recovery.preparation.v1',
                'inventory_sha256':self.inventory_sha256, 'files':len(self._files),
                'bytes':sum(len(raw) for _,raw in self._files),
                'provenance':'UNATTESTED_INPUT', 'source_verified':False,
                'backup_verified':False, 'writer_exclusion_verified':False,
                'native_qualified':False, 'import_allowed':False, 'uninstall_allowed':False}


def read_source_recovery(source, destination, *, selected, now, packages_root,
                         fence=None, clock=time.monotonic):
    """Read/re-read derived basenames beneath one retained owned directory.

    0700 and0755 directory/0600 and0644 file modes are supported without changing
    any permissions. This does not qualify a live mount or other writer behavior.
    Between-call checks bound elapsed time; blocked regular-file I/O remains a
    platform qualification gap, unlike the dedicated Supervisor transport timer.
    """
    fd = None
    try:
        require(type(fence) is WriterLease and callable(clock), 'FENCE_UNAVAILABLE')
        fence.check()
        require(type(source) is dict and type(destination) is dict and
                all(type(v) is bytes for v in (*source.values(), *destination.values())), 'HISTORY_BYTES')
        source, destination = dict(source), dict(destination)
        require(type(packages_root) is str and os.path.isabs(packages_root) and
                os.path.realpath(packages_root) == packages_root and packages_root != fence.root and
                os.path.commonpath((packages_root, fence.root)) == fence.root, 'ROOT_UNSAFE')
        start = clock()
        require(type(start) in (int,float) and math.isfinite(start), 'CLOCK')
        fence_root = fence.root
        def check():
            elapsed = clock()-start
            require(type(elapsed) in (int,float) and math.isfinite(elapsed) and
                    0 <= elapsed < MAX_SECONDS, 'RECOVERY_EXPIRED')
            require(fence.root == fence_root, 'FENCE_CHANGED')
            fence.check()
            if fd is not None:
                held = os.fstat(fd)
                visible = os.stat(packages_root, follow_symlinks=False)
                require(os.path.realpath(packages_root) == packages_root and
                        identity(held) == root_identity == identity(visible), 'ROOT_DRIFT')
        check()
        _, expected = required_recovery(source, destination, selected=selected, now=now)
        check()
        fd = os.open(packages_root, FLAGS | os.O_DIRECTORY)
        root = os.fstat(fd)
        require(stat.S_ISDIR(root.st_mode) and root.st_uid == os.geteuid() and
                stat.S_IMODE(root.st_mode) in (0o700,0o755), 'ROOT_MODE')
        root_identity = identity(root)
        check()
        def read(path, limit):
            check()
            # Paths come solely from the shared validated-history contract.
            name = path.rsplit('/',1)[1]
            file_fd = os.open(name, FLAGS, dir_fd=fd)
            try:
                before = os.fstat(file_fd)
                require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and
                        before.st_uid == os.geteuid() and stat.S_IMODE(before.st_mode) in (0o600,0o644),
                        'RECOVERY_FILE')
                require(before.st_size <= limit, 'RECOVERY_SIZE')
                output = bytearray()
                while True:
                    check()
                    chunk = os.pread(file_fd, min(65536,limit+1-len(output)),len(output))
                    check()
                    if not chunk: break
                    output.extend(chunk)
                    require(len(output) <= limit, 'RECOVERY_SIZE')
                after = os.fstat(file_fd)
                entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
                require(identity(before) == identity(after) == identity(entry) and
                        len(output) == after.st_size, 'RECOVERY_DRIFT')
                check()
                return bytes(output), identity(after)
            finally: os.close(file_fd)
        files, observed, total = {}, {}, 0
        for path, sha in sorted(expected.items()):
            raw, item = read(path, MAX_RECOVERY_BYTES-total)
            require(hashlib.sha256(raw).hexdigest() == sha, 'RECOVERY_HASH')
            files[path], observed[path] = raw, item
            total += len(raw)
        # Whole selected inventory is read again after the final first-pass read.
        for path, raw in files.items():
            second, item = read(path, len(raw))
            require(second == raw and item == observed[path], 'RECOVERY_DRIFT')
        qualify(source,destination,files,selected=selected,now=now)
        check()
        inventory = json.dumps(expected,sort_keys=True,separators=(',',':')).encode()
        return PreparedRecovery(tuple(sorted(files.items())),hashlib.sha256(inventory).hexdigest())
    except RecoveryError: raise
    except history.HistoryError: raise RecoveryError('HISTORY_REFUSED') from None
    except Exception: raise RecoveryError('RECOVERY_UNAVAILABLE') from None  # noqa: BLE001 - fail closed to a fixed refusal code
    finally:
        if fd is not None: os.close(fd)
