"""Inactive private export cutover; no installed App reads this pointer.

Complete immutable fixture bundles precede single-pointer publication. No live
Journal rewrite, backup installation, App route, CLI or production trust issuer.
"""
from __future__ import annotations

import hashlib
import copy
import os
import secrets

from .. import history
from ..writer import WriterLease
from .deployer_history_store import GenerationStore, Scope, MAX_META
from .deployer_activation_reader import read_and_verify, MARKER

ACTIVE = "ACTIVE_EXPORT.json"


class CutoverError(ValueError):
    """Fixed sanitized reason; no private paths or underlying exception text."""


def _require(ok, reason):
    if not ok:
        raise CutoverError(reason)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


class PrivateCutover:
    def __init__(self, root, *, admission=None, clock=None, checkpoint=None):
        self.root = root
        self.admission = admission
        self.clock = clock
        self.checkpoint = checkpoint or (lambda _: None)

    def _root(self, fence, root_bindings=None):
        _require(isinstance(fence, WriterLease), "FENCE_UNAVAILABLE")
        fence.check()
        if root_bindings is not None:
            from .deployer_working_journal import WorkingJournal
            return WorkingJournal._root(self.root, fence, root_bindings)
        _require(isinstance(self.root, str) and os.path.isabs(self.root)
                 and os.path.realpath(self.root) == self.root
                 and self.root != fence.root
                 and os.path.commonpath((self.root, fence.root)) == fence.root, "ROOT_UNSAFE")
        return GenerationStore._directory(None, self.root)

    @staticmethod
    def _pointer(parent):
        try:
            raw = GenerationStore._read(parent, ACTIVE, 384)
        except FileNotFoundError:
            return None
        value = history._json(raw)
        _require(isinstance(value, dict) and set(value) == {"export", "generation", "seal_sha256"}, "ACTIVE_SHAPE")
        digest = value["seal_sha256"]
        _require(isinstance(digest, str) and len(digest) == 64
                 and all(c in "0123456789abcdef" for c in digest)
                 and value["export"] == "e-" + digest
                 and value["generation"] == "g-" + digest, "ACTIVE_SHAPE")
        return value

    def _verify(self, parent, active, scope, fence, *, clock=None, root_bindings=None, phase_guard=None):
        folder = active["export"]
        fd = GenerationStore._directory(parent, folder)
        try:
            _require(set(os.listdir(fd)) == {"journal", "recovery", "SEAL.json"}, "EXPORT_SHAPE")
            seal = GenerationStore._read(fd, "SEAL.json", MAX_META)
            _require(_sha(seal) == active["seal_sha256"], "SEAL_IDENTITY")
            decoded = history._json(seal)
            children = []
            try:
                children.append(GenerationStore._directory(fd, "journal"))
                children.append(GenerationStore._directory(fd, "recovery"))
                jfd, rfd = children
                _require(set(os.listdir(jfd)) == set(decoded["history"]) | {MARKER}
                         and set(os.listdir(rfd)) == {p.rsplit("/", 1)[1]
                              for p in decoded["private_recovery_index"]}, "EXPORT_SHAPE")
            finally:
                for child in children:
                    os.close(child)
            pointer = {k: active[k] for k in ("generation", "seal_sha256")}
            receipt = read_and_verify(seal, expected_pointer=pointer, expected_scope=scope,
                                      journal_root=os.path.join(self.root, folder, "journal"),
                                      recovery_root=os.path.join(self.root, folder, "recovery"),
                                      fence=fence, clock=clock or self.clock,
                                      root_bindings=root_bindings, phase_guard=phase_guard)
            _require((os.fstat(fd).st_dev, os.fstat(fd).st_ino) ==
                     (os.stat(folder, dir_fd=parent, follow_symlinks=False).st_dev,
                      os.stat(folder, dir_fd=parent, follow_symlinks=False).st_ino), "ROOT_DRIFT")
            return receipt
        finally:
            os.close(fd)

    @staticmethod
    def _root_identity(parent, root):
        held = os.fstat(parent)
        visible = os.stat(root, follow_symlinks=False)
        _require((held.st_dev, held.st_ino) == (visible.st_dev, visible.st_ino), "ROOT_DRIFT")

    def inspect(self, *, expected_active, scope, fence=None, root_bindings=None, phase_guard=None):
        """No cleanup/replay: absent pointer stays absent; complete pointer verified."""
        parent = None
        try:
            _require(callable(self.clock) and isinstance(scope, Scope), "DEPENDENCY_UNAVAILABLE")
            scope.validate(self.clock())
            if root_bindings is not None:
                _require(callable(phase_guard), 'CUTOVER_BOUND_AUTHORITY')
                phase_guard()
            parent = self._root(fence, root_bindings)
            observed = self._pointer(parent)
            _require(observed == expected_active, "ACTIVE_CHANGED")
            receipt = None if observed is None else self._verify(parent, observed, scope, fence,
                       root_bindings=root_bindings, phase_guard=phase_guard)
            fence.check()
            self._root_identity(parent, self.root)
            _require(self._pointer(parent) == observed, "ACTIVE_CHANGED")
            if phase_guard is not None:
                phase_guard()
            return observed, receipt
        except CutoverError:
            raise
        except (OSError, ValueError, TypeError, RuntimeError, KeyError, AttributeError):
            raise CutoverError("INSPECTION_REFUSED") from None
        finally:
            if parent is not None:
                os.close(parent)

    def publish(self, *, source_root, expected_generation, expected_active, scope, fence=None,
                root_bindings=None, phase_guard=None):
        """Copy a completely reread sealed generation into an exclusive export.

        Admission is an unimplemented host dependency, not a supplied receipt.
        Fixture admission cannot grant production trust. Incomplete exports and
        temporary pointers stay retained. Existing export names refuse retries.

        Installed layout (#985 M4, 0.3.23): with ``root_bindings`` both the export root
        and the source generation root are private descendants of the bound ``data``
        root; every write then re-checks ``phase_guard`` and the held fence.
        """
        descriptors = []
        try:
            _require(callable(self.admission) and callable(self.clock)
                     and isinstance(scope, Scope), "DEPENDENCY_UNAVAILABLE")
            expected_generation = copy.deepcopy(expected_generation)
            expected_active = copy.deepcopy(expected_active)
            scope.validate(self.clock())
            if root_bindings is not None:
                from .deployer_working_journal import WorkingJournal
                _require(callable(phase_guard), "DEPENDENCY_UNAVAILABLE")
                phase_guard()
                inner_fence = fence

                class _Guarded:
                    root = inner_fence.root

                    def check(inner):
                        phase_guard()
                        inner_fence.check()
                writer = _Guarded()
            else:
                writer = fence
            parent = self._root(fence, root_bindings)
            descriptors.append(parent)
            _require(isinstance(source_root, str) and os.path.isabs(source_root)
                     and os.path.realpath(source_root) == source_root
                     and source_root != fence.root
                     and (root_bindings is not None or os.path.commonpath((source_root, fence.root)) == fence.root)
                     and os.path.commonpath((source_root, self.root)) not in (source_root, self.root), "SOURCE_UNSAFE")
            source = (GenerationStore._directory(None, source_root) if root_bindings is None
                      else WorkingJournal._root(source_root, fence, root_bindings))
            descriptors.append(source)
            _require(GenerationStore._pointer(source) == expected_generation
                     and expected_generation is not None, "SOURCE_CHANGED")
            snapshot = GenerationStore._load(source, expected_generation)
            generation = GenerationStore._directory(source, expected_generation["generation"])
            try:
                seal = GenerationStore._read(generation, "SEAL.json", MAX_META)
            finally:
                os.close(generation)
            decoded = history._json(seal)
            _require(decoded["scope"] == vars(scope), "SCOPE_MISMATCH")
            _require(self._pointer(parent) == expected_active, "ACTIVE_CHANGED")
            # No old export is overwritten. Existing active bytes must remain
            # valid and be retained even when the new generation is rejected.
            if expected_active is not None:
                old_fd = GenerationStore._directory(parent, expected_active["export"])
                try:
                    old_seal = history._json(GenerationStore._read(old_fd, "SEAL.json", MAX_META))
                finally:
                    os.close(old_fd)
                old_scope = Scope(**old_seal["scope"])
                # Historical expiry does not grant new authority; old content
                # verification uses its original observation time only.
                self._verify(parent, expected_active, old_scope, fence,
                             clock=lambda: old_scope.expires_at - 1)
            scope.validate(self.clock())
            binding = history._canonical({"schema": "hbd.private_cutover_admission.v1",
                                         "scope": vars(scope), "generation": expected_generation,
                                         "expected_active": expected_active, "seal_sha256": _sha(seal)})
            _require(self.admission(binding) is True, "ADMISSION_REFUSED")
            scope.validate(self.clock())
            fence.check()
            digest = _sha(seal)
            active = {"export": "e-" + digest, **expected_generation}
            os.mkdir(active["export"], mode=0o700, dir_fd=parent)
            export = GenerationStore._directory(parent, active["export"])
            descriptors.append(export)
            self.checkpoint("export_created")
            recovery = {path.rsplit("/", 1)[1]: snapshot["recovery"][member]
                        for path, member in snapshot["private_recovery_index"].items()}
            _require(len(recovery) == len(snapshot["recovery"]), "RECOVERY_COLLISION")
            for folder, values in (("journal", snapshot["history"]), ("recovery", recovery)):
                fence.check()
                os.mkdir(folder, mode=0o700, dir_fd=export)
                child = GenerationStore._directory(export, folder)
                try:
                    for name, raw in sorted(values.items()):
                        GenerationStore._write(child, name, raw, writer)
                    if folder == "journal":
                        GenerationStore._write(child, MARKER, history._canonical(expected_generation), writer)
                    fence.check()
                    os.fsync(child)
                finally:
                    os.close(child)
                self.checkpoint(folder + "_durable")
            GenerationStore._write(export, "SEAL.json", seal, writer)
            fence.check()
            os.fsync(export)
            os.fsync(parent)
            self._verify(parent, active, scope, fence, root_bindings=root_bindings, phase_guard=phase_guard)
            self.checkpoint("export_verified")
            temporary = ".active-" + secrets.token_hex(16)
            GenerationStore._write(parent, temporary, history._canonical(active), writer)
            self.checkpoint("pointer_prepared")
            scope.validate(self.clock())
            fence.check()
            self._root_identity(parent, self.root)
            self._root_identity(source, source_root)
            _require(GenerationStore._pointer(source) == expected_generation, "SOURCE_CHANGED")
            _require(self._pointer(parent) == expected_active, "ACTIVE_CHANGED")
            self._verify(parent, active, scope, fence, root_bindings=root_bindings, phase_guard=phase_guard)
            scope.validate(self.clock())
            fence.check()
            self._root_identity(parent, self.root)
            self._root_identity(source, source_root)
            _require(GenerationStore._load(source, expected_generation) == snapshot, "SOURCE_BYTES_CHANGED")
            _require(GenerationStore._pointer(source) == expected_generation, "SOURCE_CHANGED")
            _require(self._pointer(parent) == expected_active, "ACTIVE_CHANGED")
            scope.validate(self.clock())
            fence.check()
            os.replace(temporary, ACTIVE, src_dir_fd=parent, dst_dir_fd=parent)
            self.checkpoint("pointer_replaced")
            fence.check()
            os.fsync(parent)
            self.checkpoint("publication_durable")
            _, receipt = self.inspect(expected_active=active, scope=scope, fence=fence,
                                      root_bindings=root_bindings, phase_guard=phase_guard)
            return active, {**receipt, "fixture_export_published": True}
        except CutoverError:
            raise
        except FileExistsError:
            raise CutoverError("EXPORT_ALREADY_EXISTS") from None
        except (OSError, ValueError, TypeError, RuntimeError, KeyError, AttributeError):
            raise CutoverError("CUTOVER_REFUSED") from None
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
