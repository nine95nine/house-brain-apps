"""One-use fixture admission custody; authentic issuer is absent by default.

Spent/partial operation markers are retained before any admitted action. This
ledger never issues grants, renews consent, retries imports or authorizes startup.
"""
import hashlib
import os
import re

from .. import history
from ..writer import WriterLease
from .deployer_history_store import GenerationStore, Scope, MAX_META


class AdmissionError(ValueError): pass


def require(ok,code):
    if not ok: raise AdmissionError(code)


def sha(raw): return hashlib.sha256(raw).hexdigest()


class MigrationAdmission:
    """Consume one exact generation binding using an explicit trusted boundary.

    The verifier interface is the existing canonical-bytes/True-or-refuse contract.
    No production verifier is supplied. Installing this class alone grants nothing.
    Successful fixture consumption proves local use custody, not source authority.
    """
    def __init__(self,root,scope,binding_sha256,*,fence=None,verifier=None,clock=None):
        self.root,self.scope,self.binding_sha256=root,scope,binding_sha256
        self.fence,self.verifier,self.clock=fence,verifier,clock

    def consume(self,binding):
        fd=None
        try:
            root,scope,expected=self.root,self.scope,self.binding_sha256
            fence,verifier,clock=self.fence,self.verifier,self.clock
            require(type(scope) is Scope and type(fence) is WriterLease and
                    callable(verifier) and callable(clock),'ADMISSION_DEPENDENCY')
            require(type(expected) is str and re.fullmatch(r'[0-9a-f]{64}',expected) and
                    type(binding) is bytes and 0<len(binding)<=MAX_META and sha(binding)==expected,
                    'ADMISSION_BINDING')
            pinned=history._canonical(vars(scope));common=fence.root
            require(type(root) is str and os.path.isabs(root) and os.path.realpath(root)==root and
                    root!=common and os.path.commonpath((root,common))==common,'ADMISSION_ROOT')
            body=history._json(binding)
            require(type(body) is dict and body.get('schema')=='hbd.generation_preparation.v1' and
                    body.get('scope')==vars(scope) and history._canonical(body)==binding,'ADMISSION_SCOPE')
            def check():
                require((self.root,self.scope,self.binding_sha256)==(root,scope,expected) and
                        self.fence is fence and self.verifier is verifier and self.clock is clock and
                        history._canonical(vars(scope))==pinned and fence.root==common,
                        'ADMISSION_DEPENDENCY_CHANGED')
                scope.validate(clock());fence.check()
                if fd is not None:
                    held=os.fstat(fd);visible=os.stat(root,follow_symlinks=False)
                    require(os.path.realpath(root)==root and
                            (held.st_dev,held.st_ino,held.st_mode,held.st_uid)==root_identity==
                            (visible.st_dev,visible.st_ino,visible.st_mode,visible.st_uid),'ADMISSION_ROOT_DRIFT')
            check()
            fd=GenerationStore._directory(None,root)
            info=os.fstat(fd);root_identity=(info.st_dev,info.st_ino,info.st_mode,info.st_uid)
            check()
            name='spent-'+sha(scope.operation_id.encode())+'.json'
            # Any name presence, including empty/partial/foreign markers, blocks.
            try: os.stat(name,dir_fd=fd,follow_symlinks=False)
            except FileNotFoundError: pass
            else: raise AdmissionError('ADMISSION_ALREADY_SPENT')
            require(verifier(binding) is True,'ADMISSION_ISSUER_REFUSED')
            check()
            record=history._canonical({'schema':'hbd.migration_use.preparation.v1',
                'operation_sha256':sha(scope.operation_id.encode()),'scope_sha256':sha(pinned),
                'binding_sha256':expected,'state':'SPENT_BEFORE_ACTION',
                'source_verified':False,'native_qualified':False,'import_verified':False})
            # Existing exclusive private writer fsyncs its file; no overwrite or
            # marker cleanup on failure. A partial marker permanently holds replay.
            GenerationStore._write(fd,name,record,fence)
            check();os.fsync(fd)
            require(GenerationStore._read(fd,name,4096)==record,'ADMISSION_READBACK')
            require(verifier(binding) is True,'ADMISSION_ISSUER_REFUSED')
            require(GenerationStore._read(fd,name,4096)==record,'ADMISSION_READBACK')
            check()
            return True  # Only the explicit consumer boundary, never a public grant.
        except AdmissionError: raise
        except FileExistsError: raise AdmissionError('ADMISSION_ALREADY_SPENT') from None
        except Exception: raise AdmissionError('ADMISSION_REFUSED') from None  # noqa: BLE001 - fail closed to a fixed refusal code
        finally:
            if fd is not None:
                try: os.close(fd)
                except OSError: pass
