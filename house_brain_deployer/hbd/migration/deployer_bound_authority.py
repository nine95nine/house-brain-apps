"""Fresh recovery guard for descriptor I/O, never an issuer or an installed grant."""
from .. import history
from ..root_bindings import RootBindings
from ..writer import WriterLease
from .deployer_startup_authorization import StartupAuthorization
from .deployer_working_journal import WorkingError, _require


class BoundRecoveryGuard:
    """Recheck the separately supplied authorization before each primitive effect.

    The existing startup-recovery grant bounds only this selected transaction;
    it cannot authorize forward work or Maintenance staging. No default verifier,
    renewal, loaded-source attestation or selected-backup proof is provided here.
    """

    def __init__(self, working, authorization, scope, pointer, packages_root, fence, pending_sha256):
        _require(type(authorization) is StartupAuthorization and type(fence) is WriterLease
                 and type(authorization.root_bindings) is RootBindings, 'BOUND_RECOVERY_AUTHORITY')
        self.working, self.authorization = working, authorization
        self.scope, self.pointer, self.packages_root = scope, pointer, packages_root
        self.fence, self.pending_sha256 = fence, pending_sha256
        self._pins = (working, authorization, authorization.grant, authorization.verifier,
                      authorization.clock, authorization.root_bindings, scope, pointer, fence)
        self._binding = history._canonical({'scope': vars(scope), 'pointer': pointer,
                                           'working_root': working.root, 'packages_root': packages_root,
                                           'pending_sha256': pending_sha256})
        self.held = False
        self()

    def __call__(self):
        try:
            _require(not self.held, 'BOUND_RECOVERY_HELD')
            def pin():
                working, auth, grant, verifier, clock, roots, scope, pointer, fence = self._pins
                _require(self.working is working and self.authorization is auth
                         and auth.grant is grant and auth.verifier is verifier
                         and auth.clock is clock and auth.root_bindings is roots
                         and self.scope is scope and self.pointer is pointer and self.fence is fence
                         and working.clock is clock
                         and history._canonical({'scope': vars(scope), 'pointer': pointer,
                                                 'working_root': working.root,
                                                 'packages_root': self.packages_root,
                                                 'pending_sha256': self.pending_sha256}) == self._binding,
                         'BOUND_RECOVERY_DEPENDENCY_CHANGED')
            pin()
            self.working.check_startup(scope=self.scope, pointer=self.pointer,
                                       packages_root=self.packages_root, fence=self.fence,
                                       authorization=self.authorization, action='recover',
                                       pending_sha256=self.pending_sha256)
            pin()
            self.fence.check()
        except Exception:  # noqa: BLE001 - any authority callback refusal holds this guard
            self.held = True
            raise WorkingError('BOUND_RECOVERY_HELD') from None
