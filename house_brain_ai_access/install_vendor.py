#!/usr/bin/env python3
"""Build-time pinned vendor installation; no runtime download/update facility.

Only five named files are installed. The unused cloudflared executable and its
manifest are deliberately excluded. Full release signatures are a separate gate.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import os
import stat
import urllib.request
import zipfile

LOCK_SHA = 'bd5359643ba4e382fe84f72728eb8c1954496fad201eb687babf861c8b2932b5'
MAX_ARCHIVE = 32 * 1024 * 1024


def asset(lock: bytes, arch: str) -> dict:
    if hashlib.sha256(lock).hexdigest() != LOCK_SHA:
        raise ValueError('LOCK_IDENTITY')
    value = json.loads(lock)
    if arch not in value['assets']:
        raise ValueError('ARCHITECTURE_REJECTED')
    return value['assets'][arch]


def install(raw: bytes, item: dict, destination: Path) -> None:
    if not 0 < len(raw) <= MAX_ARCHIVE or hashlib.sha256(raw).hexdigest() != item['archive_sha256']:
        raise ValueError('ARCHIVE_IDENTITY')
    # Parse and verify everything before creating any destination path.
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = archive.namelist()
        expected = set(item['files']) | {'cloudflared', 'cloudflared-manifest.json'}
        if len(names) != len(expected) or set(names) != expected:
            raise ValueError('ARCHIVE_MEMBERSHIP')
        for entry in archive.infolist():
            mode = entry.external_attr >> 16
            if entry.is_dir() or stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                raise ValueError('ARCHIVE_MEMBER_TYPE')
            # The pinned full-client ZIP also contains a larger cloudflared
            # companion. It is never decompressed or installed by this App.
            limit = 128 * 1024 * 1024 if entry.filename == 'cloudflared' else MAX_ARCHIVE
            if not 0 < entry.file_size <= limit:
                raise ValueError('ARCHIVE_MEMBER_SIZE')
        verified = {}
        for name, digest in item['files'].items():
            data = archive.read(name)
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError('PAYLOAD_IDENTITY')
            verified[name] = data
        binary = verified['tunnel-client']
        if binary[:6] != b'\x7fELF\x02\x01' or int.from_bytes(binary[18:20], 'little') != item['elf_machine']:
            raise ValueError('ELF_ARCHITECTURE')
    destination.mkdir(mode=0o755, parents=False, exist_ok=False)
    for name, data in verified.items():
        path = destination / name
        with path.open('xb') as stream:
            stream.write(data)
        path.chmod(0o555 if name == 'tunnel-client' else 0o444)
        if hashlib.sha256(path.read_bytes()).hexdigest() != item['files'][name]:
            raise ValueError('FINAL_PATH_IDENTITY')


class HttpsOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urllib.parse.urlparse(newurl).scheme != 'https':
            raise ValueError('TLS_DOWNGRADE_REJECTED')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--arch', required=True)
    parser.add_argument('--lock', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    item = asset(args.lock.read_bytes(), args.arch)
    # Build has no credentials. Ignore inherited HTTP proxy configuration.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), HttpsOnly())
    with opener.open(item['url'], timeout=30) as response:
        raw = response.read(MAX_ARCHIVE + 1)
    install(raw, item, args.destination)
    print('PINNED_VENDOR_PAYLOAD_INSTALLED_SIGNATURE_GATE_SEPARATE')


if __name__ == '__main__':
    main()
