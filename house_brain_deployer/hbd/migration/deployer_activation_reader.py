"""Inactive private destination reader; no App route or activation writer.

Requires a caller-held common-root lease, never creates a lock or an activation
marker. The marker is a proposed private cutover artifact, not present or trusted
in the accepted App. Strict private modes are not native compatibility evidence.
"""
from __future__ import annotations

import hashlib
import os

from .. import history
from ..policy import check_backup_path
from ..writer import WriterLease
from .deployer_history_store import GenerationStore, Scope, MAX_META, MAX_RECOVERY_FILES
from .qualify_deployer_history import MAX_RECOVERY_BYTES
from .deployer_activation_readback import verify_readback, ReadbackError

MARKER = "ACTIVE_GENERATION.json"


class ReaderError(ValueError):
    """Fixed sanitized code; private source paths and values never leave errors."""


def _require(ok, reason):
    if not ok:
        raise ReaderError(reason)


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_nlink,
            info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _observed(parent, name):
    try:
        return _signature(os.stat(name, dir_fd=parent, follow_symlinks=False))
    except FileNotFoundError:
        return None


def read_and_verify(seal_bytes, *, expected_pointer, expected_scope, journal_root,
                    recovery_root, fence=None, clock=None, checkpoint=None,
                    root_bindings=None, phase_guard=None):
    """Read fixed Journal names and exact seal-indexed flat backup names only.

    No generic paths, shell, network, control-file creation or destination writes.
    Verify all identities/absences before and after bounded descriptor reads.
    Lease is advisory; uncooperative privileged changes are not excluded. Elapsed
    freshness refuses slow reads but does not preempt a blocked filesystem call.
    """
    descriptors = []
    try:
        _require(isinstance(fence, WriterLease) and callable(clock), "FENCE_UNAVAILABLE")
        fence.check()
        observed_at = clock()
        _require(isinstance(expected_scope, Scope), "SCOPE_INVALID")
        expected_scope.validate(observed_at)
        _require(isinstance(seal_bytes, bytes) and len(seal_bytes) <= MAX_META, "SEAL_BYTES")
        digest = hashlib.sha256(seal_bytes).hexdigest()
        _require(expected_pointer == {"generation": "g-" + digest, "seal_sha256": digest}, "SEAL_IDENTITY")
        seal = history._json(seal_bytes)
        _require(isinstance(seal, dict) and seal.get("scope") == vars(expected_scope), "SCOPE_MISMATCH")
        index = seal.get("private_recovery_index")
        _require(isinstance(index, dict) and len(index) <= MAX_RECOVERY_FILES
                 and all(isinstance(p, str) for p in index), "RECOVERY_INDEX")
        names = {}
        for path in index:
            check_backup_path(path)
            names[path] = path.rsplit("/", 1)[1]
        _require(len(set(names.values())) == len(names), "RECOVERY_INDEX")
        roots = (journal_root, recovery_root)
        for root in roots:
            _require(isinstance(root, str) and os.path.isabs(root)
                     and os.path.realpath(root) == root
                     and root != fence.root, "ROOT_UNSAFE")
            if root_bindings is None:
                _require(os.path.commonpath((root, fence.root)) == fence.root, 'ROOT_UNSAFE')
            else:
                _require(callable(phase_guard), 'READER_BOUND_AUTHORITY')
                phase_guard()
                data = root_bindings.path('data', fence=fence)
                _require(root != data and os.path.commonpath((root, data)) == data, 'ROOT_UNSAFE')
        _require(journal_root != recovery_root
                 and os.path.commonpath(roots) not in roots, "ROOT_OVERLAP")
        for root in roots:
            if root_bindings is None:
                descriptors.append(GenerationStore._directory(None, root))
            else:
                parts = tuple(os.path.relpath(root, data).split(os.sep))
                descriptors.append(root_bindings.open_directory('data', parts, fence=fence))
        root_identities = [_signature(os.fstat(fd)) for fd in descriptors]
        jfd, rfd = descriptors
        requested = [(jfd, name) for name in (*history.FILES, MARKER)] + \
                    [(rfd, name) for name in names.values()]
        before = {(fd, name): _observed(fd, name) for fd, name in requested}
        # Root identities are stable directory inodes; directory metadata may
        # change during a read and is checked against the initial observation.
        fence.check()
        if checkpoint:
            checkpoint("identities_observed")
        actual_history, actual_recovery = {}, {}
        for folder, parent, selection, limit in (
                (actual_history, jfd, {name: name for name in history.FILES}, history.MAX_BYTES),
                (actual_recovery, rfd, names, MAX_RECOVERY_BYTES)):
            total = 0
            for key, name in selection.items():
                if phase_guard is not None:
                    phase_guard()
                fence.check()
                if before[(parent, name)] is None:
                    continue
                value = GenerationStore._read(parent, name, limit - total)
                total += len(value)
                folder[key] = value
        fence.check()
        marker_bytes = GenerationStore._read(jfd, MARKER, 256)
        observed_pointer = history._json(marker_bytes)
        if checkpoint:
            checkpoint("bytes_read")
        fence.check()
        _require(all(before[(fd, name)] == _observed(fd, name) for fd, name in requested), "READBACK_DRIFT")
        _require(all(original == _signature(os.fstat(fd)) ==
                     _signature(os.stat(root, follow_symlinks=False))
                     for original, fd, root in zip(root_identities, descriptors, roots)), "ROOT_DRIFT")  # noqa: B905 - pairing kept as written (no strict)
        result = verify_readback(seal_bytes, actual_history, actual_recovery,
                                 expected_pointer=expected_pointer, observed_pointer=observed_pointer,
                                 expected_scope=expected_scope, observed_at=observed_at, now=clock())
        fence.check()
        if phase_guard is not None:
            phase_guard()
        return result
    except ReaderError:
        raise
    except ReadbackError as error:
        raise ReaderError(str(error)) from None
    except (OSError, ValueError, TypeError, RuntimeError, KeyError, AttributeError, OverflowError):
        raise ReaderError("DESTINATION_READ_REFUSED") from None
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
