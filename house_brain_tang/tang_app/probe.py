"""Local check of Tang's advertisement, and the public signing-key thumbprints for the log.

`check()` asks Tang itself (GET http://127.0.0.1:7500/adv) and answers only (ok, reason). The body is
parsed and judged here, then dropped: it is never returned, logged or served. An advertisement holds
public keys only; one that carries a private-key member is judged INVALID, never passed on.

`thumbprints()` runs Debian's own `tang-show-keys` (the signing-key thumbprint, SHA-256), the value the
MacBook compares before Clevis binds (design R1 §5 step 3). Only lines shaped like a thumbprint are kept.
"""
import base64
import binascii
import json
import re
import subprocess
import urllib.error
import urllib.request

ADV_URL = 'http://127.0.0.1:7500/adv'
TANG_PORT = 7500
SHOW_KEYS = '/usr/bin/tang-show-keys'
TIMEOUT_S = 3
MAX_ADV_BYTES = 65536
B64URL = re.compile(r'[A-Za-z0-9_-]+')
THUMBPRINT = re.compile(r'[A-Za-z0-9_-]{43}')  # base64url SHA-256, no padding
PRIVATE_MEMBERS = frozenset({'d', 'p', 'q', 'dp', 'dq', 'qi', 'k', 'oth'})
REASON_UNREACHABLE = 'TANG_ADVERTISEMENT_UNREACHABLE'
REASON_INVALID = 'TANG_ADVERTISEMENT_INVALID'


def _no_duplicates(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError('duplicate key')
        out[key] = value
    return out


def _no_constant(_):
    raise ValueError('constant')


def _strict_json(raw):
    return json.loads(raw.decode('utf-8'), object_pairs_hook=_no_duplicates, parse_constant=_no_constant)


def _b64url(text):
    if not isinstance(text, str) or not text or not B64URL.fullmatch(text):
        raise ValueError('not base64url')
    return base64.urlsafe_b64decode(text + '=' * (-len(text) % 4))


def valid_advertisement(raw):
    """True only for a JWS in JSON serialization (flattened, as Tang sends with one signing key, or general)
    whose payload is a public JWK set with a verify key and a deriveKey key, and no private member
    anywhere. Never raises."""
    try:
        if not isinstance(raw, (bytes, bytearray)) or not raw or len(raw) > MAX_ADV_BYTES:
            return False
        jws = _strict_json(bytes(raw))
        if not isinstance(jws, dict) or not isinstance(jws.get('payload'), str):
            return False
        if 'signatures' in jws:  # general serialization (several signing keys)
            if 'signature' in jws or 'protected' in jws:
                return False
            signatures = jws['signatures']
        else:  # flattened serialization (Tang's form with one signing key)
            signatures = [{'protected': jws.get('protected'), 'signature': jws.get('signature')}]
        if not isinstance(signatures, list) or not signatures:
            return False
        for sig in signatures:
            if not isinstance(sig, dict):
                return False
            _b64url(sig.get('protected'))
            _b64url(sig.get('signature'))
        jwks = _strict_json(_b64url(jws['payload']))
        keys = jwks.get('keys') if isinstance(jwks, dict) else None
        if not isinstance(keys, list) or not keys:
            return False
        ops = set()
        for key in keys:
            if not isinstance(key, dict) or not isinstance(key.get('kty'), str) or PRIVATE_MEMBERS & set(key):
                return False
            key_ops = key.get('key_ops', [])
            if not isinstance(key_ops, list):
                return False
            ops.update(op for op in key_ops if isinstance(op, str))
        return {'verify', 'deriveKey'} <= ops
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error, RecursionError):
        return False


def fetch(url=ADV_URL, timeout=TIMEOUT_S):
    """The raw body (bounded) or None. A non-200 answer, refusal or timeout is None."""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method='GET'), timeout=timeout) as response:
            if response.status != 200:
                return None
            raw = response.read(MAX_ADV_BYTES + 1)
            return raw if len(raw) <= MAX_ADV_BYTES else b''
    except (urllib.error.URLError, OSError, ValueError):
        return None


def check(url=ADV_URL, timeout=TIMEOUT_S, get=fetch):
    """(True, None) when Tang answers a valid advertisement; else (False, fixed reason code)."""
    raw = get(url, timeout)
    if raw is None:
        return False, REASON_UNREACHABLE
    return (True, None) if valid_advertisement(raw) else (False, REASON_INVALID)


def thumbprints(run=subprocess.run, show_keys=SHOW_KEYS, port=TANG_PORT):
    """Signing-key thumbprints from Debian's tang-show-keys (public values), or [] when it cannot say."""
    try:
        result = run([show_keys, str(port)], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                     timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0 or not isinstance(result.stdout, str):
        return []
    return sorted({line.strip() for line in result.stdout.splitlines() if THUMBPRINT.fullmatch(line.strip())})
