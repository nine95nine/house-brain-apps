"""Offline private bundle preparation and atomic activation; never calls HA.

Hashes detect corruption and bind a reviewed artifact. They are not signatures
or permission to install. Run activation only through the approved release lane.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from .contracts import validate_bindings
from tools.epr6_banking_ledger import load_policy
from tools.evaluate_sense_load_identity_evidence import validate_register

LIMIT = 2_000_000
FILES = {name: name + '.json' for name in
         ('options', 'tariff', 'policy', 'register', 'identity_rules', 'adapters')}
EVIDENCE_FILES = {name: name + '.json' for name in ('bill_packet', 'statements', 'credit_statements')}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('BUNDLE_DUPLICATE_KEY')
        result[key] = value
    return result


def decode(raw):
    return json.loads(raw, object_pairs_hook=unique,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('BUNDLE_NONFINITE')))


def read_private(path):
    # O_NOFOLLOW plus fstat closes the symlink/check/open race for the file.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > LIMIT:
            raise ValueError('BUNDLE_FILE_REFUSED')
        raw = stream.read(LIMIT + 1)
    if len(raw) > LIMIT:
        raise ValueError('BUNDLE_TOO_LARGE')
    return raw


class TextSnapshot:
    """Feed immutable validated bytes to the unchanged accepted policy loader."""
    def __init__(self, text):
        self.text = text

    def read_text(self, encoding='utf-8'):
        return self.text


def validate(raw):
    if len(raw) > LIMIT:
        raise ValueError('BUNDLE_TOO_LARGE')
    packet = decode(raw)
    if not isinstance(packet, dict) or set(packet) != {'schema_version', 'source_commit', 'files'}:
        raise ValueError('BUNDLE_FIELDS')
    if type(packet['schema_version']) is not int or packet['schema_version'] != 2:
        raise ValueError('BUNDLE_SCHEMA')
    if not isinstance(packet['source_commit'], str) or not re.fullmatch('[0-9a-f]{40}', packet['source_commit']):
        raise ValueError('BUNDLE_SOURCE_COMMIT')
    if (not isinstance(packet['files'], dict) or not set(FILES) <= set(packet['files'])
            or set(packet['files']) - set(FILES) - set(EVIDENCE_FILES)):
        raise ValueError('BUNDLE_FILE_SET')
    values = {}
    for name, entry in packet['files'].items():
        if not isinstance(entry, dict) or set(entry) != {'text', 'sha256'} or not isinstance(entry['text'], str):
            raise ValueError('BUNDLE_ENTRY')
        if digest(entry['text'].encode()) != entry['sha256']:
            raise ValueError('BUNDLE_HASH_MISMATCH')
        values[name] = decode(entry['text'])
    options = values['options']
    if not isinstance(options, dict) or set(options) != {'bindings', 'mode', 'publish_status', 'backfill'}:
        raise ValueError('BUNDLE_OPTIONS')
    validate_bindings(options['bindings'])
    if options['mode'] not in {'SHADOW', 'BUILD/REPLAY'} or any(type(options[k]) is not bool for k in ('publish_status', 'backfill')):
        raise ValueError('BUNDLE_NO_CONTROL')
    tariff = values['tariff']
    if not isinstance(tariff, dict) or tariff.get('schema_version') != 'aps_legacy_et2_tariff.v1':
        raise ValueError('BUNDLE_TARIFF_SCHEMA')
    # This release implements exactly the accepted tariff, not arbitrary calendars.
    if tariff.get('authority', {}).get('physical_actions') is not False:
        raise ValueError('BUNDLE_TARIFF_AUTHORITY')
    if packet['files']['tariff']['sha256'] != 'e4fcf593edaa400e598e5fa9ebd4120a4aca314a377862483d871d1dc1b3e242':
        raise ValueError('BUNDLE_TARIFF_REQUIRES_CODE_REVIEW')
    values['parsed_register'] = validate_register(values['register'], values['identity_rules'])
    values['policy'] = load_policy(TextSnapshot(packet['files']['policy']['text']))
    from .native import validate_adapters
    from .billing import statement_summary
    from tools.epr6_banking_ledger import evaluate
    values['adapters'] = validate_adapters(values['adapters'])
    if 'bill_packet' in values:
        values['historical_ledger'] = evaluate(values['bill_packet'], values['policy'])
        if not values['historical_ledger']['ledger_decision_grade']:
            raise ValueError('BUNDLE_HISTORICAL_LEDGER_NOT_RECONCILED')
    if 'statements' in values:
        values['statement_summary'] = statement_summary(values['statements'])
    values['bundle_sha256'] = digest(raw)
    values['source_commit'] = packet['source_commit']
    return values


def prepare(private_root, source_commit, evidence_sources=None):
    entries = {}
    for key, relative in FILES.items():
        raw = read_private(Path(private_root) / relative)
        entries[key] = {'text': raw.decode(), 'sha256': digest(raw)}
    for key, path in (evidence_sources or {}).items():
        if key not in EVIDENCE_FILES:
            raise ValueError('BUNDLE_FILE_SET')
        raw = read_private(path)
        entries[key] = {'text': raw.decode(), 'sha256': digest(raw)}
    raw = canonical({'schema_version': 2, 'source_commit': source_commit, 'files': entries})
    validate(raw)
    return raw


def activate(raw, destination, expected_sha256, replace_sha256=None):
    """Offline CAS replacement. Previous bytes retained separately for explicit rollback.

Caller must stop the app first. No live reload, automatic downgrade or service call.
"""
    if digest(raw) != expected_sha256:
        raise ValueError('BUNDLE_REVIEW_DIGEST_MISMATCH')
    validate(raw)
    destination = Path(destination)
    parent = destination.parent
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError('BUNDLE_PARENT_REFUSED')
    lock = parent / '.energy-bundle-install.lock'
    lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        if destination.exists() or destination.is_symlink():
            previous = read_private(destination)
            if digest(previous) != replace_sha256:
                raise ValueError('BUNDLE_REPLACEMENT_REQUIRES_EXACT_DIGEST')
            validate(previous)
            atomic_write(parent / 'household.previous.json', previous)
        elif replace_sha256 is not None:
            raise ValueError('BUNDLE_REPLACEMENT_MISSING')
        atomic_write(destination, raw)
    finally:
        os.close(lock_fd)
        lock.unlink()


def atomic_write(path, raw):
    fd, name = tempfile.mkstemp(prefix='.energy-bundle-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    build = commands.add_parser('prepare')
    build.add_argument('--private-root', type=Path, required=True)
    build.add_argument('--source-commit', required=True)
    build.add_argument('--out', type=Path, required=True)
    check = commands.add_parser('check')
    check.add_argument('bundle', type=Path)
    install = commands.add_parser('activate')
    install.add_argument('bundle', type=Path)
    install.add_argument('--destination', type=Path, required=True)
    install.add_argument('--expected-sha256', required=True)
    install.add_argument('--replace-sha256')
    args = parser.parse_args()
    if args.command == 'prepare':
        raw = prepare(args.private_root, args.source_commit)
        # Preparation cannot overwrite an existing reviewed artifact.
        fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    else:
        raw = read_private(args.bundle)
        validate(raw)
        if args.command == 'activate':
            activate(raw, args.destination, args.expected_sha256, args.replace_sha256)
    print(json.dumps({'status': 'VALIDATED_OFFLINE', 'sha256': digest(raw), 'physical_actions': False}))


if __name__ == '__main__':
    main()
