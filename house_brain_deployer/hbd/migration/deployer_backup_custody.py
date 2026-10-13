"""Selected-backup custody for the legacy local Deployer (owner pop-up 2026-10-12, #985).

One Supervisor partial backup that holds ONLY the stopped legacy App
``local_house_brain_deployer`` is taken under a unique name, then its Supervisor
metadata (slug, name, date, size, exact contents) is pinned in a custody record.
``verify`` re-reads the backup list and refuses unless the same backup is still
there with identical metadata. Nothing here deletes, restores or downloads a
backup.

Limit, stated plainly: this App has no backup download or /backup mount, so the
pin is Supervisor metadata, not a hash of the archive bytes. Restore authority
is a separate owner-approved step.
"""
import hashlib
import json
import re

from .deployer_supervisor_custody import SOURCE

SCHEMA = 'hbd.legacy_backup_custody.v1'
RE_NONCE = re.compile(r'[0-9a-f]{16,32}')
RE_SLUG = re.compile(r'[0-9a-f]{8,64}')
FIELDS = ('slug', 'name', 'date', 'size', 'content')


class BackupCustodyError(ValueError):
    """Fixed sanitized reason only."""


def _require(ok, reason):
    if not ok:
        raise BackupCustodyError(reason)


def _digest(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _matching(ha, name):
    items = ha.backups()
    _require(type(items) is list, 'BACKUP_LIST')
    return [b for b in items if type(b) is dict and b.get('name') == name]


def _metadata(entry):
    content = entry.get('content')
    _require(type(content) is dict
             and content.get('homeassistant') is False
             and content.get('addons') == [SOURCE]
             and content.get('folders') in ([], None), 'BACKUP_CONTENT')
    _require(type(entry.get('slug')) is str and RE_SLUG.fullmatch(entry['slug'])
             and type(entry.get('date')) is str and 0 < len(entry['date']) <= 64
             and type(entry.get('size')) in (int, float) and entry['size'] > 0, 'BACKUP_SHAPE')
    return {'slug': entry['slug'], 'name': entry['name'], 'date': entry['date'], 'size': entry['size'],
            'content': {'homeassistant': False, 'addons': [SOURCE], 'folders': []}}


def take(ha, *, nonce):
    """Take one legacy-only partial backup and return its custody record. Never retries."""
    _require(type(nonce) is str and RE_NONCE.fullmatch(nonce), 'BACKUP_NONCE')
    name = 'hbd-legacy-0.3.6-' + nonce
    _require(_matching(ha, name) == [], 'BACKUP_NAME_IN_USE')
    try:
        ha.backup_legacy_app(name)
    except Exception:  # noqa: BLE001 - fail closed to a fixed refusal code
        raise BackupCustodyError('BACKUP_FAILED') from None
    found = _matching(ha, name)
    _require(len(found) == 1, 'BACKUP_NOT_UNIQUE')
    body = {'schema': SCHEMA, 'source': SOURCE, 'source_version': '0.3.6', **_metadata(found[0])}
    return {**body, 'custody_sha256': _digest(body), 'content_hashed': False,
            'restore_allowed': False, 'uninstall_allowed': False}


def verify(ha, record):
    """True only if the pinned backup is still listed with identical metadata."""
    _require(type(record) is dict and record.get('schema') == SCHEMA, 'CUSTODY_RECORD')
    body = {k: record.get(k) for k in ('schema', 'source', 'source_version', *FIELDS)}
    _require(record.get('custody_sha256') == _digest(body), 'CUSTODY_DIGEST')
    found = _matching(ha, record['name'])
    _require(len(found) == 1, 'BACKUP_MISSING_OR_DUPLICATED')
    _require(_metadata(found[0]) == {k: record[k] for k in FIELDS}, 'BACKUP_CHANGED')
    return True
