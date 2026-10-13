"""Compose existing private readers without manufacturing capture authority.

No App route/default component reader, writes, migration admission or attestation.
One retained caller fence and repeated state/byte observations cover this call.
"""
from dataclasses import dataclass, field
import hashlib
import json
import math
import os
import secrets
import stat
import time

from .. import history
from ..writer import WriterLease
from .deployer_history_store import Scope
from .deployer_maintenance_stage import validate_snapshot
from .deployer_supervisor_custody import SupervisorCustodyReader, SOURCE
from .deployer_source_recovery import read_source_recovery, identity

MAX_SECONDS = 30


class CaptureError(ValueError): pass


def require(ok, code):
    if not ok: raise CaptureError(code)


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class PreparedCapture:
    _history: tuple = field(repr=False)
    _recovery: tuple = field(repr=False)
    binding_sha256: str

    def private_history(self): return dict(self._history)
    def private_recovery(self): return dict(self._recovery)
    def receipt(self):
        return {'schema':'hbd.capture_composition.preparation.v1','binding_sha256':self.binding_sha256,
                'history_files':len(self._history),'recovery_files':len(self._recovery),
                'provenance':'UNATTESTED_INPUT','source_verified':False,'backup_verified':False,
                'writer_exclusion_verified':False,'native_qualified':False,
                'import_allowed':False,'uninstall_allowed':False}


def prepare_capture(*, scope, component_root, packages_root, component_sha256,
                    expected_files, destination, selected, now, fence=None,
                    supervisor=None, component_reader=None, clock=time.monotonic):
    """Two nonce-bound stopped observations surround repeated component/backups.

    Inputs are comparison selections, not authentic backup/candidate/owner proof.
    Filesystem elapsed checks do not interrupt blocked regular-file I/O. The
    separately bounded Supervisor reader still requires a dedicated Linux process.
    """
    fd=None
    try:
        require(type(scope) is Scope and type(fence) is WriterLease and
                type(supervisor) is SupervisorCustodyReader and callable(component_reader) and
                callable(clock) and supervisor.clock is clock, 'CAPTURE_DEPENDENCY')
        require(type(expected_files) is dict and type(destination) is dict and
                all(type(v) is bytes for v in destination.values()), 'CAPTURE_INPUT')
        expected_files,destination=dict(expected_files),dict(destination)
        start=clock()
        require(type(start) in (int,float) and math.isfinite(start),'CAPTURE_CLOCK')
        pinned_scope=dict(vars(scope));root=fence.root
        transport=supervisor.transport;token=supervisor._token
        state_reader=supervisor.read
        destination_version=supervisor.destination_version
        require(scope.source_instance==SOURCE and scope.destination_instance==supervisor.destination,
                'CAPTURE_SCOPE')
        require(type(component_root) is str and os.path.isabs(component_root) and
                os.path.realpath(component_root)==component_root and component_root!=root and
                os.path.commonpath((component_root,root))==root,'COMPONENT_ROOT')
        def check():
            current=clock()
            require(type(current) in (int,float) and math.isfinite(current) and
                    start<=current<start+MAX_SECONDS,'CAPTURE_EXPIRED')
            require(vars(scope)==pinned_scope and fence.root==root and supervisor.clock is clock and
                    supervisor.transport is transport and supervisor.read==state_reader and supervisor._token==token and
                    supervisor.destination==scope.destination_instance and
                    supervisor.destination_version==destination_version,'CAPTURE_DEPENDENCY_CHANGED')
            scope.validate(current);fence.check()
            if fd is not None:
                require(os.path.realpath(component_root)==component_root and
                        identity(os.fstat(fd))==component_identity==
                        identity(os.stat(component_root,follow_symlinks=False)),'COMPONENT_DRIFT')
        check()
        fd=os.open(component_root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
        info=os.fstat(fd)
        require(stat.S_ISDIR(info.st_mode) and info.st_uid==os.geteuid() and
                stat.S_IMODE(info.st_mode)==0o700,'COMPONENT_MODE')
        component_identity=identity(info)
        check()
        snapshot_sha=digest({'component_sha256':component_sha256,'source_slug':SOURCE,
                             'snapshot_version':'0.3.6','files':expected_files})
        def component():
            check()
            snapshot=component_reader(fd,component_sha256=component_sha256,expected_files=dict(expected_files))
            check()
            result=validate_snapshot(snapshot,expected_component_sha256=component_sha256,
                         expected_snapshot_sha256=snapshot_sha,expected_files=expected_files)
            check()
            return result.private_files()
        def state():
            check();nonce=secrets.token_hex(16);observed=state_reader(nonce);check()
            require(observed['nonce']==nonce and
                    all(observed[key] is False for key in ('source_verified','backup_verified',
                        'writer_exclusion_verified','native_qualified','import_allowed','uninstall_allowed')),
                    'OBSERVATION_AUTHORITY')
            return {'source':observed['source'],'destination':observed['destination']}
        before=state()
        files=component()
        first=read_source_recovery(files,destination,selected=selected,now=now,
                                   packages_root=packages_root,fence=fence,clock=clock)
        check()
        require(component()==files,'CAPTURE_HISTORY_DRIFT')
        second=read_source_recovery(files,destination,selected=selected,now=now,
                                    packages_root=packages_root,fence=fence,clock=clock)
        check()
        require(second.private_files()==first.private_files() and second.receipt()==first.receipt(),
                'CAPTURE_RECOVERY_DRIFT')
        del second
        after=state()
        require(before==after,'CAPTURE_STATE_DRIFT')
        # Final reread after remote callbacks; never return stale private bytes.
        require(component()==files,'CAPTURE_HISTORY_DRIFT')
        final=read_source_recovery(files,destination,selected=selected,now=now,
                                   packages_root=packages_root,fence=fence,clock=clock)
        require(final.private_files()==first.private_files() and final.receipt()==first.receipt(),
                'CAPTURE_RECOVERY_DRIFT')
        check()
        binding=digest({'scope':pinned_scope,'component_sha256':component_sha256,
                        'snapshot_sha256':snapshot_sha,'recovery':first.inventory_sha256,
                        'instances':after,'selected':selected,
                        'component_root':component_root,'packages_root':packages_root,
                        'fence_inode':identity(os.fstat(fence._file)),
                        'destination_history_sha256':history.fingerprint(destination)})
        return PreparedCapture(tuple(sorted(files.items())),tuple(sorted(first.private_files().items())),binding)
    except CaptureError: raise
    except Exception: raise CaptureError('CAPTURE_REFUSED') from None  # noqa: BLE001 - fail closed to a fixed refusal code
    finally:
        if fd is not None:
            try: os.close(fd)
            except OSError: pass
