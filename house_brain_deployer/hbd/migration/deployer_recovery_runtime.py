"""Single recovery-only construction boundary; not wired into the installed App.

Requires an explicitly supplied, independently verified startup authorization and
caller-retained common fence. No issuer, grant renewal, import, forward Service,
fallback Journal, installation claim or native execution evidence is provided.
"""
import os
from .. import history
from ..engine import Engine, Settings
from ..fsops import Packages
from ..journal import Journal
from ..writer import WriterLease
from .deployer_history_store import Scope, GenerationStore
from .deployer_startup_authorization import StartupAuthorization
from .deployer_startup_recovery import recover_pending
from .deployer_startup_use import StartupRecoveryUse
from .deployer_working_journal import WorkingJournal, WorkingError, _require


def inspect_working(*, working_root, packages_root, uses_root, scope, pointer,
                    fence=None, authorization=None):
    """Freshly authenticate pending selection without constructing effect clients.

    Separate roots are observations, not permission for the existing absolute-path
    Journal/Packages mutation route. No durable use is consumed by inspection.
    """
    fd = None
    try:
        _require(type(fence) is WriterLease and type(scope) is Scope
                 and type(authorization) is StartupAuthorization
                 and callable(authorization.verifier) and callable(authorization.clock),
                 'RUNTIME_DEPENDENCY')
        pins = (authorization.grant, authorization.verifier, authorization.clock,
                authorization.root_bindings)
        binding = history._canonical({'scope': vars(scope), 'pointer': pointer})
        roots = authorization.root_bindings
        if roots is None:
            paths = (working_root, packages_root, uses_root)
            _require(all(type(r) is str and os.path.isabs(r) and os.path.realpath(r) == r
                         and r != fence.root and os.path.commonpath((r, fence.root)) == fence.root for r in paths)
                     and all(os.path.commonpath((a, b)) not in (a, b)
                             for i, a in enumerate(paths) for b in paths[i + 1:]), 'RUNTIME_ROOTS')
        else:
            from ..root_bindings import RootBindings
            _require(type(roots) is RootBindings and roots.path('uses') == uses_root,
                     'RUNTIME_ROOTS')
        working = WorkingJournal(working_root, clock=authorization.clock)
        # Authenticate the observation before opening even the use directory.
        working.check_startup(scope=scope, pointer=pointer, packages_root=packages_root,
                              fence=fence, authorization=authorization)
        fd = (GenerationStore._directory(None, uses_root) if roots is None
              else roots.open_directory('uses'))
        selected = working.resolve(expected_pointer=pointer, scope=scope, packages_root=packages_root,
                                   fence=fence, allow_pending=True, authorization=authorization)
        _require(selected['state'] == 'RECOVERY_REQUIRED', 'NO_PENDING_RECOVERY')
        working.check_startup(scope=scope, pointer=pointer, packages_root=packages_root,
                              fence=fence, authorization=authorization, action='recover',
                              pending_sha256=selected['pending_sha256'])
        _require(authorization.grant is pins[0] and authorization.verifier is pins[1]
                 and authorization.clock is pins[2] and authorization.root_bindings is pins[3]
                 and history._canonical({'scope': vars(scope), 'pointer': pointer}) == binding,
                 'RUNTIME_DEPENDENCY_CHANGED')
        WorkingJournal._same(fd, uses_root)
        if roots is not None:
            roots.check()
        fence.check()
        return selected
    except WorkingError:
        raise
    except Exception:  # noqa: BLE001 - fail closed to a fixed refusal code
        raise WorkingError('RUNTIME_HELD') from None
    finally:
        if fd is not None:
            os.close(fd)


def recover_working(*, working_root, packages_root, uses_root, scope, pointer,
                    fence=None, authorization=None, ha=None, settings=None, manifest_for=None):
    """Resolve and construct actual Journal/Packages/Engine without exporting them.

    Roots must already exist and satisfy existing private custody checks. The
    caller retains the lease for the entire call. The existing <=30s startup grant
    stays bounded; an approval wait that outlasts it holds with no automatic retry.
    Returned fixture status cannot satisfy Maintenance VerifiedExecution.
    """
    state_files = packages = None
    try:
        _require(type(fence) is WriterLease and type(scope) is Scope
                 and type(authorization) is StartupAuthorization
                 and callable(authorization.verifier) and callable(authorization.clock)
                 and ha is not None and type(settings) is Settings and settings.board is None
                 and settings.dry_run is False and callable(manifest_for), 'RUNTIME_DEPENDENCY')
        bindings = authorization.root_bindings
        if bindings is not None:
            inspect_working(working_root=working_root, packages_root=packages_root, uses_root=uses_root,
                            scope=scope, pointer=pointer, fence=fence, authorization=authorization)
        else:
            roots = (working_root, packages_root, uses_root)
            _require(all(type(r) is str and os.path.isabs(r) and os.path.realpath(r) == r
                     and r != fence.root and os.path.commonpath((r, fence.root)) == fence.root for r in roots)
                 and all(os.path.commonpath((a, b)) not in (a, b)
                         for i, a in enumerate(roots) for b in roots[i + 1:]), 'RUNTIME_ROOTS')
        fd = (GenerationStore._directory(None, uses_root) if bindings is None
              else bindings.open_directory('uses'))
        os.close(fd)
        pins = authorization.grant, authorization.verifier, authorization.clock, bindings
        binding = history._canonical({'scope': vars(scope), 'pointer': pointer})
        def dependencies():
            _require(authorization.grant is pins[0] and authorization.verifier is pins[1]
                     and authorization.clock is pins[2]
                     and authorization.root_bindings is pins[3]
                     and history._canonical({'scope': vars(scope), 'pointer': pointer}) == binding,
                     'RUNTIME_DEPENDENCY_CHANGED')
        working = WorkingJournal(working_root, clock=authorization.clock)
        selected = working.resolve(expected_pointer=pointer, scope=scope, packages_root=packages_root,
                                   fence=fence, allow_pending=True, authorization=authorization)
        _require(selected['state'] == 'RECOVERY_REQUIRED', 'NO_PENDING_RECOVERY')
        working.check_startup(scope=scope, pointer=pointer, packages_root=packages_root,
                              fence=fence, authorization=authorization, action='recover',
                              pending_sha256=selected['pending_sha256'])
        if bindings is None:
            packages = Packages(packages_root, mutation_guard=fence.check)
            journal = Journal(selected['journal_root'])
        else:
            from .deployer_bound_authority import BoundRecoveryGuard
            from .deployer_bound_files import BoundStateFiles
            from .deployer_bound_packages import BoundPackageFiles
            guard = BoundRecoveryGuard(working, authorization, scope, pointer, packages_root,
                                       fence, selected['pending_sha256'])
            state_files = BoundStateFiles(bindings, fence, pointer['working'], guard)
            packages = BoundPackageFiles(bindings, fence, guard)
            journal = Journal(selected['journal_root'], file_io=state_files)
        observed = working.resolve(expected_pointer=pointer, scope=scope, packages_root=packages_root,
                                   fence=fence, allow_pending=True, authorization=authorization)
        _require(observed == selected, 'RUNTIME_SELECTION_CHANGED')
        dependencies()
        use = StartupRecoveryUse(uses_root, scope=scope, pointer=pointer, working_root=working_root,
                                 packages_root=packages_root, fence=fence, authorization=authorization)
        engine = Engine(ha, None, packages, journal, settings)
        def manifest(request_id):
            dependencies()
            result = manifest_for(request_id)
            dependencies()
            return result
        def admit(raw):
            dependencies()
            result = use.consume(raw)
            dependencies()
            return result
        result = recover_pending(working, pointer=pointer, scope=scope, packages_root=packages_root,
                               fence=fence, engine=engine, manifest_for=manifest,
                               admission=admit, authorization=authorization)
        dependencies()
        return result
    except WorkingError:
        raise
    except Exception:  # noqa: BLE001 - fail closed to a fixed refusal code
        raise WorkingError('RUNTIME_HELD') from None
    finally:
        if state_files is not None:
            state_files.close()
        if packages is not None and hasattr(packages, 'close'):
            packages.close()
