"""Inactive startup grant contract; production issuer/factory NOT implemented.

Fresh grant authorizes only private read/recovery checks, never another import.
Exact issuer callback is an unimplemented trusted-host dependency, not a receipt.
"""
from dataclasses import dataclass, asdict
import hashlib
import os
import re

from .. import history
from ..writer import WriterLease, LOCK_NAME
from ..root_bindings import RootBindings
from .deployer_history_store import Scope


class StartupError(ValueError):
    pass


def _require(ok, reason):
    if not ok:
        raise StartupError(reason)


@dataclass(frozen=True)
class StartupGrant:
    startup_id: str
    issued_at: float
    expires_at: float
    migration_scope_sha256: str
    pointer_sha256: str
    working_root: str
    packages_root: str
    common_root: str
    lease_identity: tuple[int, int]
    action: str
    pending_sha256: str | None = None
    root_bindings_sha256: str | None = None


# Owner pop-up 2026-10-12 (design R1 option A): one authenticated owner tap may open a
# recovery-only session of at most two hours. Every check still re-verifies the session.
SESSION_MAX_SECONDS = 7200


@dataclass(frozen=True)
class OwnerSessionGrant(StartupGrant):
    """Recovery-only grant opened by one owner tap; never read-only forward, import or staging."""
    session_id: str = ''
    approved_binding_sha256: str = ''


def session_binding(grant):
    """Digest of exactly what the owner approved: every grant field except timing/identity."""
    fields = {key: value for key, value in asdict(grant).items()
              if key not in ('startup_id', 'issued_at', 'expires_at', 'session_id', 'approved_binding_sha256')}
    return digest({'schema': 'hbd.owner_recovery_session.v1', 'grant': fields})


def digest(value):
    return hashlib.sha256(history._canonical(value)).hexdigest()


class StartupAuthorization:
    def __init__(self, grant, *, verifier=None, clock=None, root_bindings=None):
        self.grant = grant
        self.verifier = verifier
        self.clock = clock
        self.root_bindings = root_bindings

    def check(self, *, scope, pointer, working_root, packages_root, fence, action='read', pending_sha256=None):
        try:
            grant, issuer, clock = self.grant, self.verifier, self.clock
            roots = self.root_bindings
            session = getattr(fence, 'session', None)
            _require(type(grant) in (StartupGrant, OwnerSessionGrant) and callable(issuer) and callable(clock)
                     and isinstance(scope, Scope) and isinstance(fence, WriterLease), 'STARTUP_DEPENDENCY')
            # Validate historical scope structure WITHOUT treating its expiry as
            # current authority. Only the independently fresh grant may do that.
            scope.validate(scope.expires_at - 1)
            pin = history._canonical(asdict(grant))
            def validate():
                now = clock()
                _require(fence.session == session, 'STARTUP_LEASE_SESSION')
                _require(history._finite_number(now) and history._finite_number(grant.issued_at)
                         and history._finite_number(grant.expires_at)
                         and grant.issued_at <= now < grant.expires_at
                         and 0 < grant.expires_at - grant.issued_at <= (
                             SESSION_MAX_SECONDS if type(grant) is OwnerSessionGrant else 30), 'STARTUP_FRESHNESS')
                if type(grant) is OwnerSessionGrant:
                    _require(grant.action == 'recover'
                             and type(grant.session_id) is str and re.fullmatch(r'[0-9a-f]{32}', grant.session_id)
                             and grant.approved_binding_sha256 == session_binding(grant), 'STARTUP_SESSION')
                _require(isinstance(grant.startup_id, str) and re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', grant.startup_id)
                         and grant.action in ('read', 'recover') and action in ('read', 'recover')
                         and (action == 'read' or grant.action == 'recover'), 'STARTUP_ACTION')
                _require(grant.migration_scope_sha256 == digest(vars(scope))
                         and grant.pointer_sha256 == digest(pointer)
                         and grant.working_root == working_root and grant.packages_root == packages_root
                         and grant.common_root == fence.root, 'STARTUP_SCOPE')
                _require(grant.pending_sha256 is None if grant.action == 'read' else
                         isinstance(grant.pending_sha256, str) and re.fullmatch(r'[0-9a-f]{64}', grant.pending_sha256),
                         'STARTUP_TRANSACTION')
                if pending_sha256 is not None:
                    _require(grant.action == 'recover' and grant.pending_sha256 == pending_sha256, 'STARTUP_TRANSACTION')
                if roots is None:
                    _require(grant.root_bindings_sha256 is None, 'STARTUP_ROOT_BINDINGS')
                else:
                    _require(type(roots) is RootBindings
                             and type(grant.root_bindings_sha256) is str
                             and re.fullmatch(r'[0-9a-f]{64}', grant.root_bindings_sha256)
                             and roots.path('working', fence=fence) == working_root
                             and roots.path('packages', fence=fence) == packages_root
                             and roots.path('writer', fence=fence) == fence.root
                             and hashlib.sha256(roots.evidence(fence=fence)).hexdigest() == grant.root_bindings_sha256,
                             'STARTUP_ROOT_BINDINGS')
                fence.check()
                st = os.stat(LOCK_NAME, dir_fd=fence._directory, follow_symlinks=False)
                _require(type(grant.lease_identity) is tuple and len(grant.lease_identity) == 2
                         and all(type(v) is int for v in grant.lease_identity)
                         and grant.lease_identity == (st.st_dev, st.st_ino), 'STARTUP_LEASE')
            validate()
            binding = history._canonical({'schema':'hbd.startup_authorization.v1', 'grant':asdict(grant)})
            _require(issuer(binding) is True, 'STARTUP_ISSUER_REFUSED')
            _require(self.verifier is issuer and self.clock is clock and self.grant is grant
                     and self.root_bindings is roots
                     and history._canonical(asdict(grant)) == pin, 'STARTUP_DEPENDENCY_CHANGED')
            validate()  # callback expiry/lease/config drift cannot widen admission
            return {'startup_checked':True, 'capture_trusted':False, 'journal_activated':False,
                    'native_verified':False, 'uninstall_allowed':False}
        except StartupError:
            raise
        except (OSError, ValueError, RuntimeError, TypeError, AttributeError):
            raise StartupError('STARTUP_REFUSED') from None
