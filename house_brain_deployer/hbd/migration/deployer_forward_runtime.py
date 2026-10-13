"""Explicit migrated Service binding; no installed issuer or default activation.

Every boundary asks the supplied provider for a new nonce-bound permit. A
startup read grant alone cannot grant forward effects. The provider's two
verifiers remain unimplemented production trust dependencies. No grant is
extended, no recovery is replayed, and no receipt here permits legacy removal.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import os
import secrets
import threading

from .. import history
from ..writer import WriterLease
from ..root_bindings import RootBindings
from .deployer_history_store import GenerationStore, Scope
from .deployer_startup_authorization import StartupAuthorization, StartupGrant
from .deployer_working_journal import WorkingJournal, WorkingError, _require


@dataclass(frozen=True)
class ForwardProvider:
    authorize: object
    fence: object = None
    root_bindings: object = None


@dataclass(frozen=True)
class ForwardPermit:
    nonce: str
    purpose: str
    authorization: object
    verifier: object


class _Calls:
    def __init__(self, target, runtime, *, socket=False):
        self._target, self._runtime, self._socket = target, runtime, socket

    def __getattr__(self, name):
        value = getattr(self._target, name)
        if not callable(value):
            return value
        if self._socket and name == 'close':
            return value  # Cleanup must survive denial/expiry.
        def call(*args, **kwargs):
            result = None
            try:
                with self._runtime.boundary():
                    result = value(*args, **kwargs)
            except BaseException:
                if name == 'ws' and result is not None:
                    result.close()
                raise
            return _Calls(result, self._runtime, socket=True) if name == 'ws' else result
        return call


class _Lease:
    """Service-compatible lease whose checks include fresh forward authority."""
    def __init__(self, runtime):
        self.runtime, self.inner = runtime, None

    def __enter__(self):
        _require(getattr(self.runtime._local, 'fence', None) is None, 'FORWARD_NESTED_LEASE')
        # Installed (bound) path never creates the lock at runtime (#985 enrollment plan R1).
        self.inner = WriterLease(self.runtime.plan['common_root'], create=self.runtime._bindings is None)
        self.inner.__enter__()
        self.runtime._local.fence = self.inner
        try:
            self.check()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def check(self):
        _require(self.inner is not None and getattr(self.runtime._local, 'fence', None) is self.inner,
                 'FORWARD_LEASE_THREAD')
        self.runtime.check(self.inner)

    def __exit__(self, *_args):
        self.runtime._local.fence = None
        if self.inner is not None:
            self.inner.close()
            self.inner = None


class ForwardRuntime:
    def __init__(self, data_dir, raw, plan, scope, provider):
        self.data_dir, self.raw = data_dir, raw
        self.plan = history._json(history._canonical(plan))
        self.scope, self.provider = scope, provider
        self._plan_pin = history._canonical(self.plan)
        self._scope_pin = history._canonical(vars(scope))
        self._authorize = provider.authorize
        self._initial_fence = provider.fence
        self._bindings = provider.root_bindings
        self._bound_io = False
        self._binding_pin = None
        self._local = threading.local()
        self._pid = os.getpid()
        self._roots = []
        self._identity = None
        self._clock = self._issuer = self._verifier = None
        self._use_name = self._use_raw = None
        self.journal_root = None
        self._service = self._service_pin = None
        self._options_pin = None
        self.closed = False
        self.held = False

    def close(self):
        self.closed = True
        for fd, _, _ in reversed(self._roots):
            os.close(fd)
        self._roots.clear()

    def _dependencies(self, fence):
        from .deployer_startup_service import _read_marker
        _require(os.getpid() == self._pid and not self.closed and not self.held and type(fence) is WriterLease
                 and fence.root == self.plan['common_root']
                 and self.provider.authorize is self._authorize
                 and self.provider.fence is self._initial_fence
                 and self.provider.root_bindings is self._bindings
                 and history._canonical(self.plan) == self._plan_pin
                 and history._canonical(vars(self.scope)) == self._scope_pin
                 and _read_marker(self.data_dir) == self.raw, 'FORWARD_DEPENDENCY_CHANGED')
        fence.check()
        if self._bindings is not None:
            _require(self._bindings.evidence(fence=fence) == self._binding_pin,
                     'FORWARD_BINDINGS_CHANGED')
        if self._service is not None:
            service = self._service
            current = (service.ha, service.gh, service.pk, service.j, service.writer_factory,
                       service.o, service.expected_owner_id, service.board)
            _require(all(a is b for i, (a, b) in enumerate(zip(current, self._service_pin, strict=True))
                         if i != 6) and current[6] == self._service_pin[6],
                     'FORWARD_SERVICE_CHANGED')
            _require(history._canonical(vars(service.o)) == self._options_pin,
                     'FORWARD_OPTIONS_CHANGED')
            _require(service.j.dir == self.journal_root
                     and service.pk.root == self.plan['packages_root']
                     and getattr(service.pk.mutation_guard, '__self__', None) is service
                     and getattr(service.pk.mutation_guard, '__name__', None) == '_check_writer',
                     'FORWARD_SERVICE_CHANGED')
            if service.engine is not None:
                _require(service.engine.j is service.j and service.engine.ha is service.ha
                         and service.engine.pk is service.pk and service.engine.gh is service.gh
                         and service.engine.s.owner_user_id == self.scope.owner_id
                         and service.engine.s.board is service.board
                         and service.engine.s.dry_run == service.o.dry_run
                         and service.engine.s.require_auth == service.o.require_phone_unlock
                         and service.engine.s.notify_service == service.o.notify_service
                         and service.engine.s.timing is service.timing,
                         'FORWARD_ENGINE_CHANGED')
        for fd, path, identity in self._roots:
            held, visible = os.fstat(fd), os.stat(path, follow_symlinks=False)
            _require(os.path.realpath(path) == path
                     and (held.st_dev, held.st_ino, held.st_mode, held.st_uid) == identity
                     == (visible.st_dev, visible.st_ino, visible.st_mode, visible.st_uid),
                     'FORWARD_ROOT_CHANGED')
        if self._use_name is not None:
            _require(GenerationStore._read(self._uses_fd, self._use_name, 4096) == self._use_raw,
                     'FORWARD_USE_CHANGED')

    def check(self, fence, *, purpose='effect'):
        """A new challenge per boundary; no cached authority across an approval wait."""
        try:
            return self._check(fence, purpose=purpose)
        except WorkingError:
            self.held = True
            raise
        except Exception:  # noqa: BLE001 - fail closed to a fixed refusal code
            self.held = True
            raise WorkingError('FORWARD_HELD') from None

    def _check(self, fence, *, purpose):
        # Effects require the descriptor clients; read selection never enables it.
        _require(self._bindings is None or purpose == 'startup' or self._bound_io,
                 'FORWARD_BOUND_MUTATION_UNAVAILABLE')
        self._dependencies(fence)
        state_pin = (self._identity, self._issuer, self._clock, self._verifier)
        request = {'schema': 'hbd.forward_authorization.v1', 'nonce': secrets.token_hex(32),
                   'purpose': purpose, 'plan': history._json(self._plan_pin)}
        permit = self._authorize(history._json(history._canonical(request)), fence)
        self._dependencies(fence)
        _require((self._identity, self._issuer, self._clock, self._verifier) == state_pin,
                 'FORWARD_ISSUER_CHANGED')
        _require(type(permit) is ForwardPermit and permit.nonce == request['nonce']
                 and type(permit.nonce) is str and type(permit.purpose) is str
                 and permit.purpose == purpose and callable(permit.verifier)
                 and type(permit.authorization) is StartupAuthorization,
                 'FORWARD_PERMIT_REFUSED')
        auth = permit.authorization
        _require(auth.root_bindings is self._bindings, 'FORWARD_BINDINGS_CHANGED')
        grant, issuer, clock, verifier = auth.grant, auth.verifier, auth.clock, permit.verifier
        grant_pin = history._canonical(asdict(grant)) if type(grant) is StartupGrant else None
        _require(type(grant) is StartupGrant and grant.action == 'read'
                 and grant.pending_sha256 is None, 'FORWARD_GRANT_REFUSED')
        identity = asdict(grant)
        identity.pop('issued_at'); identity.pop('expires_at')
        identity = history._canonical(identity)
        if self._identity is not None:
            _require(identity == self._identity and issuer is self._issuer
                     and clock is self._clock and verifier is self._verifier,
                     'FORWARD_ISSUER_CHANGED')
        auth.check(scope=self.scope, pointer=self.plan['pointer'],
                   working_root=self.plan['working_root'], packages_root=self.plan['packages_root'],
                   fence=fence)
        binding = history._canonical({'request': request, 'grant': asdict(grant)})
        _require(verifier(binding) is True, 'FORWARD_VERIFIER_REFUSED')
        _require(auth.grant is grant and auth.verifier is issuer and auth.clock is clock
                 and permit.verifier is verifier, 'FORWARD_PERMIT_CHANGED')
        auth.check(scope=self.scope, pointer=self.plan['pointer'],
                   working_root=self.plan['working_root'], packages_root=self.plan['packages_root'],
                   fence=fence)
        self._dependencies(fence)
        _require(history._canonical(asdict(grant)) == grant_pin
                 and permit.nonce == request['nonce'] and permit.purpose == purpose,
                 'FORWARD_PERMIT_CHANGED')
        if self._identity is None:
            self._identity, self._issuer, self._clock, self._verifier = identity, issuer, clock, verifier
        return auth

    def writer_factory(self):
        return _Lease(self)

    @contextmanager
    def boundary(self):
        fence = getattr(self._local, 'fence', None)
        if fence is None:
            with self.writer_factory() as lease:
                try:
                    yield
                finally:
                    lease.check()
        else:
            self.check(fence)
            try:
                yield
            finally:
                self.check(fence)

    def wrap(self, target):
        return _Calls(target, self)

    def bind_service(self, service):
        _require(self._service is None, 'FORWARD_SERVICE_ALREADY_BOUND')
        if self._bindings is not None:
            from ..journal import Journal
            from .deployer_bound_forward_io import BoundForwardState, BoundForwardPackages
            journal = getattr(service.j, '_target', None)
            files = getattr(journal, '_files', None)
            _require(type(journal) is Journal and type(files) is BoundForwardState
                     and journal._file_pin is files and files._runtime is self
                     and type(service.pk) is BoundForwardPackages and service.pk._runtime is self,
                     'FORWARD_DESCRIPTOR_CUSTODY')
        self._service = service
        self._service_pin = (service.ha, service.gh, service.pk, service.j, service.writer_factory,
                             service.o, service.expected_owner_id, service.board)
        self._options_pin = history._canonical(vars(service.o))

    @classmethod
    def open(cls, data_dir, raw, plan, scope, provider):
        return cls._open(data_dir, raw, plan, scope, provider, selection_only=False)

    @classmethod
    def inspect(cls, data_dir, raw, plan, scope, provider):
        """Authenticate a fresh forward challenge and read selected custody only.

        No Service, Journal constructor, startup spend or effect is exported.
        This is not installed provider trust or an activation grant.
        """
        return cls._open(data_dir, raw, plan, scope, provider, selection_only=True)

    @classmethod
    def _open(cls, data_dir, raw, plan, scope, provider, *, selection_only):
        """Resolve an already selected, nonpending Journal and durably spend once.

        Borrow the initial fence; later calls acquire/check the same common lock
        inode. No read grant, marker, or durable use alone activates this route.
        """
        _require(type(provider) is ForwardProvider and callable(provider.authorize)
                 and type(provider.fence) is WriterLease and type(scope) is Scope,
                 'FORWARD_PROVIDER_UNAVAILABLE')
        runtime = cls(data_dir, raw, plan, scope, provider)
        fence = provider.fence
        try:
            common = plan['common_root']
            _require(plan['schema'] != 'hbd.migrated_startup.v2' or runtime._bindings is not None,
                     'FORWARD_ROOTS')
            roots = (data_dir, plan['working_root'], plan['packages_root'], plan['uses_root'])
            if runtime._bindings is not None:
                bindings = runtime._bindings
                _require(type(bindings) is RootBindings
                         and all(bindings.path(role, fence=fence) == path for role, path in zip(
                             ('data', 'working', 'packages', 'uses', 'writer'), (*roots, common), strict=True)),
                         'FORWARD_ROOTS')
                runtime._binding_pin = bindings.evidence(fence=fence)
                for role in ('writer', 'data', 'working', 'packages', 'uses'):
                    path = bindings.path(role, fence=fence)
                    fd = bindings.open_directory(role, fence=fence)
                    st = os.fstat(fd)
                    runtime._roots.append((fd, path,
                                           (st.st_dev, st.st_ino, st.st_mode, st.st_uid)))
            else:
                _require(all(type(root) is str and os.path.isabs(root) and os.path.realpath(root) == root
                         and root != common and os.path.commonpath((root, common)) == common for root in roots)
                     and all(os.path.commonpath((a, b)) not in (a, b)
                             for i, a in enumerate(roots) for b in roots[i + 1:]), 'FORWARD_ROOTS')
                for root in (common, *roots):
                    fd = GenerationStore._directory(None, root)
                    st = os.fstat(fd)
                    runtime._roots.append((fd, root, (st.st_dev, st.st_ino, st.st_mode, st.st_uid)))
            runtime._uses_fd = runtime._roots[-1][0]
            auth = runtime.check(fence, purpose='startup')
            working = WorkingJournal(plan['working_root'], clock=auth.clock)
            selected = working.resolve(expected_pointer=plan['pointer'], scope=scope,
                                       packages_root=plan['packages_root'], fence=fence, authorization=auth)
            _require(selected['state'] in ('INITIAL', 'PROGRESSED'), 'FORWARD_PENDING_RECOVERY')
            if selection_only:
                runtime.check(fence, purpose='startup')
                return selected
            runtime._bound_io = runtime._bindings is not None
            runtime.journal_root = selected['journal_root']
            fd = (GenerationStore._directory(None, runtime.journal_root) if runtime._bindings is None
                  else runtime._bindings.open_directory('working',
                       (plan['pointer']['working'], 'journal'), fence=fence))
            st = os.fstat(fd)
            runtime._roots.append((fd, runtime.journal_root, (st.st_dev, st.st_ino, st.st_mode, st.st_uid)))
            name = 'forward-spent-' + hashlib.sha256(auth.grant.startup_id.encode()).hexdigest() + '.json'
            record = history._canonical({'schema': 'hbd.forward_use.v1',
                        'plan_sha256': hashlib.sha256(runtime._plan_pin).hexdigest(),
                        'identity_sha256': hashlib.sha256(runtime._identity).hexdigest(),
                        'state': 'SPENT_BEFORE_SERVICE', 'capture_trusted': False,
                        'native_verified': False, 'uninstall_allowed': False})
            runtime.check(fence, purpose='startup')
            class CheckedUse:
                def check(self):
                    runtime.check(fence, purpose='startup')
            GenerationStore._write(runtime._uses_fd, name, record, CheckedUse())
            os.fsync(runtime._uses_fd)
            _require(GenerationStore._read(runtime._uses_fd, name, 4096) == record, 'FORWARD_USE_READBACK')
            runtime._use_name, runtime._use_raw = name, record
            runtime.check(fence, purpose='startup')
            runtime._local.fence = fence
            return runtime
        except WorkingError:
            runtime.close()
            raise
        except Exception:  # noqa: BLE001 - fail closed to a fixed refusal code
            runtime.close()
            raise WorkingError('FORWARD_STARTUP_HELD') from None
        except BaseException:
            runtime.close()
            raise
        finally:
            if selection_only:
                runtime.close()

    def release_initial(self):
        # The caller owns this borrowed fence. Never close or unlink it here.
        self._local.fence = None
