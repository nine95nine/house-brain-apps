"""Normal-entrypoint migration hold and explicit recovery-provider composition.

The private marker is a hold signal, never an authority receipt. Explicitly
admitted preparation can publish it. Explicit source-injected recovery and forward
providers are separate; no installed issuer or default activation exists.
"""
from dataclasses import dataclass
import os
import secrets
import stat

from .. import history
from ..engine import Settings
from ..writer import WriterLease, LOCK_NAME
from .deployer_history_store import GenerationStore, Scope, MAX_META
from .deployer_recovery_runtime import recover_working
from .deployer_startup_authorization import StartupAuthorization
from .deployer_working_journal import WorkingJournal, WorkingError, _require

MARKER = "MIGRATED_STARTUP.json"


def prepare_startup(*, data_dir, working_root, exports_root, active, scope,
                    packages_root, uses_root, fence=None, admission=None,
                    clock=None, checkpoint=None, root_bindings=None, phase_verify=None):
    """Publish the startup hold before preparing and selecting working history.

    The trusted caller must separately establish installed quiescence/custody and
    supply one-use admission for the complete startup-preparation binding. This
    function supplies no issuer or runtime route. Existing markers always refuse;
    any partial work is retained under the hold, never retried or cleaned up.
    """
    descriptors = []
    try:
        _require(type(fence) is WriterLease and type(scope) is Scope
                 and callable(admission) and callable(clock)
                 and (checkpoint is None or callable(checkpoint)), "STARTUP_PREPARATION_DEPENDENCY")
        checkpoint = checkpoint or (lambda _: None)
        active = history._json(history._canonical(active))
        scope_pin = history._canonical(vars(scope))
        common = fence.root
        roots = (data_dir, working_root, exports_root, packages_root, uses_root)
        if root_bindings is None:
            _require(all(type(root) is str and os.path.isabs(root) and os.path.realpath(root) == root
                     and root != common and os.path.commonpath((root, common)) == common for root in roots)
                 and all(os.path.commonpath((a, b)) not in (a, b)
                         for i, a in enumerate(roots) for b in roots[i + 1:]), "STARTUP_PREPARATION_ROOTS")
        else:
            from ..root_bindings import RootBindings
            _require(type(root_bindings) is RootBindings and callable(phase_verify),
                     'STARTUP_PREPARATION_AUTHORITY')
            _require(all(root_bindings.path(role, fence=fence) == path for role, path in (
                     ('data', data_dir), ('working', working_root), ('uses', uses_root),
                     ('packages', packages_root), ('writer', common))), 'STARTUP_PREPARATION_ROOTS')
            _require(exports_root != data_dir and os.path.commonpath((data_dir, exports_root)) == data_dir
                     and all(os.path.commonpath((exports_root, other)) not in (exports_root, other)
                             for other in (working_root, uses_root, packages_root)), 'STARTUP_PREPARATION_ROOTS')
        for root in roots:
            descriptors.append(WorkingJournal._root(root, fence, root_bindings))
        parent = descriptors[0]
        _require(not os.listdir(descriptors[-1]), "STARTUP_USES_NOT_EMPTY")
        _require(_read_marker(data_dir) is None, "STARTUP_ALREADY_SELECTED")
        binding = {"schema": "hbd.startup_preparation.v1", "scope": vars(scope),
                   "active": active, "common_root": common, "data_root": data_dir,
                   "working_root": working_root, "exports_root": exports_root,
                   "packages_root": packages_root, "uses_root": uses_root}
        session = fence.session
        root_evidence = None if root_bindings is None else root_bindings.evidence(fence=fence)
        started = clock()
        if root_bindings is not None:
            import hashlib
            lock = os.stat(LOCK_NAME, dir_fd=fence._directory, follow_symlinks=False)
            binding.update(schema='hbd.startup_preparation.v2', purpose='initial_migration_prepare',
                           root_bindings_sha256=hashlib.sha256(root_evidence).hexdigest(),
                           lease_identity=[lock.st_dev, lock.st_ino], lease_session=session,
                           expected_pointer=None)
        binding_raw = history._canonical(binding)
        def phase_guard():
            if root_bindings is None:
                return
            def pin():
                now = clock()
                _require(history._finite_number(now) and history._finite_number(started)
                         and started <= now < started + 30 and fence.session == session
                         and history._canonical(vars(scope)) == scope_pin
                         and root_bindings.evidence(fence=fence) == root_evidence,
                         'STARTUP_PREPARATION_REVOKED')
                fence.check()
            pin()
            _require(phase_verify(binding_raw, fence) is True, 'STARTUP_PREPARATION_ISSUER')
            pin()
        class CheckedWrite:
            def check(inner):
                phase_guard()
                fence.check()
        writer = fence if root_bindings is None else CheckedWrite()
        phase_guard()
        held_raw = history._canonical({"schema": "hbd.migrated_startup.pending.v1",
                                       "preparation": binding})
        _require(len(held_raw) <= MAX_META, "STARTUP_PREPARATION_SIZE")
        admitted = False
        marker_expected = held_raw

        def dependencies(expected_marker):
            phase_guard()
            scope.validate(clock())
            _require(history._canonical(vars(scope)) == scope_pin and fence.root == common,
                     "STARTUP_PREPARATION_CHANGED")
            fence.check()
            for fd, root in zip(descriptors, roots):  # noqa: B905 - pairing kept as written (no strict)
                WorkingJournal._same(fd, root)
            _require(not os.listdir(descriptors[-1]), "STARTUP_USES_NOT_EMPTY")
            _require(_read_marker(data_dir) == expected_marker, "STARTUP_SELECTION_CHANGED")

        def admit(raw):
            nonlocal admitted
            dependencies(None)
            expected = {"schema": "hbd.working_preparation.v1", "scope": vars(scope),
                        "active": active, "working_root": working_root, "packages_root": packages_root}
            _require(raw == history._canonical(expected) and not admitted,
                     "STARTUP_PREPARATION_CHANGED")
            _require(admission(binding_raw) is True, "STARTUP_PREPARATION_REFUSED")
            dependencies(None)
            admitted = True
            return True

        def stage(name):
            if name == "preparation_admitted":
                _require(admitted, "STARTUP_PREPARATION_REFUSED")
                dependencies(None)
                GenerationStore._write(parent, MARKER, held_raw, writer)
                writer.check()
                os.fsync(parent)
                dependencies(held_raw)
                checkpoint("startup_hold_durable")
            dependencies(marker_expected)
            checkpoint(name)
            dependencies(marker_expected)

        working = WorkingJournal(working_root, admission=admit, clock=clock, checkpoint=stage)
        pointer = working.prepare(exports_root=exports_root, active=active, scope=scope,
                                  packages_root=packages_root, fence=fence,
                                  root_bindings=root_bindings, phase_guard=phase_guard if root_bindings is not None else None)
        dependencies(held_raw)
        selected = working.resolve(expected_pointer=pointer, scope=scope, packages_root=packages_root,
                                   fence=fence, root_bindings=root_bindings, phase_guard=phase_guard if root_bindings is not None else None)
        _require(selected["state"] == "INITIAL" and selected["journal_activated"] is False,
                 "STARTUP_INITIAL_READBACK")
        plan = {"schema": "hbd.migrated_startup.v1" if root_bindings is None else "hbd.migrated_startup.v2",
                "scope": vars(scope), "pointer": pointer,
                "common_root": common, "working_root": working_root,
                "packages_root": packages_root, "uses_root": uses_root}
        raw = history._canonical(plan)
        _plan(raw)
        temporary = ".startup-" + secrets.token_hex(16)
        GenerationStore._write(parent, temporary, raw, writer)
        checkpoint("startup_selection_prepared")
        dependencies(held_raw)
        # Re-read actual working state after the last external checkpoint.
        _require(working.resolve(expected_pointer=pointer, scope=scope, packages_root=packages_root,
                                 fence=fence, root_bindings=root_bindings, phase_guard=phase_guard if root_bindings is not None else None) == selected, "STARTUP_INITIAL_READBACK")
        _require(GenerationStore._read(parent, temporary, MAX_META) == raw, "STARTUP_SELECTION_CHANGED")
        dependencies(held_raw)
        os.replace(temporary, MARKER, src_dir_fd=parent, dst_dir_fd=parent)
        marker_expected = raw
        checkpoint("startup_selection_replaced")
        writer.check()
        os.fsync(parent)
        checkpoint("startup_selection_durable")
        dependencies(raw)
        _require(working.resolve(expected_pointer=pointer, scope=scope, packages_root=packages_root,
                                 fence=fence, root_bindings=root_bindings, phase_guard=phase_guard if root_bindings is not None else None) == selected, "STARTUP_INITIAL_READBACK")
        dependencies(raw)
        return {"state": "SELECTED_HELD", "pointer": pointer, "journal_activated": False,
                "capture_trusted": False, "native_verified": False, "uninstall_allowed": False}
    except WorkingError:
        raise
    except Exception:  # noqa: BLE001 - fail closed to a fixed refusal code
        raise WorkingError("STARTUP_PREPARATION_HELD") from None
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


@dataclass(frozen=True)
class RecoveryProvider:
    """Internal dependencies only; supplying this object does not prove trust.

    The supplied authorization must independently verify every existing exact
    scope/root/lease/pending binding. No options/environment selects a provider.
    """
    authorize: object
    manifest_for: object
    fence: object = None


def _read_marker(data_dir):
    """Absent is the accepted ordinary path; any present invalid marker holds."""
    try:
        os.lstat(os.path.join(data_dir, MARKER))
    except FileNotFoundError:
        # A dangling data-root symlink is not an absent marker.
        _require(os.path.isabs(data_dir) and os.path.realpath(data_dir) == data_dir,
                 "STARTUP_DATA_ROOT")
        return None
    # Owner pop-up 2026-10-12 "Accept 0755 as observed": the installed App's /data is 0755 (#454 6117189087).
    # The data root may be 0700 or 0755 exactly, owned by this App; the marker file itself stays 0600.
    parent = os.open(data_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    st = os.fstat(parent)
    if st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) not in (0o700, 0o755):
        os.close(parent)
        raise WorkingError("STARTUP_DATA_ROOT")
    try:
        raw = GenerationStore._read(parent, MARKER, MAX_META)
        visible, held = os.stat(data_dir, follow_symlinks=False), os.fstat(parent)
        _require((visible.st_dev, visible.st_ino) == (held.st_dev, held.st_ino),
                 "STARTUP_DATA_DRIFT")
        return raw
    finally:
        os.close(parent)


def _plan(raw):
    value = history._json(raw)
    _require(type(value) is dict and set(value) == {
        "schema", "scope", "pointer", "common_root", "working_root",
        "packages_root", "uses_root"} and value["schema"] in (
            "hbd.migrated_startup.v1", "hbd.migrated_startup.v2"),
        "STARTUP_MARKER_SHAPE")
    _require(type(value["scope"]) is dict and set(value["scope"]) == {
        "operation_id", "source_instance", "destination_instance", "owner_id", "candidate_sha256", "expires_at"},
        "STARTUP_SCOPE_SHAPE")
    scope = Scope(**value["scope"])
    scope.validate(scope.expires_at - 1)  # Historical custody, not fresh authority.
    pointer = value["pointer"]
    _require(type(pointer) is dict and set(pointer) == {"working", "completion_sha256"},
             "STARTUP_POINTER_SHAPE")
    roots = tuple(value[key] for key in ("common_root", "working_root", "packages_root", "uses_root"))
    _require(all(type(root) is str and os.path.isabs(root) and os.path.realpath(root) == root
                 for root in roots), "STARTUP_ROOTS")
    common, *children = roots
    if value['schema'] == 'hbd.migrated_startup.v1':
        _require(all(root != common and os.path.commonpath((common, root)) == common for root in children),
                 'STARTUP_ROOTS')
    else:
        _require(all(os.path.commonpath((common, root)) not in (common, root) for root in children),
                 'STARTUP_ROOTS')
    _require(all(os.path.commonpath((a, b)) not in (a, b)
                 for i, a in enumerate(children) for b in children[i + 1:]), "STARTUP_ROOTS")
    return value, scope


def startup_hold(data_dir, *, provider=None, ha=None, settings_for=None):
    """Called before normal Journal/Service/credential construction.

    No marker and no provider preserves ordinary startup. With a marker, missing
    authority holds before constructing dependencies. RecoveryProvider may recover
    ONLY the selected pending operation and still holds afterward. ForwardProvider
    separately requires fresh, nonce-bound permits for the selected Service.
    No exception detail/private marker data is published through the entrypoint.
    """
    try:
        raw = _read_marker(data_dir)
        if raw is None:
            _require(provider is None, "STARTUP_SELECTION_ABSENT")
            return
        from .deployer_forward_runtime import ForwardProvider, ForwardRuntime
        if type(provider) is ForwardProvider:
            plan, scope = _plan(raw)
            return ForwardRuntime.open(data_dir, raw, plan, scope, provider)
        _require(type(provider) is RecoveryProvider and callable(provider.authorize)
                 and callable(provider.manifest_for), "STARTUP_PROVIDER_UNAVAILABLE")
        _require(ha is not None and callable(settings_for), "STARTUP_RECOVERY_SETTINGS")
        plan, scope = _plan(raw)
        authorize, manifest_for = provider.authorize, provider.manifest_for
        fence = provider.fence
        _require(type(fence) is WriterLease and fence.root == plan["common_root"],
                 "STARTUP_FENCE_UNAVAILABLE")
        fence.check()
        def dependencies():
            fence.check()
            _require(provider.authorize is authorize and provider.manifest_for is manifest_for
                     and provider.fence is fence
                     and _read_marker(data_dir) == raw,
                     "STARTUP_PROVIDER_OR_SELECTION_CHANGED")
        dependencies()
        # Independent callback gets a copy; it cannot mutate selected roots/scope.
        authorization = authorize(history._json(raw), fence)
        dependencies()
        _require(type(authorization) is StartupAuthorization,
                 "STARTUP_AUTHORIZATION_UNAVAILABLE")
        def authorized():
            dependencies()
            authorization.check(scope=scope, pointer=plan["pointer"],
                                working_root=plan["working_root"], packages_root=plan["packages_root"],
                                fence=fence)
            if plan['schema'] == 'hbd.migrated_startup.v2':
                from ..root_bindings import RootBindings
                roots = authorization.root_bindings
                _require(type(roots) is RootBindings and roots.path('data') == data_dir
                         and roots.path('uses') == plan['uses_root'], 'STARTUP_ROOT_BINDINGS')
            dependencies()
        authorized()
        settings = settings_for()
        authorized()
        _require(type(settings) is Settings and settings.board is None and settings.dry_run is False
                 and settings.owner_user_id == scope.owner_id, "STARTUP_RECOVERY_SETTINGS")
        def manifest(request_id):
            dependencies()
            result = manifest_for(request_id)
            dependencies()
            return result
        result = recover_working(
            working_root=plan["working_root"], packages_root=plan["packages_root"],
            uses_root=plan["uses_root"], scope=scope, pointer=plan["pointer"],
            fence=fence, authorization=authorization, ha=ha, settings=settings,
            manifest_for=manifest)
        dependencies()
        _require(type(result) is dict and result.get("state") in ("READY_FIXTURE", "HELD")
                 and all(result.get(key) is False for key in (
                     "journal_activated", "capture_trusted", "native_verified", "uninstall_allowed")),
                 "STARTUP_RECOVERY_RESULT")
        raise WorkingError("STARTUP_FORWARD_AUTHORITY_UNAVAILABLE")
    except WorkingError:
        raise
    except Exception:  # noqa: BLE001 - fail closed to a fixed refusal code
        raise WorkingError("STARTUP_PROVIDER_HELD") from None
