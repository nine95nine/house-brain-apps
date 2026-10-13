"""Inactive pending-operation startup bridge; no trusted provider or App consumer."""
import hashlib

from .. import history
from ..engine import Engine, Prepared, FAILED_MANUAL
from ..manifest import Manifest, RE_SHA256
from .deployer_working_journal import WorkingError, _require
from .deployer_pending_snapshot import read_working_snapshot

PHASES = {'APPLYING', 'CHECKING', 'AWAITING_RESTART', 'RESTARTING', 'HEALTH',
          'ROLLING_BACK', 'ROLLBACK_RESTARTING', 'AWAITING_ROLLBACK_RESTART'}


class _GuardedCalls:
    """Guard before/after boundary calls, including a returned HA websocket.

    A call admitted while fresh can complete after expiry; no subsequent effect
    may use that elapsed grant. Socket close remains available for cleanup.
    """
    def __init__(self, target, guard, *, socket=False):
        self._target, self._guard, self._socket = target, guard, socket

    def __getattr__(self, name):
        value = getattr(self._target, name)
        if not callable(value):
            return value
        if self._socket and name == 'close':
            return value
        def guarded(*args, **kwargs):
            self._guard()
            result = value(*args, **kwargs)
            try:
                self._guard()
            except BaseException:
                if name == 'ws' and result is not None:
                    result.close()
                raise
            return _GuardedCalls(result, self._guard, socket=True) if name == 'ws' else result
        return guarded


def recover_pending(working, *, pointer, scope, packages_root, fence, engine=None,
                    manifest_for=None, admission=None, authorization=None):
    """Exact pending bytes/manifest/step-plan admission before existing recovery.

    Fixture callbacks grant no production authority. Caller holds fence throughout.
    Returns READY only after recovery clears txn; never starts forward deployment.
    """
    try:
        _require(isinstance(engine, Engine) and callable(manifest_for) and callable(admission),
                 'RECOVERY_DEPENDENCY_UNAVAILABLE')
        selected = working.resolve(expected_pointer=pointer, scope=scope, packages_root=packages_root,
                                   fence=fence, allow_pending=True, authorization=authorization)
        _require(selected['state'] == 'RECOVERY_REQUIRED', 'NO_PENDING_RECOVERY')
        if authorization is not None and authorization.root_bindings is not None:
            from ..journal import Journal
            from .deployer_bound_authority import BoundRecoveryGuard
            from .deployer_bound_files import BoundStateFiles
            from .deployer_bound_packages import BoundPackageFiles
            roots = authorization.root_bindings
            files = getattr(engine.j, '_files', None)
            guard = getattr(files, '_guard', None)
            _require(type(engine.j) is Journal and type(files) is BoundStateFiles
                     and type(engine.pk) is BoundPackageFiles and type(guard) is BoundRecoveryGuard
                     and engine.j._file_pin is files
                     and files._roots is roots and engine.pk._roots is roots
                     and files._lease is fence and engine.pk._lease is fence
                     and files._parts == (pointer['working'], 'journal')
                     and engine.pk._guard is guard and guard.authorization is authorization
                     and guard.fence is fence and guard.scope is scope
                     and guard.pointer == pointer and guard.packages_root == packages_root
                     and guard.pending_sha256 == selected['pending_sha256'],
                     'RECOVERY_BOUND_CUSTODY')
            guard()
        _require(engine.j.dir == selected['journal_root'] and engine.pk.root == packages_root
                 and getattr(engine.pk.mutation_guard, '__self__', None) is fence
                 and getattr(engine.pk.mutation_guard, '__name__', None) == 'check', 'ENGINE_CUSTODY')
        raw = read_working_snapshot(engine.j.dir)['txn.json']
        _require(hashlib.sha256(raw).hexdigest() == selected['pending_sha256'], 'TRANSACTION_DRIFT')
        txn = history._json(raw)
        _require(isinstance(txn, dict) and isinstance(txn.get('request_id'), str)
                 and txn.get('phase') in PHASES and isinstance(txn.get('pre'), dict)
                 and isinstance(txn.get('facts'), dict) and type(txn.get('restarted')) is bool
                 and isinstance(txn.get('unchanged'), dict), 'TRANSACTION_SHAPE')
        m = manifest_for(txn['request_id'])
        _require(isinstance(m, Manifest) and m.request_id == txn['request_id']
                 and m.digest == txn.get('digest') and not m.http_operation, 'MANIFEST_CUSTODY')
        _require(set(txn['pre']) == set(m.touched_paths())
                 and all(v is None or (isinstance(v, str) and RE_SHA256.fullmatch(v))
                         for v in txn['pre'].values()), 'PRECONDITION_CUSTODY')
        for op in m.files:
            _require(txn['pre'][op.target] == (op.expect_current_sha256 if op.mode == 'replace' else None),
                     'PRECONDITION_CUSTODY')
            if op.backup_as:
                _require(txn['pre'][op.backup_as] is None, 'PRECONDITION_CUSTODY')
        for op in m.retire:
            _require(txn['pre'][op.path] == op.expect_current_sha256 and txn['pre'][op.rename_to] is None,
                     'PRECONDITION_CUSTODY')
        _require(set(txn['unchanged']) <= set(m.expect_unchanged)
                 and all(v is None or isinstance(v, str) for v in txn['unchanged'].values()), 'UNCHANGED_SCOPE')
        plan = engine.plan_steps(Prepared(m, {}, [], txn['pre']))
        _require(txn.get('steps') == plan, 'STEP_PLAN_CUSTODY')
        done, intent = txn.get('done'), txn.get('intent')
        _require(type(done) is int and type(intent) is int and 0 <= done <= len(plan)
                 and -1 <= intent < len(plan) and intent in (done - 1, done), 'STEP_COUNTERS')
        binding = history._canonical({'schema': 'hbd.startup_recovery_admission.v1', 'scope': vars(scope),
                                     'pointer': pointer, 'transaction_sha256': selected['pending_sha256'],
                                     'manifest_digest': m.digest, 'packages_root': packages_root})
        _require(admission(binding) is True, 'RECOVERY_ADMISSION_REFUSED')
        # Issuer/manifest callbacks can change state; reread after both.
        observed = working.resolve(expected_pointer=pointer, scope=scope, packages_root=packages_root,
                                   fence=fence, allow_pending=True, authorization=authorization)
        _require(observed == selected and read_working_snapshot(engine.j.dir)['txn.json'] == raw,
                 'TRANSACTION_DRIFT')
        working.check_startup(scope=scope, pointer=pointer, packages_root=packages_root,
                              fence=fence, authorization=authorization, action='recover',
                              pending_sha256=selected['pending_sha256'])
        fence.check()
        guard = engine.pk.mutation_guard
        journal, ha, packages, settings = engine.j, engine.ha, engine.pk, engine.s
        clock = working.clock
        pin = history._canonical({'scope': vars(scope), 'pointer': pointer})
        auth_pins = ((authorization.grant, authorization.verifier, authorization.clock)
                     if authorization is not None else None)
        def dependencies():
            _require(engine.j is guarded_journal and engine.ha is guarded_ha
                     and engine.pk is packages and engine.s is settings and working.clock is clock
                     and journal.dir == selected['journal_root'] and packages.root == packages_root
                     and packages.mutation_guard is guarded_mutation
                     and history._canonical({'scope': vars(scope), 'pointer': pointer}) == pin,
                     'RECOVERY_DEPENDENCY_CHANGED')
            if auth_pins is not None:
                _require(authorization.grant is auth_pins[0] and authorization.verifier is auth_pins[1]
                         and authorization.clock is auth_pins[2], 'RECOVERY_DEPENDENCY_CHANGED')
        def guarded_mutation():
            dependencies()
            working.check_startup(scope=scope, pointer=pointer, packages_root=packages_root,
                              fence=fence, authorization=authorization, action='recover',
                              pending_sha256=selected['pending_sha256'])
            fence.check()
            dependencies()
        guarded_journal = _GuardedCalls(journal, guarded_mutation)
        guarded_ha = _GuardedCalls(ha, guarded_mutation)
        engine.j, engine.ha = guarded_journal, guarded_ha
        engine.pk.mutation_guard = guarded_mutation
        try:
            result = engine.recover(lambda rid: m if rid == m.request_id else None)
            guarded_mutation()
        finally:
            packages.mutation_guard = guard
            engine.j, engine.ha = journal, ha
        _require(result is not None and result[0] == m.request_id, 'RECOVERY_RESULT')
        if result[1].outcome == FAILED_MANUAL or engine.j.load_txn() is not None:
            return {'state': 'HELD', 'outcome': result[1].outcome, 'journal_activated': False}
        final = working.resolve(expected_pointer=pointer, scope=scope, packages_root=packages_root, fence=fence, authorization=authorization)
        _require(final['state'] in ('INITIAL', 'PROGRESSED'), 'RECOVERY_INCOMPLETE')
        return {'state': 'READY_FIXTURE', 'outcome': result[1].outcome, 'journal_activated': False,
                'capture_trusted': False, 'native_verified': False, 'uninstall_allowed': False}
    except WorkingError:
        raise
    except (OSError, ValueError, TypeError, RuntimeError, KeyError, AttributeError):
        raise WorkingError('RECOVERY_HELD') from None
