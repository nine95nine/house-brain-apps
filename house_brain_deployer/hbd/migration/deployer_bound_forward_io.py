"""Descriptor clients scoped to the current fresh forward boundary, no issuer."""
from contextlib import contextmanager

from ..fsops import Packages
from ..writer import WriterLease
from .deployer_bound_files import BoundStateFiles
from .deployer_bound_packages import BoundPackageFiles
from .deployer_working_journal import _require


class _ForwardAccess:
    def __init__(self, runtime):
        _require(runtime._bindings is not None and runtime._bound_io, 'FORWARD_DESCRIPTOR_UNAVAILABLE')
        self._runtime, self._roots = runtime, runtime._bindings
        self._parts = (runtime.plan['pointer']['working'], 'journal')
        self._pins = (runtime, self._roots, self._parts)

    @contextmanager
    def _frame(self):
        runtime = self._runtime
        _require(self._pins == (runtime, self._roots, self._parts)
                 and runtime._bindings is self._roots and runtime._bound_io,
                 'FORWARD_DESCRIPTOR_CHANGED')
        with runtime.boundary():
            fence = getattr(runtime._local, 'fence', None)
            _require(type(fence) is WriterLease, 'FORWARD_DESCRIPTOR_LEASE')
            session = fence.session
            def guard():
                _require(runtime._bindings is self._roots and runtime._bound_io
                         and self._pins == (self._runtime, self._roots, self._parts)
                         and getattr(runtime._local, 'fence', None) is fence
                         and fence.session == session, 'FORWARD_DESCRIPTOR_CHANGED')
                runtime.check(fence)
            guard()
            yield fence, guard
            guard()

    def _check(self):
        with self._frame():
            pass


class BoundForwardState(_ForwardAccess):
    """Journal I/O protocol; each method borrows the actual current writer lease."""
    def _call(self, name, *args, **kwargs):
        with self._frame() as (fence, guard):
            with BoundStateFiles(self._roots, fence, self._parts[0], guard) as files:
                return getattr(files, name)(*args, **kwargs)

    def read(self, name):
        return self._call('read', name)

    def replace(self, name, raw, *, expected):
        return self._call('replace', name, raw, expected=expected)

    def append_audit(self, line, *, expected):
        return self._call('append_audit', line, expected=expected)

    def clear(self, name, *, expected):
        return self._call('clear', name, expected=expected)


class BoundForwardPackages(_ForwardAccess, Packages):
    """Compatible package surface, never an absolute-path effect fallback."""
    def __init__(self, runtime):
        _ForwardAccess.__init__(self, runtime)
        self.root = runtime.plan['packages_root']
        self.mutation_guard = None

    def _call(self, name, *args, **kwargs):
        with self._frame() as (fence, guard):
            with BoundPackageFiles(self._roots, fence, guard) as packages:
                if name in ('write_stage', 'link_noclobber', 'unlink_if', 'replace_with', 'check_writer'):
                    packages.mutation_guard = self.mutation_guard
                return getattr(packages, name)(*args, **kwargs)

    def check_writer(self):
        return self._call('check_writer')

    def check_root(self):
        return self._call('check_root')

    def local(self, owner_path):
        return self._call('local', owner_path)

    def stage_path(self, request_id, index):
        return self._call('stage_path', request_id, index)

    def snapshot(self, owner_paths):
        return self._call('snapshot', owner_paths)

    def file_sha(self, path):
        return self._call('file_sha', path)

    def read_bytes(self, path):
        return self._call('read_bytes', path)

    def space_named_files(self):
        return self._call('space_named_files')

    def write_stage(self, stage, data, sha):
        return self._call('write_stage', stage, data, sha)

    def link_noclobber(self, src, dst, *, expected_sha=None):
        return self._call('link_noclobber', src, dst, expected_sha=expected_sha)

    def unlink_if(self, path, expected_sha):
        return self._call('unlink_if', path, expected_sha)

    def replace_with(self, stage, target, *, expected_stage=None, expected_target=None):
        return self._call('replace_with', stage, target,
                          expected_stage=expected_stage, expected_target=expected_target)
