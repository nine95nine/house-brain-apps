"""Owner-tap rooted recovery issuer (design R1 option A, owner pop-up 2026-10-12).

One authenticated owner tap (the existing ``approval.ask`` path: owner user id plus a
single-use nonce) opens ONE recovery session for ONE exact selected pending
transaction, for at most two hours. The session key lives in this process's memory
only: it is never written, logged or exported, and an App restart forgets it, so a new
tap is needed. Every check re-verifies the session (open, unexpired, exact binding),
so no step relies on an earlier one.

Recovery only. It never mints read-only forward, initial import (``prepare``) or
Maintenance staging authority. Not wired into App startup: the installed roots and
shared writer are not enrolled yet. Supplying this object alone grants nothing
without the owner's tap.
"""
from dataclasses import asdict
import hashlib
import hmac
import os
import re
import secrets
import time

from .. import history
from ..root_bindings import RootBindings
from ..writer import LOCK_NAME, WriterLease
from .deployer_history_store import Scope
from .deployer_startup_authorization import (SESSION_MAX_SECONDS, OwnerSessionGrant,
                                             StartupAuthorization, digest, session_binding)

STAGE = "MIGRATION_RECOVER"
APPROVE = "APPROVE"   # approval.APPROVE; kept literal so this module needs no HA import


class IssuerError(ValueError):
    """Fixed sanitized reason only."""


def _require(ok, reason):
    if not ok:
        raise IssuerError(reason)


class OwnerSessionIssuer:
    """Ask once, then verify every step against an in-memory session key.

    ``ask(stage=, title=, message=)`` must return an object whose ``outcome`` is the
    exact string ``"APPROVE"`` only for an authenticated owner tap; production wiring
    passes ``approval.ask`` bound to the configured owner, notify service and timeout.
    """

    def __init__(self, ask, *, clock=time.time, session_seconds=SESSION_MAX_SECONDS,
                 key_factory=secrets.token_bytes):
        _require(callable(ask) and callable(clock) and callable(key_factory), 'ISSUER_DEPENDENCY')
        _require(type(session_seconds) in (int, float) and 0 < session_seconds <= SESSION_MAX_SECONDS,
                 'ISSUER_SESSION_LENGTH')
        self._ask, self._clock, self._key_factory = ask, clock, key_factory
        self._seconds = session_seconds
        self._used = False
        self._closed = False
        self._key = None

    def authorize_recovery(self, *, scope, pointer, working_root, packages_root, fence,
                           pending_sha256, root_bindings=None):
        _require(not self._used and not self._closed, 'ISSUER_ONE_SESSION')
        self._used = True
        try:
            _require(type(scope) is Scope and type(fence) is WriterLease
                     and (root_bindings is None or type(root_bindings) is RootBindings), 'ISSUER_INPUT')
            _require(type(pending_sha256) is str and re.fullmatch(r'[0-9a-f]{64}', pending_sha256),
                     'ISSUER_TRANSACTION')
            fence.check()
            st = os.stat(LOCK_NAME, dir_fd=fence._directory, follow_symlinks=False)
            roots_sha = (None if root_bindings is None
                         else hashlib.sha256(root_bindings.evidence(fence=fence)).hexdigest())
            draft = OwnerSessionGrant(
                startup_id='owner-pending', issued_at=0.0, expires_at=1.0,
                migration_scope_sha256=digest(vars(scope)), pointer_sha256=digest(pointer),
                working_root=working_root, packages_root=packages_root, common_root=fence.root,
                lease_identity=(st.st_dev, st.st_ino), action='recover',
                pending_sha256=pending_sha256, root_bindings_sha256=roots_sha)
            approved = session_binding(draft)
            minutes = int(self._seconds // 60)
            decision = self._ask(
                stage=STAGE, title="Finish the interrupted install?",
                message=("The Deployer stopped in the middle of an install. Approve to let it finish "
                         f"recovering that one install within {minutes} minutes. Every step is checked "
                         "again; anything that changed stops it. Reject keeps everything on hold and "
                         f"changes nothing. Fingerprint {approved[:12]}."))
            _require(getattr(decision, 'outcome', None) == APPROVE, 'ISSUER_NOT_APPROVED')
            fence.check()
            now = self._clock()
            _require(history._finite_number(now), 'ISSUER_CLOCK')
            session_id = secrets.token_hex(16)
            grant = OwnerSessionGrant(**{**asdict(draft), 'startup_id': 'owner-' + session_id[:16],
                                         'issued_at': now, 'expires_at': now + self._seconds,
                                         'session_id': session_id, 'approved_binding_sha256': approved})
            key = self._key_factory(32)
            _require(type(key) is bytes and len(key) == 32, 'ISSUER_KEY')
            self._key = key
            binding = history._canonical({'schema': 'hbd.startup_authorization.v1', 'grant': asdict(grant)})
            tag = hmac.digest(key, binding, 'sha256')
            clock, expires = self._clock, grant.expires_at

            def verify(value):
                current = self._key
                if self._closed or current is None or type(value) is not bytes:
                    return False
                now = clock()
                if not (history._finite_number(now) and now < expires):
                    return False
                return hmac.compare_digest(hmac.digest(current, value, 'sha256'), tag)

            return StartupAuthorization(grant, verifier=verify, clock=self._clock, root_bindings=root_bindings)
        except IssuerError:
            self.close()
            raise
        except Exception:  # noqa: BLE001 - fail closed to a fixed refusal code
            self.close()
            raise IssuerError('ISSUER_REFUSED') from None

    def close(self):
        """End the session: every later check refuses. Idempotent."""
        self._closed = True
        self._key = None


MAX_TXN_BYTES = 1024 * 1024


def installed_root_specs(plan, data_dir):
    """RootSpecs for a v2 selection, with each mode taken exactly as observed (never chmod-ed).

    RootBindings then enforces the policy: data 0700 or 0755 (layout R2), packages 0700/0750/0755,
    every other role 0700, the App's own uid/gid, no overlap and no symlinks. A wrong mode refuses.
    """
    import stat
    from ..root_bindings import RootSpec
    _require(type(plan) is dict and plan.get('schema') == 'hbd.migrated_startup.v2'
             and type(data_dir) is str, 'ISSUER_PLAN')
    paths = (('data', data_dir), ('working', plan.get('working_root')), ('uses', plan.get('uses_root')),
             ('packages', plan.get('packages_root')), ('writer', plan.get('common_root')))
    specs = []
    for role, path in paths:
        _require(type(path) is str and os.path.isabs(path), 'ISSUER_PLAN')
        st = os.stat(path, follow_symlinks=False)
        _require(stat.S_ISDIR(st.st_mode), 'ISSUER_ROOT_TYPE')
        specs.append(RootSpec(role, path, os.geteuid(), os.getegid(), stat.S_IMODE(st.st_mode)))
    return tuple(specs)


def owner_recovery_authorize(issuer, root_bindings):
    """``RecoveryProvider.authorize`` for ``startup_hold``: one owner tap per selected pending install.

    Reads the pending transaction's bytes through the bound working root (descriptor-relative, no
    symlinks, bounded) only to fingerprint it; recovery itself re-reads and re-checks everything.
    """
    _require(type(issuer) is OwnerSessionIssuer and type(root_bindings) is RootBindings, 'ISSUER_INPUT')

    def authorize(plan, fence):
        _require(type(plan) is dict and plan.get('schema') == 'hbd.migrated_startup.v2'
                 and type(plan.get('scope')) is dict and type(plan.get('pointer')) is dict, 'ISSUER_PLAN')
        scope = Scope(**plan['scope'])
        pointer = plan['pointer']
        directory = root_bindings.open_directory('working', (pointer['working'], 'journal'), fence=fence)
        try:
            fd = os.open('txn.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
            try:
                raw = os.read(fd, MAX_TXN_BYTES + 1)
            finally:
                os.close(fd)
        except FileNotFoundError:
            raise IssuerError('ISSUER_NO_PENDING') from None
        finally:
            os.close(directory)
        _require(0 < len(raw) <= MAX_TXN_BYTES, 'ISSUER_TRANSACTION')
        return issuer.authorize_recovery(scope=scope, pointer=pointer, working_root=plan['working_root'],
                                         packages_root=plan['packages_root'], fence=fence,
                                         pending_sha256=hashlib.sha256(raw).hexdigest(),
                                         root_bindings=root_bindings)
    return authorize


class installed_owner_provider:
    """Context manager for ``build_service``: the owner-tap ``RecoveryProvider`` or ``None``.

    No selection marker: ``None`` and nothing else happens (ordinary startup unchanged).
    A v2 marker: open the installed roots with modes exactly as observed, take the enrolled writer
    lock WITHOUT creating it, and build the provider. Anything missing or wrong yields ``None`` so
    ``startup_hold`` keeps today's hold; no phone ask is ever sent from a failed setup.
    ``manifest_for`` defaults to "unavailable", so migrated recovery takes the existing conservative
    path (old files restored) and never pushes an interrupted install forward.
    """

    def __init__(self, data_dir, ask, *, manifest_for=None, clock=time.time, sleep=time.sleep):
        self.data_dir, self.ask, self.clock, self.sleep = data_dir, ask, clock, sleep
        self.manifest_for = manifest_for if callable(manifest_for) else (lambda _request_id: None)
        self._roots = self._fence = self.issuer = None
        self.forward = None
        self.reason = None

    def __enter__(self):
        from .deployer_startup_service import RecoveryProvider, _plan, _read_marker
        try:
            raw = _read_marker(self.data_dir)
            if raw is None:
                self.reason = 'NO_SELECTION'
                return None
            plan, _scope = _plan(raw)
            _require(plan['schema'] == 'hbd.migrated_startup.v2', 'ISSUER_PLAN')
            self._roots = RootBindings(installed_root_specs(plan, self.data_dir))
            self._roots.__enter__()
            self._fence = self._take_lock(plan['common_root'])
            if not self._pending(plan):
                # 0.3.23 (owner pop-ups "Build both", "No tap after Select"): no interrupted install, so the
                # selected history runs normally; the Select-approved selection file is the authority.
                from ..legacy_migrate import ForwardIssuer
                from .deployer_forward_runtime import ForwardProvider
                self.forward = ForwardIssuer(self.data_dir, raw, plan, _scope, self._roots, clock=self.clock)
                return ForwardProvider(self.forward.authorize, self._fence, self._roots)
            self.issuer = OwnerSessionIssuer(self.ask, clock=self.clock)
            return RecoveryProvider(owner_recovery_authorize(self.issuer, self._roots),
                                    self.manifest_for, self._fence)
        except Exception as error:  # noqa: BLE001 - fail closed: startup_hold keeps the hold
            self.reason = type(error).__name__ + ':' + str(error)[:60]
            self.__exit__(None, None, None)
            return None

    # 0.3.23 (review finding 4): both Apps start together after a reboot; Maintenance may briefly hold the lock.
    LOCK_TRIES, LOCK_WAIT_SECONDS = 6, 5.0

    def _take_lock(self, root):
        from ..writer import WriterError
        for attempt in range(self.LOCK_TRIES):
            fence = WriterLease(root, create=False)
            try:
                fence.__enter__()
                return fence
            except WriterError as error:
                fence.close()
                if str(error) != 'WRITER_BUSY' or attempt == self.LOCK_TRIES - 1:
                    raise
                self.sleep(self.LOCK_WAIT_SECONDS)
        raise IssuerError('ISSUER_LOCK')

    def _pending(self, plan):
        """True when the selected working journal holds an interrupted install (txn.json present)."""
        directory = self._roots.open_directory('working', (plan['pointer']['working'], 'journal'), fence=self._fence)
        try:
            os.stat('txn.json', dir_fd=directory, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False
        finally:
            os.close(directory)

    def keep_for_runtime(self):
        """A forward runtime keeps the bound roots for its whole life; the initial lock is still released."""
        roots, self._roots = self._roots, None
        return roots

    def __exit__(self, *_exc):
        for item in (self.issuer, self._fence, self._roots):
            if item is None:
                continue
            try:
                item.close()
            except Exception:  # noqa: BLE001,S110 - cleanup must not mask the startup result
                pass
        self._roots = self._fence = self.issuer = None
        return False
