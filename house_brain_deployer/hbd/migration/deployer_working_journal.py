"""Private working-Journal/recovery transaction; inactive in installed App.

Fixture admission is NOT host authority. Recovery roots must already be private,
quiescent, and beneath the caller-held common fence for preparation. Read-only
startup may use separately authenticated directory bindings. No cleanup or replay.
"""
from __future__ import annotations

import copy
import hashlib
import os
import secrets

from .. import history
from ..fsops import Packages
from ..writer import WriterLease
from .deployer_history_store import GenerationStore, Scope, MAX_META
from .deployer_private_cutover import PrivateCutover
from .deployer_pending_snapshot import read_working_snapshot
from .qualify_deployer_history import qualify, MAX_RECOVERY_BYTES

SELECTOR = "WORKING_JOURNAL.json"
COMPLETION = "COMPLETION.json"


class WorkingError(ValueError):
    """Sanitized reason only."""


def _require(ok, reason):
    if not ok:
        raise WorkingError(reason)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


class WorkingJournal:
    def __init__(self, root, *, admission=None, clock=None, checkpoint=None):
        self.root = root
        self.admission = admission
        self.clock = clock
        self.checkpoint = checkpoint or (lambda _: None)

    @staticmethod
    def _root(root, fence, bindings=None):
        _require(isinstance(fence, WriterLease), "FENCE_UNAVAILABLE")
        fence.check()
        if bindings is not None:
            from ..root_bindings import RootBindings
            _require(type(bindings) is RootBindings, 'PREPARATION_ROOT_BINDINGS')
            for role in ('data', 'working', 'uses', 'packages'):
                base = bindings.path(role, fence=fence)
                if root == base:
                    return bindings.open_directory(role, fence=fence)
            base = bindings.path('data', fence=fence)
            if root != base and os.path.commonpath((root, base)) == base:
                parts = tuple(os.path.relpath(root, base).split(os.sep))
                return bindings.open_directory('data', parts, fence=fence)
            raise WorkingError('PREPARATION_ROOT_SCOPE')
        _require(isinstance(root, str) and os.path.isabs(root)
                 and os.path.realpath(root) == root and root != fence.root
                 and os.path.commonpath((root, fence.root)) == fence.root, "ROOT_UNSAFE")
        return GenerationStore._directory(None, root)

    def _startup_root(self, root, fence, authorization, root_bindings=None):
        if root_bindings is None and (authorization is None or authorization.root_bindings is None):
            return self._root(root, fence)
        roots = root_bindings if root_bindings is not None else authorization.root_bindings
        _require(authorization is None or root_bindings is None or authorization.root_bindings is roots,
                 'STARTUP_ROOT_SCOPE')
        # check_startup has authenticated the exact observation digest before
        # this opener is reached. Only selected working/private descendants and
        # the exact package root are readable through this route.
        if root == roots.path('packages', fence=fence):
            return roots.open_directory('packages', fence=fence)
        working = roots.path('working', fence=fence)
        _require(working == self.root and os.path.commonpath((root, working)) == working,
                 'STARTUP_ROOT_SCOPE')
        parts = () if root == working else tuple(os.path.relpath(root, working).split(os.sep))
        return roots.open_directory('working', parts, fence=fence)

    @staticmethod
    def _same(fd, path):
        held, visible = os.fstat(fd), os.stat(path, follow_symlinks=False)
        _require((held.st_dev, held.st_ino) == (visible.st_dev, visible.st_ino), "ROOT_DRIFT")

    @staticmethod
    def _pointer(parent):
        try:
            raw = GenerationStore._read(parent, SELECTOR, 512)
        except FileNotFoundError:
            return None
        pointer = history._json(raw)
        _require(isinstance(pointer, dict) and set(pointer) == {"working", "completion_sha256"}, "SELECTOR_SHAPE")
        digest = pointer["completion_sha256"]
        _require(isinstance(digest, str) and len(digest) == 64
                 and all(c in "0123456789abcdef" for c in digest)
                 and isinstance(pointer["working"], str)
                 and pointer["working"].startswith("w-") and len(pointer["working"]) == 66
                 and all(c in "0123456789abcdef" for c in pointer["working"][2:]), "SELECTOR_SHAPE")
        return pointer

    def _backups(self, fd, expected, fence):
        total = 0
        for name, digest in sorted(expected.items()):
            raw = GenerationStore._read(fd, name, MAX_RECOVERY_BYTES - total)
            total += len(raw)
            _require(_sha(raw) == digest, "RECOVERY_DRIFT")
        fence.check()

    def prepare(self, *, exports_root, active, scope, packages_root, fence=None,
                root_bindings=None, phase_guard=None):
        """Exclusive working copies, actual Packages backup paths, one selector.

        Existing backup files (including equal bytes) refuse: this draft has no
        custody grant for adopting them. Incomplete work remains held for review.
        """
        descriptors = []
        try:
            _require(callable(self.clock) and callable(self.admission) and isinstance(scope, Scope),
                     "DEPENDENCY_UNAVAILABLE")
            scope.validate(self.clock())
            active = copy.deepcopy(active)
            if root_bindings is not None:
                _require(callable(phase_guard), 'PREPARATION_AUTHORITY_UNAVAILABLE')
                phase_guard()
            class CheckedWrite:
                def check(inner):
                    if phase_guard is not None:
                        phase_guard()
                    fence.check()
            writer = fence if root_bindings is None else CheckedWrite()
            parent = self._root(self.root, fence, root_bindings)
            descriptors.append(parent)
            packages = self._root(packages_root, fence, root_bindings)
            descriptors.append(packages)
            _require(all(os.path.commonpath((a, b)) not in (a, b)
                         for a, b in ((self.root, packages_root), (self.root, exports_root),
                                      (packages_root, exports_root))), "ROOT_OVERLAP")
            _require(self._pointer(parent) is None and not os.listdir(parent), "ALREADY_STARTED")
            publisher = PrivateCutover(exports_root, clock=self.clock)
            publisher.inspect(expected_active=active, scope=scope, fence=fence,
                              root_bindings=root_bindings, phase_guard=phase_guard)
            exports = self._root(exports_root, fence, root_bindings)
            descriptors.append(exports)
            export = GenerationStore._directory(exports, active["export"])
            descriptors.append(export)
            seal = GenerationStore._read(export, "SEAL.json", MAX_META)
            decoded = history._json(seal)
            _require(_sha(seal) == active["seal_sha256"] and decoded["scope"] == vars(scope), "COPY_IDENTITY")
            jfd = GenerationStore._directory(export, "journal")
            descriptors.append(jfd)
            rfd = GenerationStore._directory(export, "recovery")
            descriptors.append(rfd)
            source_history = {name: GenerationStore._read(jfd, name, history.MAX_BYTES)
                              for name in decoded["history"]}
            recovery = {path.rsplit("/", 1)[1]: GenerationStore._read(rfd, path.rsplit("/", 1)[1], MAX_RECOVERY_BYTES)
                        for path in decoded["private_recovery_index"]}
            _require({k: _sha(v) for k, v in source_history.items()} == decoded["history"]
                     and all(_sha(recovery[path.rsplit("/", 1)[1]]) == decoded["recovery"][member]
                             for path, member in decoded["private_recovery_index"].items()), "COPY_HASH")
            pk = Packages(packages_root)
            _require(all(pk.local(path) == os.path.join(packages_root, path.rsplit("/", 1)[1])
                         for path in decoded["private_recovery_index"]), "RECOVERY_ROUTE")
            _require(len(recovery) == len(decoded["private_recovery_index"]), "RECOVERY_COLLISION")
            for name in recovery:
                try:
                    os.stat(name, dir_fd=packages, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                raise WorkingError("RECOVERY_EXISTS")
            binding = {"schema": "hbd.working_preparation.v1", "scope": vars(scope), "active": active,
                       "working_root": self.root, "packages_root": packages_root}
            _require(self.admission(history._canonical(binding)) is True, "ADMISSION_REFUSED")
            scope.validate(self.clock())
            writer.check()
            self._same(parent, self.root)
            self._same(packages, packages_root)
            # The App startup publisher durably installs its hold at this point:
            # admission has passed, and no working state or backup has been written.
            self.checkpoint("preparation_admitted")
            scope.validate(self.clock())
            writer.check()
            self._same(parent, self.root)
            self._same(packages, packages_root)
            # Name derives from immutable input and scope, not mutable Journal.
            name = "w-" + _sha(history._canonical(binding))
            writer.check()
            os.mkdir(name, mode=0o700, dir_fd=parent)
            work = GenerationStore._directory(parent, name)
            descriptors.append(work)
            writer.check()
            os.mkdir("baseline", mode=0o700, dir_fd=work)
            baseline = GenerationStore._directory(work, "baseline")
            descriptors.append(baseline)
            writer.check()
            os.mkdir("journal", mode=0o700, dir_fd=work)
            journal = GenerationStore._directory(work, "journal")
            descriptors.append(journal)
            self.checkpoint("working_created")
            for key, raw in sorted(source_history.items()):
                GenerationStore._write(baseline, key, raw, writer)
                GenerationStore._write(journal, key, raw, writer)
            writer.check()
            os.fsync(baseline)
            writer.check()
            os.fsync(journal)
            self.checkpoint("history_durable")
            for key, raw in sorted(recovery.items()):
                GenerationStore._write(packages, key, raw, writer)
            writer.check()
            os.fsync(packages)
            self.checkpoint("recovery_durable")
            backup_hashes = {key: _sha(raw) for key, raw in recovery.items()}
            self._backups(packages, backup_hashes, fence)
            _require(history.read_snapshot(os.path.join(self.root, name, "journal")) == source_history,
                     "INITIAL_HISTORY_DRIFT")
            publisher.inspect(expected_active=active, scope=scope, fence=fence, root_bindings=root_bindings, phase_guard=phase_guard)
            completion = {**binding, "working": name, "initial_sha256": history.fingerprint(source_history),
                          "baseline_files": {k: _sha(v) for k, v in source_history.items()},
                          "backups": backup_hashes,
                          "journal_identity": [os.fstat(journal).st_dev, os.fstat(journal).st_ino],
                          "packages_identity": [os.fstat(packages).st_dev, os.fstat(packages).st_ino]}
            raw = history._canonical(completion)
            _require(len(raw) <= MAX_META, "COMPLETION_SIZE")
            GenerationStore._write(work, COMPLETION, raw, writer)
            writer.check()
            os.fsync(work)
            writer.check()
            os.fsync(parent)
            self.checkpoint("completion_durable")
            pointer = {"working": name, "completion_sha256": _sha(raw)}
            temporary = ".working-" + secrets.token_hex(16)
            GenerationStore._write(parent, temporary, history._canonical(pointer), writer)
            scope.validate(self.clock())
            writer.check()
            self._same(parent, self.root)
            self._same(packages, packages_root)
            self._same(work, os.path.join(self.root, name))
            self._same(baseline, os.path.join(self.root, name, "baseline"))
            _require(set(os.listdir(baseline)) == set(source_history)
                     and all(GenerationStore._read(baseline, k, history.MAX_BYTES) == v
                             for k, v in source_history.items()), "BASELINE_DRIFT")
            self._same(journal, os.path.join(self.root, name, "journal"))
            _require(self._pointer(parent) is None, "SELECTOR_CHANGED")
            _require(history.read_snapshot(os.path.join(self.root, name, "journal")) == source_history,
                     "INITIAL_HISTORY_DRIFT")
            self._backups(packages, backup_hashes, fence)
            publisher.inspect(expected_active=active, scope=scope, fence=fence, root_bindings=root_bindings, phase_guard=phase_guard)
            _require(GenerationStore._read(work, COMPLETION, MAX_META) == raw, "COMPLETION_DRIFT")
            scope.validate(self.clock())
            writer.check()
            writer.check()
            os.replace(temporary, SELECTOR, src_dir_fd=parent, dst_dir_fd=parent)
            self.checkpoint("selector_replaced")
            writer.check()
            os.fsync(parent)
            self.checkpoint("selection_durable")
            return pointer
        except WorkingError:
            raise
        except (OSError, ValueError, RuntimeError, TypeError, KeyError, AttributeError):
            raise WorkingError("PREPARATION_REFUSED") from None
        finally:
            for fd in reversed(descriptors):
                os.close(fd)


    def check_startup(self, *, scope, pointer, packages_root, fence, authorization=None,
                      action="read", pending_sha256=None):
        if authorization is None:
            scope.validate(self.clock())
        else:
            from .deployer_startup_authorization import StartupAuthorization
            _require(isinstance(authorization, StartupAuthorization) and authorization.clock is self.clock, "STARTUP_AUTHORIZATION")
            authorization.check(scope=scope, pointer=pointer, working_root=self.root,
                                packages_root=packages_root, fence=fence, action=action,
                                pending_sha256=pending_sha256)

    def resolve(self, *, expected_pointer, scope, packages_root, fence=None, allow_pending=False, authorization=None,
                root_bindings=None, phase_guard=None):
        """Read-only restart: no reimport; progress is not initial-copy corruption.

        Without explicit startup authorization, scope must be fresh. Separate fresh
        startup grants can bind expired migration custody; production issuer is
        still unimplemented. Neither path grants another history import.
        """
        descriptors = []
        try:
            _require(callable(self.clock) and isinstance(scope, Scope), "DEPENDENCY_UNAVAILABLE")
            self.check_startup(scope=scope, pointer=expected_pointer, packages_root=packages_root,
                               fence=fence, authorization=authorization)
            _require(type(allow_pending) is bool, "PENDING_MODE")
            expected_pointer = copy.deepcopy(expected_pointer)
            if root_bindings is not None:
                _require(callable(phase_guard), 'PREPARATION_AUTHORITY_UNAVAILABLE')
                phase_guard()
            parent = self._startup_root(self.root, fence, authorization, root_bindings)
            descriptors.append(parent)
            observed = self._pointer(parent)
            _require(observed == expected_pointer, "SELECTOR_CHANGED")
            if observed is None:
                _require(not os.listdir(parent), "INCOMPLETE_PREPARATION")
                self._same(parent, self.root)
                fence.check()
                self.check_startup(scope=scope, pointer=expected_pointer, packages_root=packages_root,
                               fence=fence, authorization=authorization)
                return {"state": "UNSTARTED", "journal_root": None, "journal_activated": False}
            work = GenerationStore._directory(parent, observed["working"])
            descriptors.append(work)
            _require(set(os.listdir(work)) == {"journal", "baseline", COMPLETION}, "WORKING_SHAPE")
            raw = GenerationStore._read(work, COMPLETION, MAX_META)
            _require(_sha(raw) == observed["completion_sha256"], "COMPLETION_DRIFT")
            completed = history._json(raw)
            _require(set(completed) == {"schema", "scope", "active", "working_root", "packages_root", "working",
                                       "initial_sha256", "baseline_files", "backups", "journal_identity", "packages_identity"}
                     and completed["schema"] == "hbd.working_preparation.v1"
                     and completed["scope"] == vars(scope) and completed["working_root"] == self.root
                     and completed["packages_root"] == packages_root and completed["working"] == observed["working"],
                     "COMPLETION_BINDING")
            journal_root = os.path.join(self.root, observed["working"], "journal")
            journal = self._startup_root(journal_root, fence, authorization, root_bindings)
            descriptors.append(journal)
            packages = self._startup_root(packages_root, fence, authorization, root_bindings)
            descriptors.append(packages)
            _require(completed["journal_identity"] == [os.fstat(journal).st_dev, os.fstat(journal).st_ino]
                     and completed["packages_identity"] == [os.fstat(packages).st_dev, os.fstat(packages).st_ino],
                     "DIRECTORY_REPLACED")
            self._backups(packages, completed["backups"], fence)
            baseline = GenerationStore._directory(work, "baseline")
            descriptors.append(baseline)
            _require(set(completed["baseline_files"]) <= set(history.FILES)
                     and set(os.listdir(baseline)) == set(completed["baseline_files"]), "BASELINE_SHAPE")
            original, total = {}, 0
            for key, digest in sorted(completed["baseline_files"].items()):
                value = GenerationStore._read(baseline, key, history.MAX_BYTES - total)
                total += len(value)
                _require(_sha(value) == digest, "BASELINE_DRIFT")
                original[key] = value
            _require(history.fingerprint(original) == completed["initial_sha256"], "BASELINE_DRIFT")
            reader = read_working_snapshot if allow_pending else history.read_snapshot
            snapshot = reader(journal_root)
            pending = history._json(snapshot.get("txn.json", b"null"))
            if pending is not None:
                self.check_startup(scope=scope, pointer=expected_pointer, packages_root=packages_root,
                                   fence=fence, authorization=authorization, action="recover",
                                   pending_sha256=_sha(snapshot["txn.json"]))
            regular = {k: v for k, v in snapshot.items() if k != "txn.json"}
            initial, current = history._decode(original), history._decode(regular)
            _require(set(initial["ledger"]) <= set(current["ledger"]), "HISTORY_LOST")
            retained = {rec["request_id"]: rec for rec in current["installs"]}
            for rec in initial["installs"]:
                _require(rec["request_id"] in retained
                         and all(retained[rec["request_id"]].get(k) == v for k, v in rec.items()), "HISTORY_LOST")
            _require(current["audit"][:len(initial["audit"])] == initial["audit"], "HISTORY_LOST")
            qualify(regular, {}, {}, selected=(), now=self.clock())
            state = "RECOVERY_REQUIRED" if pending is not None else ("INITIAL" if history.fingerprint(snapshot) == completed["initial_sha256"] else "PROGRESSED")
            self.checkpoint("restart_observed")
            _require(reader(journal_root) == snapshot, "WORKING_DRIFT")
            self._backups(packages, completed["backups"], fence)
            _require(GenerationStore._read(work, COMPLETION, MAX_META) == raw, "COMPLETION_DRIFT")
            _require(self._pointer(parent) == observed, "SELECTOR_CHANGED")
            _require(set(os.listdir(baseline)) == set(original)
                     and all(GenerationStore._read(baseline, key, history.MAX_BYTES) == value
                             for key, value in original.items()), "BASELINE_DRIFT")
            for fd, path in ((parent, self.root), (work, os.path.join(self.root, observed["working"])),
                             (journal, journal_root), (packages, packages_root),
                             (baseline, os.path.join(self.root, observed["working"], "baseline"))):
                self._same(fd, path)
            self.check_startup(scope=scope, pointer=expected_pointer, packages_root=packages_root,
                               fence=fence, authorization=authorization)
            fence.check()
            if phase_guard is not None:
                phase_guard()
            return {"state": state, "journal_root": journal_root, "pending_sha256": _sha(snapshot["txn.json"]) if pending is not None else None,
                    "journal_activated": False,
                    "capture_trusted": False, "native_verified": False, "uninstall_allowed": False}
        except WorkingError:
            raise
        except (OSError, ValueError, RuntimeError, TypeError, KeyError, AttributeError):
            raise WorkingError("STARTUP_REFUSED") from None
        finally:
            for fd in reversed(descriptors):
                os.close(fd)
