"""0.3.23 (#985 plan steps M4/M5): move the old Deployer's history, each step after one owner Approve.

Owner pop-ups 2026-10-13: "Build both", "Yes, /data only", "Re-merge at Select", "7 days", "No tap after Select".

* ``house_brain_legacy_prepare_request.v1`` (M4) reads the stage Maintenance wrote under
  ``/share/house_brain_legacy_stage_v1/stage-<32 hex>/`` read-only, checks it byte for byte with
  Maintenance's own reader, merges it with this Deployer's live history, and after Approve writes a sealed
  copy and an export inside ``/data`` only. Nothing is selected.
* ``house_brain_legacy_select_request.v1`` (M5) re-reads the stage, merges again with the LATEST live
  history (nothing recorded since Prepare is lost), and after Approve prepares the working copy and
  publishes ``MIGRATED_STARTUP.json``. The next App start runs on the moved history.
* ``house_brain_legacy_unselect_request.v1`` renames that file aside (kept as evidence) before a restart.
  The App option ``return_to_previous_history`` does the same at startup when the App is held.

Every request names no path. Refusals never ask. Every write holds the enrolled shared writer lock, which is
never created here. Writes go only to ``/data/hbd-*-v1`` and the selection file; packages, ``/share`` and
the old App are never written.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
import re
import secrets
import stat
import threading
import time

from . import VERSION, history, release_status, writer_enroll
from . import ha as ha_mod
from .manifest import RE_REQUEST_ID, REQUESTERS, ManifestError, _obj, _str
from .writer import LOCK_NAME, WriterError, WriterLease

PREPARE = "house_brain_legacy_prepare_request.v1"
SELECT = "house_brain_legacy_select_request.v1"
UNSELECT = "house_brain_legacy_unselect_request.v1"
SCHEMAS = (PREPARE, SELECT, UNSELECT, "house_brain_legacy_reset_request.v1")

STAGE_ROOT = "/share/house_brain_legacy_stage_v1"
GENERATIONS, EXPORTS, WORKING, USES = ("hbd-generations-v1", "hbd-exports-v1", "hbd-working-v1",
                                       "hbd-startup-uses-v1")
FOLDERS = (GENERATIONS, EXPORTS, WORKING, USES)
MARKER = "MIGRATED_STARTUP.json"
SOURCE_INSTANCE = "local_house_brain_deployer"
WINDOW_SECONDS = 7 * 24 * 3600        # owner pop-up "7 days": Select must follow Prepare within this
PHASE_SECONDS = 900                   # the writes after one Approve must finish within 15 minutes
RE_STAGE = re.compile(r"stage-[0-9a-f]{32}")
RE_SHA = re.compile(r"[0-9a-f]{64}")


class LegacyError(ValueError):
    """Fixed, path-free reason code only."""


def _require(ok, reason):
    if not ok:
        raise LegacyError(reason)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def stage_root() -> str:
    if os.environ.get("HBD_TEST_MODE") == "1" and os.environ.get("HBD_LEGACY_STAGE_ROOT"):
        return os.environ["HBD_LEGACY_STAGE_ROOT"]
    return STAGE_ROOT


def destination_instance() -> str:
    return ha_mod.INSTANCE or "store_house_brain_deployer"


def candidate_sha256() -> str:
    identity = release_status.manifest_identity()
    return identity if RE_SHA.fullmatch(identity or "") else _sha(("hbd-" + VERSION).encode())


# ---------------------------------------------------------------- requests

def kind(raw: bytes) -> str | None:
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    return value.get("schema") if isinstance(value, dict) and value.get("schema") in SCHEMAS else None


def parse(raw: bytes) -> tuple[str, str, int, dict]:
    """(schema, request_id, tracking_issue, fields) or ManifestError. Exact fields; nothing names a path."""
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ManifestError("JSON") from None
    schema = data.get("schema") if isinstance(data, dict) else None
    extra = {PREPARE: {"stage", "snapshot_sha256"}, SELECT: {"prepared_request_id", "result_sha256"},
             UNSELECT: {"marker_sha256"}, "house_brain_legacy_reset_request.v1": set()}.get(schema)
    if extra is None:
        raise ManifestError("SCHEMA")
    obj = _obj(data, "legacy history", {"schema", "request_id", "requested_by", "tracking_issue"} | extra, set())
    rid = _str(obj["request_id"], "request_id", RE_REQUEST_ID, 64)
    if obj["requested_by"] not in REQUESTERS:
        raise ManifestError("REQUESTER")
    issue = obj["tracking_issue"]
    if isinstance(issue, bool) or not isinstance(issue, int) or not 1 <= issue <= 10_000_000:
        raise ManifestError("RANGE", "tracking_issue")
    fields = {}
    if schema == PREPARE:
        fields["stage"] = _str(obj["stage"], "stage", RE_STAGE, 38)
        fields["snapshot_sha256"] = _str(obj["snapshot_sha256"], "snapshot_sha256", RE_SHA, 64)
    elif schema == SELECT:
        fields["prepared_request_id"] = _str(obj["prepared_request_id"], "prepared_request_id", RE_REQUEST_ID, 64)
        fields["result_sha256"] = _str(obj["result_sha256"], "result_sha256", RE_SHA, 64)
    elif schema == UNSELECT:
        fields["marker_sha256"] = _str(obj["marker_sha256"], "marker_sha256", RE_SHA, 64)
    return schema, rid, issue, fields


def run_name(prefix: str, request_id: str) -> str:
    return prefix + "-" + _sha(request_id.encode())[:24]


# ---------------------------------------------------------------- read-only checks

def selection_present(data_dir: str) -> bool:
    try:
        os.lstat(os.path.join(data_dir, MARKER))
        return True
    except FileNotFoundError:
        return False


def _read_stage(stage: str, snapshot_sha256: str):
    """Maintenance's own reader, then the Deployer's byte contract. Returns the legacy history bytes."""
    from .migration.deployer_maintenance_stage import BridgeError, read_preparation
    from .migration.maintenance_legacy_snapshot import Refusal
    from .migration.maintenance_legacy_stage import read_stage
    fd = None
    try:
        fd = os.open(stage_root(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        receipt = read_stage(fd, stage).receipt()
        _require(receipt.get("snapshot_sha256") == snapshot_sha256, "LEGACY_SNAPSHOT_MISMATCH")
        prepared = read_preparation(fd, stage, expected_component_sha256=receipt["component_sha256"],
                                    expected_snapshot_sha256=snapshot_sha256, expected_files=receipt["files"],
                                    reader=read_stage)
        source = prepared.private_files()
        _require("txn.json" not in source, "LEGACY_SOURCE_TRANSACTION")
        return source, receipt
    except LegacyError:
        raise
    except Refusal as err:
        raise LegacyError(f"LEGACY_STAGE:{err}") from None
    except BridgeError as err:
        raise LegacyError(f"LEGACY_STAGE:{err}") from None
    except OSError:
        raise LegacyError("LEGACY_STAGE_UNAVAILABLE") from None
    finally:
        if fd is not None:
            os.close(fd)


def _merge(source: dict, destination: dict, now: float):
    from .migration.qualify_deployer_history import plan_chronology, qualify
    try:
        qualify(source, destination, {}, selected=(), now=now)
        return plan_chronology(source, destination, now=now)
    except history.HistoryError as err:
        raise LegacyError(f"LEGACY_HISTORY:{err}") from None


def counts(files: dict) -> dict:
    decoded = history._decode(files)
    return {"installs": len(decoded["installs"]), "ledger": len(decoded["ledger"]), "audit": len(decoded["audit"]),
            "undone": sum(1 for rec in decoded["installs"] if "undone_by" in rec),
            "frozen": decoded["freeze"] is not None}


def _folders_empty_or_absent(data_dir: str, names) -> None:
    for name in sorted(names):
        try:
            st = os.lstat(os.path.join(data_dir, name))
        except FileNotFoundError:
            continue
        _require(stat.S_ISDIR(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o700 and st.st_uid == os.geteuid(),
                 "LEGACY_FOLDER_SHAPE")
        _require(not os.listdir(os.path.join(data_dir, name)), "LEGACY_WORKING_NOT_EMPTY")


def inspect_prepare(data_dir: str, stage: str, snapshot_sha256: str, now: float) -> dict:
    _require(not selection_present(data_dir), "LEGACY_ALREADY_SELECTED")
    _folders_empty_or_absent(data_dir, (WORKING, USES))
    destination = _live(data_dir)
    source, receipt = _read_stage(stage, snapshot_sha256)
    planned = _merge(source, destination, now)
    return {"source": source, "destination": destination, "planned": planned, "receipt": receipt,
            "counts": counts(planned.private_files()), "legacy": counts(source)}


def _live(data_dir: str) -> dict:
    try:
        live = history.read_snapshot(data_dir)
        history._decode(live)
        return live
    except history.HistoryError as err:
        raise LegacyError(f"LEGACY_LIVE_HISTORY:{err}") from None
    except OSError:
        raise LegacyError("LEGACY_LIVE_HISTORY_UNAVAILABLE") from None


def _prepared_record(data_dir: str, prepared_request_id: str) -> dict:
    path = os.path.join(data_dir, GENERATIONS, "PREPARED-" + run_name("p", prepared_request_id) + ".json")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        raise LegacyError("LEGACY_NOT_PREPARED") from None
    try:
        st = os.fstat(fd)
        _require(stat.S_ISREG(st.st_mode) and st.st_size <= 65536 and stat.S_IMODE(st.st_mode) == 0o600
                 and st.st_uid == os.geteuid(), "LEGACY_PREPARED_RECORD")
        record = history._json(os.read(fd, 65537))
    finally:
        os.close(fd)
    _require(type(record) is dict and record.get("schema") == "hbd.legacy_prepared.v1"
             and record.get("request_id") == prepared_request_id, "LEGACY_PREPARED_RECORD")
    return record


def inspect_select(data_dir: str, prepared_request_id: str, result_sha256: str, now: float) -> dict:
    _require(not selection_present(data_dir), "LEGACY_ALREADY_SELECTED")
    _folders_empty_or_absent(data_dir, (WORKING, USES))
    record = _prepared_record(data_dir, prepared_request_id)
    _require(record["result_sha256"] == result_sha256, "LEGACY_PREPARED_MISMATCH")
    _require(history._finite_number(record["expires_at"]) and now < record["expires_at"], "LEGACY_PREPARE_EXPIRED")
    destination = _live(data_dir)
    source, _ = _read_stage(record["stage"], record["snapshot_sha256"])
    planned = _merge(source, destination, now)
    added = counts(planned.private_files())
    since = {k: added[k] - record["counts"][k] for k in ("installs", "ledger", "audit")}
    return {"source": source, "destination": destination, "planned": planned, "record": record,
            "counts": added, "since": since}


def _same_as_shown(fresh: dict, shown: dict) -> None:
    """Between the ask and the lock the legacy bytes must be identical, and no install or freeze may appear.

    The ask itself appends approval log lines (and results may land), so the live part may only grow there.
    """
    _require(fresh["planned"].source_sha256 == shown["planned"].source_sha256, "LEGACY_DRIFT")
    for key in ("installs", "undone", "frozen"):
        _require(fresh["counts"][key] == shown["counts"][key], "LEGACY_DRIFT")
    for key in ("ledger", "audit"):
        _require(fresh["counts"][key] >= shown["counts"][key], "LEGACY_DRIFT")


# ---------------------------------------------------------------- approval-bound admission

class PhaseAuthority:
    """One owner Approve, held in memory only: admits exactly the shown merge for at most 15 minutes.

    Never a StartupAuthorization; a recovery or startup check refuses it by type.
    """

    def __init__(self, *, scope, planned, fence, clock=time.time, should_stop=lambda: False):
        self.scope, self.fence, self.clock, self.should_stop = scope, fence, clock, should_stop
        self.expected = {"source_sha256": planned.source_sha256, "destination_sha256": planned.destination_sha256,
                         "result_sha256": planned.result_sha256}
        self.approved_at = clock()
        self.session = fence.session
        self.used = set()
        self.startup_binding = None

    def guard(self) -> None:
        now = self.clock()
        _require(history._finite_number(now) and self.approved_at <= now < self.approved_at + PHASE_SECONDS
                 and not self.should_stop() and self.fence.session == self.session, "LEGACY_PHASE_REVOKED")
        self.fence.check()

    def _once(self, name):
        _require(name not in self.used, "LEGACY_ADMISSION_REUSED")
        self.used.add(name)

    def admit_generation(self, seal: bytes) -> bool:
        self.guard()
        value = history._json(seal)
        ok = (value.get("schema") == "hbd.generation_preparation.v1" and value.get("scope") == vars(self.scope)
              and all(value.get(k) == v for k, v in self.expected.items())
              and value.get("selected") == [] and value.get("recovery") == {} and value.get("expected_head") is None)
        _require(ok, "LEGACY_ADMISSION_GENERATION")
        self._once("generation")
        return True

    def admit_export(self, binding: bytes) -> bool:
        self.guard()
        value = history._json(binding)
        _require(value.get("schema") == "hbd.private_cutover_admission.v1" and value.get("scope") == vars(self.scope)
                 and value.get("expected_active") is None, "LEGACY_ADMISSION_EXPORT")
        self._once("export")
        return True

    def admit_startup(self, binding: bytes) -> bool:
        self.guard()
        value = history._json(binding)
        _require(value.get("schema") == "hbd.startup_preparation.v2"
                 and value.get("purpose") == "initial_migration_prepare"
                 and value.get("scope") == vars(self.scope) and value.get("lease_session") == self.session,
                 "LEGACY_ADMISSION_STARTUP")
        self._once("startup")
        self.startup_binding = binding
        return True

    def verify_startup(self, binding: bytes, fence) -> bool:
        self.guard()
        _require(fence is self.fence, "LEGACY_PHASE_FENCE")
        if self.startup_binding is None:
            value = history._json(binding)
            _require(value.get("schema") == "hbd.startup_preparation.v2" and value.get("scope") == vars(self.scope),
                     "LEGACY_ADMISSION_STARTUP")
            return True
        return binding == self.startup_binding


# ---------------------------------------------------------------- writes (lock held by the caller)

def _private_dir(parent_fd: int, name: str, *, create: bool) -> None:
    try:
        st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        _require(create, "LEGACY_FOLDER_MISSING")
        os.mkdir(name, 0o700, dir_fd=parent_fd)
        st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    _require(stat.S_ISDIR(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o700 and st.st_uid == os.geteuid(),
             "LEGACY_FOLDER_SHAPE")


def ensure_folders(data_dir: str, fence) -> None:
    """Create only absent private folders inside /data (owner pop-up "Yes, /data only")."""
    fence.check()
    fd = os.open(data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for name in FOLDERS:
            _private_dir(fd, name, create=True)
        os.fsync(fd)
    finally:
        os.close(fd)


def _new_run(data_dir: str, folder: str, name: str, fence) -> str:
    fence.check()
    fd = os.open(os.path.join(data_dir, folder), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        try:
            os.mkdir(name, 0o700, dir_fd=fd)
        except FileExistsError:
            raise LegacyError("LEGACY_ALREADY_RUN") from None
        _private_dir(fd, name, create=False)
        os.fsync(fd)
    finally:
        os.close(fd)
    return os.path.join(data_dir, folder, name)


def root_bindings(data_dir: str, packages_root: str, writer_root: str):
    from .root_bindings import RootBindings, RootSpec
    specs = []
    for role, path in (("data", data_dir), ("working", os.path.join(data_dir, WORKING)),
                       ("uses", os.path.join(data_dir, USES)), ("packages", packages_root), ("writer", writer_root)):
        st = os.stat(path, follow_symlinks=False)
        _require(stat.S_ISDIR(st.st_mode), "LEGACY_ROOT_TYPE")
        specs.append(RootSpec(role, path, os.geteuid(), os.getegid(), stat.S_IMODE(st.st_mode)))
    return RootBindings(tuple(specs))


def make_scope(request_id: str, owner_id: str, expires_at: float):
    from .migration.deployer_history_store import Scope
    return Scope("legacy-" + _sha(request_id.encode())[:24], SOURCE_INSTANCE, destination_instance(), owner_id,
                 candidate_sha256(), float(expires_at))


def _stage_and_export(data_dir, run, source, destination, scope, authority, fence, bindings, clock):
    from .migration.deployer_history_store import GenerationStore
    from .migration.deployer_private_cutover import PrivateCutover
    generations = _new_run(data_dir, GENERATIONS, run, fence)
    exports = _new_run(data_dir, EXPORTS, run, fence)
    store = GenerationStore(generations, admission=authority.admit_generation, clock=clock)
    receipt = store.stage(source, destination, {}, selected=(), scope=scope, expected_head=None, fence=fence,
                          root_bindings=bindings, phase_guard=authority.guard)
    head = store.private_head()
    _require(head is not None, "LEGACY_GENERATION_MISSING")
    pointer = head[0]
    cutover = PrivateCutover(exports, admission=authority.admit_export, clock=clock)
    active, _ = cutover.publish(source_root=generations, expected_generation=pointer, expected_active=None,
                                scope=scope, fence=fence, root_bindings=bindings, phase_guard=authority.guard)
    return exports, active, receipt


def run_prepare(*, data_dir, packages_root, request_id, stage, snapshot_sha256, shown, owner_id,
                clock=time.time, should_stop=lambda: False) -> dict:
    """After Approve: lock, re-read, write the sealed copy and export. Never selects."""
    from .migration.deployer_history_store import GenerationStore
    with WriterLease(writer_enroll.root(), create=False) as fence:
        now = clock()
        fresh = inspect_prepare(data_dir, stage, snapshot_sha256, now)
        _same_as_shown(fresh, shown)
        ensure_folders(data_dir, fence)
        scope = make_scope(request_id, owner_id, now + WINDOW_SECONDS)
        authority = PhaseAuthority(scope=scope, planned=fresh["planned"], fence=fence, clock=clock,
                                   should_stop=should_stop)
        with root_bindings(data_dir, packages_root, fence.root) as bindings:
            run = run_name("p", request_id)
            _, active, receipt = _stage_and_export(data_dir, run, fresh["source"], fresh["destination"], scope,
                                                   authority, fence, bindings, clock)
        record = {"schema": "hbd.legacy_prepared.v1", "request_id": request_id, "stage": stage,
                  "snapshot_sha256": snapshot_sha256, "component_sha256": fresh["receipt"]["component_sha256"],
                  "source_sha256": fresh["planned"].source_sha256,
                  "destination_sha256": fresh["planned"].destination_sha256,
                  "result_sha256": fresh["planned"].result_sha256, "counts": fresh["counts"],
                  "scope": vars(scope), "expires_at": scope.expires_at, "export": active}
        raw = history._canonical(record)
        parent = os.open(os.path.join(data_dir, GENERATIONS), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            GenerationStore._write(parent, "PREPARED-" + run + ".json", raw, fence)
            os.fsync(parent)
        finally:
            os.close(parent)
        lock = os.stat(LOCK_NAME, dir_fd=fence._directory, follow_symlinks=False)
        return {"schema": "hbd.legacy_prepare.v1", "stage": stage, "snapshot_sha256": snapshot_sha256,
                "source_sha256": record["source_sha256"], "destination_sha256": record["destination_sha256"],
                "result_sha256": record["result_sha256"], "counts": record["counts"],
                "legacy_counts": fresh["legacy"], "generation_sha256": receipt["generation_sha256"],
                "expires_at": int(scope.expires_at), "lock": {"dev": lock.st_dev, "ino": lock.st_ino},
                "selected": False, "uninstall_allowed": False}


def run_select(*, data_dir, packages_root, request_id, prepared_request_id, result_sha256, shown, owner_id,
               clock=time.time, should_stop=lambda: False) -> dict:
    """After Approve: lock, re-merge with the latest live history, prepare the working copy, publish the marker."""
    from .migration.deployer_startup_service import prepare_startup
    with WriterLease(writer_enroll.root(), create=False) as fence:
        now = clock()
        fresh = inspect_select(data_dir, prepared_request_id, result_sha256, now)
        _same_as_shown(fresh, shown)
        ensure_folders(data_dir, fence)
        scope = make_scope(prepared_request_id, owner_id, fresh["record"]["expires_at"])
        authority = PhaseAuthority(scope=scope, planned=fresh["planned"], fence=fence, clock=clock,
                                   should_stop=should_stop)
        with root_bindings(data_dir, packages_root, fence.root) as bindings:
            exports, active, receipt = _stage_and_export(data_dir, run_name("s", request_id), fresh["source"],
                                                         fresh["destination"], scope, authority, fence, bindings,
                                                         clock)
            result = prepare_startup(data_dir=data_dir, working_root=os.path.join(data_dir, WORKING),
                                     exports_root=exports, active=active, scope=scope, packages_root=packages_root,
                                     uses_root=os.path.join(data_dir, USES), fence=fence,
                                     admission=authority.admit_startup, clock=clock, root_bindings=bindings,
                                     phase_verify=authority.verify_startup)
        _require(result.get("state") == "SELECTED_HELD", "LEGACY_SELECT_RESULT")
        with open(os.path.join(data_dir, MARKER), "rb") as fh:
            marker = fh.read()
        return {"schema": "hbd.legacy_select.v1", "prepared_request_id": prepared_request_id,
                "result_sha256": fresh["planned"].result_sha256, "counts": fresh["counts"], "since_prepare": fresh["since"],
                "generation_sha256": receipt["generation_sha256"], "marker_sha256": _sha(marker),
                "pointer": result["pointer"], "applies": "next Deployer start", "uninstall_allowed": False}


def withdraw(data_dir: str, *, expected_sha256: str | None, fence=None) -> str:
    """Rename the selection file aside (kept as evidence). Returns the new name."""
    path = os.path.join(data_dir, MARKER)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        raw = os.read(fd, 1024 * 1024 + 1)
    finally:
        os.close(fd)
    digest = _sha(raw)
    _require(expected_sha256 is None or digest == expected_sha256, "LEGACY_MARKER_MISMATCH")
    name = f"MIGRATED_STARTUP.withdrawn-{digest[:12]}-{int(time.time())}.json"
    if fence is not None:
        fence.check()
    os.rename(path, os.path.join(data_dir, name))
    parent = os.open(data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
    return name


def run_unselect(*, data_dir, marker_sha256) -> dict:
    with WriterLease(writer_enroll.root(), create=False) as fence:
        _require(selection_present(data_dir), "LEGACY_NOT_SELECTED")
        name = withdraw(data_dir, expected_sha256=marker_sha256, fence=fence)
    return {"schema": "hbd.legacy_unselect.v1", "withdrawn_as": name, "applies": "next Deployer start"}


def withdraw_at_startup(data_dir: str) -> str | None:
    """App option ``return_to_previous_history``: emergency path when a selected start is held.

    Takes the lock if it can (never creates it); renames the selection aside either way, because a held App
    cannot take requests. The previous live history in /data was never changed, so the next start uses it.
    """
    if not selection_present(data_dir):
        return None
    try:
        with WriterLease(writer_enroll.root(), create=False) as fence:
            return withdraw(data_dir, expected_sha256=None, fence=fence)
    except WriterError:
        return withdraw(data_dir, expected_sha256=None)


# ---------------------------------------------------------------- normal startup on the moved history

class ForwardIssuer:
    """Owner pop-up "No tap after Select": the Select-approved selection file is the authority.

    Issues fresh 30-second read grants and nonce permits for ForwardRuntime, in memory only, after re-checking
    that the selection file, its plan and the lock are exactly what this App opened. Only selections made by
    this module's Select (operation id ``legacy-…``, this App's instance) are honoured.
    """

    def __init__(self, data_dir, raw, plan, scope, bindings, clock=time.time):
        from .migration.deployer_startup_authorization import digest
        _require(type(scope.operation_id) is str and scope.operation_id.startswith("legacy-")
                 and scope.source_instance == SOURCE_INSTANCE and scope.destination_instance == destination_instance(),
                 "FORWARD_SELECTION_FOREIGN")
        self.data_dir, self.raw, self.plan, self.scope, self.bindings, self.clock = (
            data_dir, raw, plan, scope, bindings, clock)
        self._startup_id = "fwd-" + secrets.token_hex(12)
        self._scope_sha = digest(vars(scope))
        self._pointer_sha = digest(plan["pointer"])
        self._nonces = {}            # nonce -> grant expiry; pruned as they expire
        self._lock = threading.Lock()
        # ForwardRuntime pins verifier identity; a bound method is a new object on every access.
        self._grant_verifier, self._permit_verifier = self.verify_grant, self.verify_permit

    def _grant_ok(self, grant: dict) -> bool:
        now = self.clock()
        return (type(grant) is dict and grant.get("startup_id") == self._startup_id
                and grant.get("migration_scope_sha256") == self._scope_sha
                and grant.get("pointer_sha256") == self._pointer_sha
                and grant.get("working_root") == self.plan["working_root"]
                and grant.get("packages_root") == self.plan["packages_root"]
                and grant.get("common_root") == self.plan["common_root"]
                and grant.get("action") == "read" and grant.get("pending_sha256") is None
                and history._finite_number(grant.get("issued_at")) and history._finite_number(grant.get("expires_at"))
                and grant["issued_at"] <= now < grant["expires_at"] <= grant["issued_at"] + 30)

    def verify_grant(self, binding) -> bool:
        """One stable verifier for every grant this issuer made (ForwardRuntime pins its identity)."""
        try:
            value = history._json(binding)
            return (type(value) is dict and set(value) == {"schema", "grant"}
                    and value["schema"] == "hbd.startup_authorization.v1" and self._grant_ok(value["grant"]))
        except Exception:  # noqa: BLE001 - a malformed binding is simply not ours
            return False

    def verify_permit(self, binding) -> bool:
        try:
            value = history._json(binding)
            request, grant = value["request"], value["grant"]
            with self._lock:
                expiry = self._nonces.get(request.get("nonce"))
            return (set(value) == {"request", "grant"} and request.get("plan") == self.plan
                    and expiry is not None and self._grant_ok(grant) and grant["expires_at"] == expiry)
        except Exception:  # noqa: BLE001
            return False

    def authorize(self, request, fence):
        from .migration.deployer_forward_runtime import ForwardPermit
        from .migration.deployer_startup_authorization import StartupAuthorization, StartupGrant
        from .migration.deployer_startup_service import _read_marker
        _require(type(request) is dict and request.get("schema") == "hbd.forward_authorization.v1"
                 and request.get("plan") == self.plan and type(request.get("nonce")) is str
                 and request.get("purpose") in ("startup", "effect"), "FORWARD_REQUEST")
        _require(isinstance(fence, WriterLease) and fence.root == self.plan["common_root"], "FORWARD_FENCE")
        fence.check()
        _require(_read_marker(self.data_dir) == self.raw, "FORWARD_SELECTION_CHANGED")
        lock = os.stat(LOCK_NAME, dir_fd=fence._directory, follow_symlinks=False)
        now = self.clock()
        grant = StartupGrant(self._startup_id, now, now + 30, self._scope_sha, self._pointer_sha,
                             self.plan["working_root"], self.plan["packages_root"], fence.root,
                             (lock.st_dev, lock.st_ino), "read", None,
                             _sha(self.bindings.evidence(fence=fence)))
        with self._lock:
            self._nonces = {n: e for n, e in self._nonces.items() if e > now}
            self._nonces[request["nonce"]] = grant.expires_at
        auth = StartupAuthorization(grant, verifier=self._grant_verifier, clock=self.clock,
                                    root_bindings=self.bindings)
        return ForwardPermit(request["nonce"], request["purpose"], auth, self._permit_verifier)


# ---------------------------------------------------------------- 0.3.24: state, legacy Undo guard, reset, Go back

HISTORY_LINES = {
    "previous": "previous history (no migration selected)",
    "prepared": "previous history; the old Deployer's history is prepared (Select within 7 days)",
    "selected": "previous history; the moved history is selected and takes over at the next restart",
    "moved": "moved history (old Deployer's history merged in)",
    "moved_set_aside": "moved history, set aside: restart the Deployer to use your previous history",
}


def history_state(data_dir: str, *, running_moved: bool, now: float | None = None) -> str:
    """Read-only: which history is in use and where the migration stands (owner pop-up 2026-10-13)."""
    if running_moved:
        try:
            return "moved" if selection_present(data_dir) else "moved_set_aside"
        except OSError:
            return "moved"
    try:
        if selection_present(data_dir):
            return "selected"
        now = time.time() if now is None else now
        folder = os.path.join(data_dir, GENERATIONS)
        names = os.listdir(folder) if os.path.isdir(folder) else ()
    except OSError:
        return "previous"
    for name in sorted(names):
        if not (name.startswith("PREPARED-") and name.endswith(".json")):
            continue
        try:   # review 2026-10-13: one bad file must not hide another valid one
            with open(os.path.join(folder, name), "rb") as fh:
                record = history._json(fh.read(65537))
        except (OSError, ValueError, history.HistoryError):
            continue
        if isinstance(record, dict) and history._finite_number(record.get("expires_at")) \
                and now < record["expires_at"]:
            return "prepared"
    return "previous"


def legacy_install_ids(plan: dict, data_dir: str) -> frozenset:
    """Installs that came from the old Deployer: in the moved baseline, not in this App's previous history.

    Owner pop-up 2026-10-13 "Not offered by default": they are kept and shown in history, but never offered for Undo
    (Undo on an old record was never proven). Unreadable input yields "everything in the baseline", which only hides
    more buttons, never shows extra ones.
    """
    def ids(raw):
        return {rec.get("request_id") for rec in history._json(raw) if isinstance(rec, dict)}
    try:
        base = os.path.join(plan["working_root"], plan["pointer"]["working"], "baseline", "installs.json")
        with open(base, "rb") as fh:
            moved = ids(fh.read(history.MAX_BYTES + 1))
    except (OSError, KeyError, TypeError, ValueError, AttributeError, history.HistoryError):
        return frozenset({"*"})
    try:
        with open(os.path.join(data_dir, "installs.json"), "rb") as fh:
            previous = ids(fh.read(history.MAX_BYTES + 1))
    except FileNotFoundError:
        previous = set()
    except (OSError, ValueError, TypeError, history.HistoryError):
        return frozenset(moved) | {"*"}
    return frozenset(moved - previous)


RESET = "house_brain_legacy_reset_request.v1"


def reset_needed(data_dir: str) -> bool:
    """A failed Select left a working copy or startup-use behind, and nothing is selected."""
    if selection_present(data_dir):
        return False
    for name in (WORKING, USES):
        path = os.path.join(data_dir, name)
        if os.path.isdir(path) and os.listdir(path):
            return True
    return False


def run_reset(*, data_dir: str) -> dict:
    """After Approve and under the lock: rename the half-made working copy aside (kept, never deleted)."""
    with WriterLease(writer_enroll.root(), create=False) as fence:
        _require(not selection_present(data_dir), "LEGACY_ALREADY_SELECTED")
        _require(reset_needed(data_dir), "LEGACY_NOTHING_TO_RESET")
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        moved = []
        for name in (WORKING, USES):
            path = os.path.join(data_dir, name)
            if os.path.isdir(path) and os.listdir(path):
                aside = f"{name}.reset-{stamp}"
                fence.check()
                os.rename(path, os.path.join(data_dir, aside))
                moved.append(aside)
        parent = os.open(data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    return {"schema": "hbd.legacy_reset.v1", "set_aside": moved, "deleted": [], "next": "Select may be sent again"}


def go_back(data_dir: str) -> str | None:
    """Owner's page button: set the moved-history selection aside (same as Unselect / the option)."""
    if not selection_present(data_dir):
        return None
    with WriterLease(writer_enroll.root(), create=False) as fence:
        return withdraw(data_dir, expected_sha256=None, fence=fence)
