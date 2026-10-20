"""Bounded setup-only private delivery over Supervisor-managed standard input.

No HA client, SQLite, service call, listener, arbitrary path or live reload.
The release sender is responsible for approval and exact installed-image custody.
Hashes bind reviewed bytes; they do not grant authority.
"""
import os
from pathlib import Path
import re
import select
import sys
import time
from .installation import LIMIT, activate, canonical, decode, digest, read_private, validate

FRAME_LIMIT = 2 * LIMIT + 16384
BASE_FIELDS = frozenset({'schema_version','request_id','operation','manifest_sha256','issued_at','expires'})
WRITE_FIELDS = frozenset({'bundle_text','bundle_sha256','expected_current_sha256','source_commit'})
HEX = re.compile('[0-9a-f]{64}')


def public_manifest(app):
    """Verify packaged runtime bytes before reporting or accepting its identity."""
    app = Path(app)
    raw = read_private(app/'SOURCE_MANIFEST.sha256')
    seen = set()
    for line in raw.decode().splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  ([a-zA-Z0-9_./-]+)',line)
        if not match:
            raise ValueError('SETUP_MANIFEST_INVALID')
        expected,relative = match.groups()
        if relative.startswith('/') or any(p in ('','.', '..') for p in relative.split('/')) or relative in seen:
            raise ValueError('SETUP_MANIFEST_PATH')
        seen.add(relative)
        path = app/relative
        if any(p.is_symlink() for p in path.parents) or digest(read_private(path)) != expected:
            raise ValueError('SETUP_RUNTIME_BYTES_MISMATCH')
    # The receiver, entrypoint and installation guard must belong to this package.
    required = {'energy_ai/provisioning.py','energy_ai/__main__.py','energy_ai/installation.py'}
    if not required <= seen:
        raise ValueError('SETUP_MANIFEST_INCOMPLETE')
    return digest(raw)


def read_frame(descriptor, timeout=60):
    if not 0 < timeout <= 60:
        raise ValueError('SETUP_TIMEOUT_LIMIT')
    poll = select.poll()
    poll.register(descriptor,select.POLLIN|select.POLLHUP|select.POLLERR)
    deadline = time.monotonic()+timeout
    raw = bytearray()
    while True:
        remaining = deadline-time.monotonic()
        if remaining <= 0 or not poll.poll(max(1,int(remaining*1000))):
            raise ValueError('SETUP_INPUT_TIMEOUT')
        chunk = os.read(descriptor,min(8192,FRAME_LIMIT+1-len(raw)))
        if not chunk:
            raise ValueError('SETUP_INPUT_INCOMPLETE')
        raw.extend(chunk)
        if len(raw)>FRAME_LIMIT:
            raise ValueError('SETUP_FRAME_LIMIT')
        end = raw.find(b'\n')
        if end>=0:
            if raw[end+1:].strip():
                raise ValueError('SETUP_MULTIPLE_FRAMES')
            return bytes(raw[:end])


def process(raw, data, app, now=None):
    now = time.time() if now is None else now
    if len(raw)>FRAME_LIMIT:
        raise ValueError('SETUP_FRAME_LIMIT')
    packet = decode(raw)
    if not isinstance(packet,dict) or not BASE_FIELDS <= set(packet):
        raise ValueError('SETUP_FIELDS')
    operation = packet['operation']
    if operation not in ('READBACK','ACTIVATE') or set(packet) != BASE_FIELDS | (WRITE_FIELDS if operation=='ACTIVATE' else set()):
        raise ValueError('SETUP_OPERATION_FIELDS')
    if type(packet['schema_version']) is not int or packet['schema_version'] != 1:
        raise ValueError('SETUP_SCHEMA')
    if not isinstance(packet['request_id'],str) or not re.fullmatch('[0-9a-f]{32}',packet['request_id']):
        raise ValueError('SETUP_REQUEST_ID')
    issued,expires = packet['issued_at'],packet['expires']
    if (type(issued) not in (int,float) or type(expires) not in (int,float)
            or not issued<=now<expires or not 0<expires-issued<=300):
        raise ValueError('SETUP_EXPIRED_OR_FUTURE')
    manifest = public_manifest(app)
    if packet['manifest_sha256'] != manifest:
        raise ValueError('SETUP_ARTIFACT_MISMATCH')
    data = Path(data)
    if not data.is_dir() or data.is_symlink():
        raise ValueError('SETUP_DATA_DIRECTORY')
    lock = data/'.energy-bundle-install.lock'
    if lock.exists() or lock.is_symlink():
        raise ValueError('SETUP_INSTALL_LOCK_REQUIRES_INSPECTION')
    destination = data/'household.json'
    current = validate(read_private(destination)) if destination.exists() or destination.is_symlink() else None
    current_digest = current['bundle_sha256'] if current else None
    receipt = {'schema_version':1,'request_id':packet['request_id'],'operation':operation,
        'manifest_sha256':manifest,'active_sha256':current_digest,
        'source_commit':current['source_commit'] if current else None,
        'mode':'SETUP_ONLY','physical_actions':False,'runtime_started':False,'at':now}
    if operation == 'READBACK':
        return {**receipt,'status':'ACTIVE' if current else 'SETUP_REQUIRED'}
    if (not isinstance(packet['bundle_text'],str) or not isinstance(packet['bundle_sha256'],str)
            or not HEX.fullmatch(packet['bundle_sha256'])
            or packet['expected_current_sha256'] is not None and
               (not isinstance(packet['expected_current_sha256'],str) or not HEX.fullmatch(packet['expected_current_sha256']))):
        raise ValueError('SETUP_BUNDLE_FIELDS')
    content = packet['bundle_text'].encode()
    proposed = validate(content)
    if digest(content) != packet['bundle_sha256'] or proposed['source_commit'] != packet['source_commit']:
        raise ValueError('SETUP_BUNDLE_IDENTITY')
    if current_digest == packet['bundle_sha256']:
        # Retry after uncertain completion is readback only. Never replace the
        # retained previous bundle or touch the database for an already-active set.
        return {**receipt,'status':'ALREADY_ACTIVE'}
    activate(content,destination,packet['bundle_sha256'],packet['expected_current_sha256'])
    installed = validate(read_private(destination))
    if installed['bundle_sha256'] != packet['bundle_sha256']:
        raise ValueError('SETUP_ACTIVATION_READBACK_MISMATCH')
    return {**receipt,'status':'ACTIVATED','active_sha256':installed['bundle_sha256'],
            'source_commit':installed['source_commit']}


def serve(data, app):
    """One Supervisor stdin frame, bounded wait, redacted receipt, then exit."""
    try:
        result = process(read_frame(sys.stdin.fileno()),data,app)
        code = 0
    except (OSError,ValueError,KeyError,TypeError,OverflowError,RecursionError):
        result = {'schema_version':1,'status':'REFUSED','reason':'PRIVATE_SETUP_INVALID_OR_UNAVAILABLE',
                  'mode':'SETUP_ONLY','physical_actions':False,'runtime_started':False}
        code = 1
    print(canonical(result).decode(),flush=True)
    return code
