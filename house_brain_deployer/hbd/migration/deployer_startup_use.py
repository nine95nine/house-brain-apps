"""Durable consume-once recovery admission; no production issuer or App route.

Repeated StartupAuthorization checks remain necessary during recovery. This
component spends a fresh startup ID once before the existing recovery action;
checking an already active authorization does not mint another recovery use.
Interrupted/partial markers are retained, never renewed or cleaned here.
"""
from dataclasses import asdict
import hashlib
import os
import re

from .. import history
from ..writer import WriterLease
from .deployer_history_store import GenerationStore, Scope, MAX_META
from .deployer_startup_authorization import OwnerSessionGrant, StartupAuthorization, StartupGrant


class StartupUseError(ValueError): pass


def require(ok, code):
    if not ok: raise StartupUseError(code)


def sha(raw): return hashlib.sha256(raw).hexdigest()


class StartupRecoveryUse:
    """Explicit fresh-grant consumer for recover_pending's admission callback.

    Historical migration expiry is not renewed. A new startup ID after failure
    requires an independently trusted issuer; this class cannot supply one.
    """
    def __init__(self, root, *, scope, pointer, working_root, packages_root,
                 fence=None, authorization=None):
        self.root, self.scope, self.pointer = root, scope, pointer
        self.working_root, self.packages_root = working_root, packages_root
        self.fence, self.authorization = fence, authorization

    def consume(self, binding):
        fd = None
        try:
            root, scope, pointer = self.root, self.scope, self.pointer
            working, packages = self.working_root, self.packages_root
            fence, auth = self.fence, self.authorization
            require(type(scope) is Scope and type(fence) is WriterLease and
                    type(auth) is StartupAuthorization and type(auth.grant) in (StartupGrant, OwnerSessionGrant),
                    'STARTUP_USE_DEPENDENCY')
            grant, issuer, clock = auth.grant, auth.verifier, auth.clock
            roots = auth.root_bindings
            require(grant.action == 'recover' and callable(issuer) and callable(clock),
                    'STARTUP_USE_DEPENDENCY')
            pinned = history._canonical({'scope':vars(scope), 'pointer':pointer, 'grant':asdict(grant)})
            common = fence.root
            if roots is None:
                require(type(root) is str and os.path.isabs(root) and os.path.realpath(root) == root and
                        root != common and os.path.commonpath((root,common)) == common,
                        'STARTUP_USE_ROOT')
            else:
                from ..root_bindings import RootBindings
                require(type(roots) is RootBindings and roots.path('uses') == root,
                        'STARTUP_USE_ROOT')
            require(type(binding) is bytes and 0 < len(binding) <= MAX_META, 'STARTUP_USE_BINDING')
            body = history._json(binding)
            require(type(body) is dict and set(body) == {'schema','scope','pointer','transaction_sha256',
                    'manifest_digest','packages_root'} and history._canonical(body) == binding and
                    body['schema'] == 'hbd.startup_recovery_admission.v1' and body['scope'] == vars(scope) and
                    body['pointer'] == pointer and body['packages_root'] == packages and
                    body['transaction_sha256'] == grant.pending_sha256 and
                    type(body['manifest_digest']) is str and re.fullmatch(r'[0-9a-f]{64}',body['manifest_digest']),
                    'STARTUP_USE_BINDING')
            def pin_check():
                require(self.root == root and self.scope is scope and self.pointer is pointer and
                        self.working_root == working and self.packages_root == packages and
                        self.fence is fence and self.authorization is auth and auth.grant is grant and
                        auth.root_bindings is roots and
                        auth.verifier is issuer and auth.clock is clock and fence.root == common and
                        history._canonical({'scope':vars(scope),'pointer':pointer,'grant':asdict(grant)}) == pinned,
                        'STARTUP_USE_DEPENDENCY_CHANGED')
            def check(authorize=True):
                pin_check()
                if authorize:
                    auth.check(scope=scope,pointer=pointer,working_root=working,packages_root=packages,
                               fence=fence,action='recover',pending_sha256=body['transaction_sha256'])
                if roots is not None:
                    require(roots.path('uses') == root and sha(roots.evidence()) == grant.root_bindings_sha256,
                            'STARTUP_USE_ROOT_DRIFT')
                pin_check()
                now = clock()
                require(history._finite_number(now) and grant.issued_at <= now < grant.expires_at,
                        'STARTUP_USE_EXPIRED')
                fence.check()
                if fd is not None:
                    held, visible = os.fstat(fd), os.stat(root,follow_symlinks=False)
                    require(os.path.realpath(root) == root and
                            (held.st_dev,held.st_ino,held.st_mode,held.st_uid) == root_identity ==
                            (visible.st_dev,visible.st_ino,visible.st_mode,visible.st_uid), 'STARTUP_USE_ROOT_DRIFT')
                pin_check() # Clock/fence callbacks cannot change the pinned consumer.
                if roots is not None:
                    require(roots.path('uses') == root and sha(roots.evidence()) == grant.root_bindings_sha256,
                            'STARTUP_USE_ROOT_DRIFT')
            check()
            fd = (GenerationStore._directory(None,root) if roots is None
                  else roots.open_directory('uses'))
            info = os.fstat(fd); root_identity = (info.st_dev,info.st_ino,info.st_mode,info.st_uid)
            check()
            name = 'startup-spent-' + sha(grant.startup_id.encode()) + '.json'
            try: os.stat(name,dir_fd=fd,follow_symlinks=False)
            except FileNotFoundError: pass
            else: raise StartupUseError('STARTUP_USE_ALREADY_SPENT')
            record = history._canonical({'schema':'hbd.startup_use.preparation.v1',
                'startup_sha256':sha(grant.startup_id.encode()), 'grant_sha256':sha(pinned),
                'binding_sha256':sha(binding), 'state':'SPENT_BEFORE_RECOVERY',
                'source_verified':False, 'native_qualified':False, 'import_verified':False})
            GenerationStore._write(fd,name,record,fence)
            check(); os.fsync(fd)
            require(GenerationStore._read(fd,name,4096) == record, 'STARTUP_USE_READBACK')
            check() # Recheck explicit issuer/revocation after durable spend.
            require(GenerationStore._read(fd,name,4096) == record, 'STARTUP_USE_READBACK')
            check(False)
            return True # Fixture recovery boundary only, no public activation grant.
        except StartupUseError: raise
        except FileExistsError: raise StartupUseError('STARTUP_USE_ALREADY_SPENT') from None
        except Exception: raise StartupUseError('STARTUP_USE_REFUSED') from None  # noqa: BLE001 - fail closed to a fixed refusal code
        finally:
            if fd is not None:
                try: os.close(fd)
                except OSError: pass
