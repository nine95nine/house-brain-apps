"""Inactive private generation-store prototype; no production Journal activation.

The admission callback is an unimplemented host trust boundary, never a boolean
receipt from the caller. This module has no App route or CLI. Fixture publication
is not verified capture, completed migration, real Undo, or uninstall authority.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
from dataclasses import dataclass
from contextlib import nullcontext

from .. import history
from ..policy import check_backup_path, PolicyError
from ..writer import WriterLease, WriterError
from .qualify_deployer_history import qualify, plan_chronology, MAX_RECOVERY_BYTES

MAX_META = 1024 * 1024
MAX_RECOVERY_FILES = 4096  # admission only, real inventory not calibrated
POINTER = "CURRENT.json"


class StoreError(ValueError):
    """Fixed sanitized reason, never a private path or operating-system text."""


def _require(ok, reason):
    if not ok:
        raise StoreError(reason)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class Scope:
    operation_id: str
    source_instance: str
    destination_instance: str
    owner_id: str
    candidate_sha256: str
    expires_at: float

    def validate(self, now):
        _require(all(isinstance(v, str) and re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", v)
                     for v in (self.operation_id, self.source_instance, self.destination_instance, self.owner_id)),
                 "SCOPE_INVALID")
        _require(self.source_instance != self.destination_instance, "SCOPE_INVALID")
        _require(isinstance(self.candidate_sha256, str) and re.fullmatch(r"[0-9a-f]{64}", self.candidate_sha256),
                 "SCOPE_INVALID")
        _require(history._finite_number(now) and history._finite_number(self.expires_at)
                 and now < self.expires_at, "SCOPE_EXPIRED")


class GenerationStore:
    def __init__(self, root, *, admission=None, clock=None, checkpoint=None):
        self.root = root
        self.admission = admission
        self.clock = clock
        self.checkpoint = checkpoint or (lambda _: None)

    @staticmethod
    def _identity(st):
        return st.st_dev, st.st_ino

    @staticmethod
    def _directory(parent, name):
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        st = os.fstat(fd)
        if st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o700:
            os.close(fd)
            raise StoreError("PRIVATE_DIRECTORY")
        return fd

    @staticmethod
    def _read(parent, name, limit):
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            before = os.fstat(fd)
            _require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                     and before.st_uid == os.geteuid() and stat.S_IMODE(before.st_mode) == 0o600
                     and before.st_size <= limit, "PRIVATE_FILE")
            raw = bytearray()
            while len(raw) <= before.st_size:
                chunk = os.read(fd, min(65536, before.st_size + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            after = os.fstat(fd)
            visible = os.stat(name, dir_fd=parent, follow_symlinks=False)
            signature = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_nlink,  # noqa: E731 - local helper kept as written
                                   s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            _require(len(raw) == before.st_size and signature(before) == signature(after) == signature(visible),
                     "READBACK_DRIFT")
            return bytes(raw)
        finally:
            os.close(fd)

    @staticmethod
    def _write(parent, name, raw, lease):
        lease.check()
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        try:
            lease.check()
            view = memoryview(raw)
            while view:
                lease.check()
                written = os.write(fd, view)
                _require(written > 0, "WRITE_FAILED")
                view = view[written:]
            lease.check()
            os.fsync(fd)
        finally:
            os.close(fd)

    @classmethod
    def _pointer(cls, root):
        try:
            raw = cls._read(root, POINTER, 256)
        except FileNotFoundError:
            return None
        data = history._json(raw)
        _require(isinstance(data, dict) and set(data) == {"generation", "seal_sha256"}
                 and isinstance(data["generation"], str) and re.fullmatch(r"g-[0-9a-f]{64}", data["generation"])
                 and isinstance(data["seal_sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", data["seal_sha256"]),
                 "POINTER_INVALID")
        return data

    @classmethod
    def _load(cls, root, pointer):
        """Bounded complete seal/hash readback; private result never a remote API."""
        generation = cls._directory(root, pointer["generation"])
        try:
            _require(set(os.listdir(generation)) == {"history", "recovery", "SEAL.json"}, "GENERATION_SHAPE")
            raw = cls._read(generation, "SEAL.json", MAX_META)
            _require(_sha(raw) == pointer["seal_sha256"]
                     and pointer["generation"] == "g-" + _sha(raw), "SEAL_IDENTITY")
            seal = history._json(raw)
            _require(isinstance(seal, dict) and seal.get("schema") == "hbd.generation_preparation.v1",
                     "SEAL_SHAPE")
            inventories = {k: seal.get(k) for k in ("history", "recovery")}
            _require(all(isinstance(v, dict) for v in inventories.values()), "SEAL_SHAPE")
            _require(set(inventories["history"]) <= set(history.FILES)
                     and len(inventories["recovery"]) <= MAX_RECOVERY_FILES, "SEAL_SHAPE")
            _require(all(re.fullmatch(r"[0-9a-f]{64}\.bak", n) for n in inventories["recovery"]), "SEAL_SHAPE")
            index = seal.get("private_recovery_index")
            _require(isinstance(index, dict) and all(isinstance(p, str) and isinstance(n, str)
                                                   for p, n in index.items())
                     and len(index) == len(inventories["recovery"])
                     and set(index.values()) == set(inventories["recovery"]), "RECOVERY_INDEX")
            for path, name in index.items():
                try:
                    check_backup_path(path)
                except PolicyError:
                    raise StoreError("RECOVERY_INDEX") from None
                _require(name == _sha(path.encode()) + ".bak", "RECOVERY_INDEX")
            result = {}
            for folder, inventory in inventories.items():
                child = cls._directory(generation, folder)
                try:
                    _require(set(os.listdir(child)) == set(inventory), "GENERATION_SHAPE")
                    budget = history.MAX_BYTES if folder == "history" else MAX_RECOVERY_BYTES
                    values, total = {}, 0
                    for name, sha in inventory.items():
                        _require(isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha), "SEAL_SHAPE")
                        value = cls._read(child, name, budget - total)
                        total += len(value)
                        _require(_sha(value) == sha, "GENERATION_HASH")
                        values[name] = value
                    result[folder] = values
                finally:
                    os.close(child)
            _require(history.fingerprint(result["history"]) == seal.get("result_sha256"), "GENERATION_HASH")
            result["private_recovery_index"] = dict(index)
            return result
        finally:
            os.close(generation)

    def _held_fence(self, fence):
        _require(isinstance(fence, WriterLease), "FENCE_UNAVAILABLE")
        fence.check()
        _require(os.path.commonpath((self.root, fence.root)) == fence.root, "FENCE_SCOPE")
        return fence

    def private_head(self, *, fence=None):
        root = None
        try:
            _require(isinstance(self.root, str) and os.path.isabs(self.root)
                     and os.path.realpath(self.root) == self.root, "ROOT_UNSAFE")
            if fence is not None:
                self._held_fence(fence)
            root = self._directory(None, self.root)
            before = self._pointer(root)
            if before is None:
                if fence is not None:
                    fence.check()
                return None
            values = self._load(root, before)
            _require(before == self._pointer(root)
                     and self._identity(os.fstat(root)) == self._identity(os.stat(self.root, follow_symlinks=False)),
                     "READBACK_DRIFT")
            if fence is not None:
                fence.check()
            return before, values
        except StoreError:
            raise
        except (OSError, ValueError, TypeError, WriterError):
            raise StoreError("READBACK_REFUSED") from None
        finally:
            if root is not None:
                os.close(root)

    def stage(self, source, destination, recovery, *, selected, scope, expected_head, fence=None,
              root_bindings=None, phase_guard=None):
        """Publish a new sealed fixture generation; never overwrite live history.

        An injected admission service must atomically verify/consume permission
        against the complete binding. No such production service is provided.
        Interrupted generations are retained; retries cannot reuse their names.
        A supplied fence is borrowed, never reacquired or closed. Every composed
        participant must use this same common root. Omission preserves the older
        standalone fixture lock and grants no cross-root exclusion.

        Installed layout (#985 M4, 0.3.23): with ``root_bindings`` the store root is a
        private descendant of the bound ``data`` root instead of the lock folder. A held
        fence and a ``phase_guard`` are then required, and every write re-checks both.
        """
        root_fd = generation_fd = None
        try:
            _require(callable(self.admission) and callable(self.clock), "ADMISSION_UNAVAILABLE")
            _require(isinstance(scope, Scope), "SCOPE_INVALID")
            scope.validate(self.clock())
            source, destination, recovery = dict(source), dict(destination), dict(recovery)
            receipt = qualify(source, destination, recovery, selected=selected, now=self.clock())
            planned = plan_chronology(source, destination, now=self.clock())
            files = planned.private_files()
            _require(len(recovery) <= MAX_RECOVERY_FILES, "RECOVERY_COUNT")
            recovery_files = {_sha(path.encode()) + ".bak": raw for path, raw in recovery.items()}
            _require(len(recovery_files) == len(recovery), "RECOVERY_COLLISION")
            private_index = {path: _sha(path.encode()) + ".bak" for path in recovery}
            _require(isinstance(self.root, str) and os.path.isabs(self.root)
                     and os.path.realpath(self.root) == self.root, "ROOT_UNSAFE")
            if root_bindings is None:
                root_fd = self._directory(None, self.root)
                context = WriterLease(self.root) if fence is None else nullcontext(self._held_fence(fence))
            else:
                from .deployer_working_journal import WorkingJournal
                _require(isinstance(fence, WriterLease) and callable(phase_guard), "FENCE_UNAVAILABLE")
                fence.check()
                phase_guard()
                root_fd = WorkingJournal._root(self.root, fence, root_bindings)

                class _Guarded:
                    def check(inner):
                        phase_guard()
                        fence.check()
                context = nullcontext(_Guarded())
            with context as lease:
                _require(self._identity(os.fstat(root_fd)) == self._identity(os.stat(self.root, follow_symlinks=False)),
                         "ROOT_DRIFT")
                current = self._pointer(root_fd)
                _require(current == expected_head, "DESTINATION_CHANGED")
                if current is not None:
                    retained = self._load(root_fd, current)
                    _require(history.fingerprint(retained["history"]) == planned.destination_sha256,
                             "DESTINATION_HISTORY_CHANGED")
                    for filename, raw in retained["recovery"].items():
                        _require(filename not in recovery_files or recovery_files[filename] == raw,
                                 "RECOVERY_CONFLICT")
                        recovery_files[filename] = raw
                    private_index.update(retained["private_recovery_index"])
                _require(len(recovery_files) <= MAX_RECOVERY_FILES
                         and sum(len(raw) for raw in recovery_files.values()) <= MAX_RECOVERY_BYTES,
                         "RECOVERY_CUMULATIVE_LIMIT")
                binding = {"schema": "hbd.generation_preparation.v1", "scope": dict(vars(scope)),
                           "source_sha256": planned.source_sha256, "destination_sha256": planned.destination_sha256,
                           "result_sha256": planned.result_sha256, "selected": list(selected),
                           "history": {n: _sha(raw) for n, raw in files.items()},
                           "recovery": {n: _sha(raw) for n, raw in recovery_files.items()},
                           "private_recovery_index": private_index, "expected_head": expected_head}
                seal = history._canonical(binding)
                _require(len(seal) <= MAX_META, "METADATA_LIMIT")
                name = "g-" + _sha(seal)
                scope.validate(self.clock())
                # Pass detached canonical bytes, not a mutable binding dict.
                _require(self.admission(seal) is True, "ADMISSION_REFUSED")
                scope.validate(self.clock())
                lease.check()
                os.mkdir(name, mode=0o700, dir_fd=root_fd)
                generation_fd = self._directory(root_fd, name)
                self.checkpoint("generation_created")
                for folder, values in (("history", files), ("recovery", recovery_files)):
                    lease.check()
                    os.mkdir(folder, mode=0o700, dir_fd=generation_fd)
                    child = self._directory(generation_fd, folder)
                    try:
                        for filename, raw in sorted(values.items()):
                            self._write(child, filename, raw, lease)
                            _require(self._read(child, filename, history.MAX_BYTES if folder == "history"
                                                else MAX_RECOVERY_BYTES) == raw, "READBACK_DRIFT")
                        lease.check()
                        os.fsync(child)
                    finally:
                        os.close(child)
                    self.checkpoint(folder + "_durable")
                self._write(generation_fd, "SEAL.json", seal, lease)
                _require(self._read(generation_fd, "SEAL.json", MAX_META) == seal, "READBACK_DRIFT")
                lease.check()
                os.fsync(generation_fd)
                os.fsync(root_fd)
                self.checkpoint("generation_sealed")
                pointer = {"generation": name, "seal_sha256": _sha(seal)}
                self._load(root_fd, pointer)
                scope.validate(self.clock())
                _require(self._pointer(root_fd) == expected_head, "DESTINATION_CHANGED")
                temporary = ".pointer-" + secrets.token_hex(16)
                self._write(root_fd, temporary, history._canonical(pointer), lease)
                lease.check()
                # Pointer preparation/fsync can consume time or expose drift.
                # Recheck at the publication boundary, not only before writing.
                scope.validate(self.clock())
                _require(self._pointer(root_fd) == expected_head, "DESTINATION_CHANGED")
                lease.check()
                os.replace(temporary, POINTER, src_dir_fd=root_fd, dst_dir_fd=root_fd)
                self.checkpoint("pointer_replaced")
                lease.check()
                os.fsync(root_fd)
                self.checkpoint("publication_durable")
                _require(self._pointer(root_fd) == pointer, "READBACK_DRIFT")
                self._load(root_fd, pointer)
                return {"schema": "hbd.generation_preparation_receipt.v1", "generation_sha256": _sha(seal),
                        "history_files": len(files), "recovery_files": len(recovery_files),
                        "fixture_generation_published": True, "journal_activated": False,
                        "applied": False, "capture_trusted": False, "undo_verified": False,
                        "native_verified": False, "uninstall_allowed": False,
                        "history_result_sha256": receipt["result_sha256"]}
        except StoreError:
            raise
        except FileExistsError:
            raise StoreError("GENERATION_ALREADY_EXISTS") from None
        except (OSError, ValueError, TypeError, RuntimeError, WriterError):
            raise StoreError("STAGING_REFUSED") from None
        finally:
            if generation_fd is not None:
                os.close(generation_fd)
            if root_fd is not None:
                os.close(root_fd)
