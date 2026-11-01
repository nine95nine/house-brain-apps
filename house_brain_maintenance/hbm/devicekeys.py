"""0.9.41 stable device keys (reviewer spec R1 F1, 2026-10-31; owner 2026-10-31 "F1/F3/F7").

A device problem used to be keyed by Home Assistant's 32-character device id (``offline:<device id>``). That id is
a long identifier, so every post scrubbed it to ``offline:[long-id]``, and a review line written with that key was
dropped by the reader's key pattern: no offline or battery review ever counted (live 2026-10-31, the Garden 3.5
review). The key is now ``offline:<domain>:<h>`` / ``battery:<domain>:<h>`` with ``h`` the first HASH_CHARS of an
HMAC-SHA256 of the device id under a salt made once and kept locally (the ``device_keys`` doc, with ``h`` -> device
id for at most MAX_MAP devices). Ten hex characters pass the reader's key pattern and stay under the 32-character
long-id scrub, and the domain says which integration the device belongs to without naming the house.

Open records are re-keyed in place when the App first sweeps with this release (``rename``), keeping ``first``,
``alerted`` and the fingerprint, so no new alert goes out for a device already reported.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets

DOC = "device_keys"
HASH_CHARS = 10
MAX_MAP = 500
KINDS = ("offline", "battery")
RE_SALT = re.compile(r"^[0-9a-f]{32}$")
RE_DOMAIN = re.compile(r"^[a-z0-9_]{1,64}$")
RE_OLD_DEVICE = re.compile(r"^[A-Za-z0-9]{20,64}$")        # a raw Home Assistant device id (never "group"/"long")
FALLBACK_DOMAIN = "device"


def load(raw) -> dict:
    """The doc in its closed shape; a missing or broken salt is made anew (then every key changes once)."""
    doc = raw if isinstance(raw, dict) else {}
    salt = doc.get("salt") if isinstance(doc.get("salt"), str) and RE_SALT.fullmatch(doc.get("salt", "")) else None
    mapping = doc.get("map") if isinstance(doc.get("map"), dict) else {}
    return {"salt": salt or secrets.token_hex(16),
            "map": {str(k): str(v) for k, v in mapping.items() if isinstance(v, str)}}


def short(salt: str, device_id: str) -> str:
    return hmac.new(bytes.fromhex(salt), str(device_id).encode("utf-8"), hashlib.sha256).hexdigest()[:HASH_CHARS]


def domain_of(device: dict | None, platform: str | None) -> str:
    """The device's config-entry domain (projected by ``ha.device_health``), else its entities' platform."""
    for cand in ((device or {}).get("domain"), platform):
        if isinstance(cand, str) and RE_DOMAIN.fullmatch(cand):
            return cand
    return FALLBACK_DOMAIN


def key(kind: str, domain: str, h: str) -> str:
    return f"{kind}:{domain}:{h}"


class Keys:
    """``key_of(kind, device_id, domain)`` for one sweep; remembers h -> device id (bounded, newest kept)."""

    def __init__(self, doc: dict) -> None:
        self.doc = load(doc)
        self.changed = self.doc != (doc if isinstance(doc, dict) else {})

    def key_of(self, kind: str, device_id: str, domain: str) -> str:
        h = short(self.doc["salt"], device_id)
        if self.doc["map"].get(h) != device_id:
            self.doc["map"][h] = device_id
            self.changed = True
            if len(self.doc["map"]) > MAX_MAP:
                for old in list(self.doc["map"])[:len(self.doc["map"]) - MAX_MAP]:
                    del self.doc["map"][old]
        return key(kind, domain, h)

    def device_of(self, h: str) -> str | None:
        return self.doc["map"].get(h)


def rename(open_keys, snapshot: dict, keys: Keys, platform_of: dict[str, str]) -> dict[str, str]:
    """old key -> new key for every open ``offline:<device id>`` / ``battery:<device id>`` record whose device is
    still known (0.9.40 and older keyed by the raw id). Keys of another shape are left alone."""
    devices = snapshot.get("devices") or {}
    out: dict[str, str] = {}
    for old in list(open_keys):
        kind, _, rest = str(old).partition(":")
        if kind not in KINDS or not RE_OLD_DEVICE.fullmatch(rest) or rest not in devices:
            continue
        out[old] = keys.key_of(kind, rest, domain_of(devices.get(rest), platform_of.get(rest)))
    return out


def apply_rename(state: dict, renames: dict[str, str]) -> int:
    """Re-key the issues doc in place (open records, fingerprints, declined/asked marks, summary items, cleared
    names) so ``first``, ``alerted`` and the fingerprint travel with the problem: no second alert. Returns how many."""
    if not renames:
        return 0
    for section in ("open", "declined", "asked", "log_reported", "fps"):
        table = state.get(section)
        if not isinstance(table, dict):
            continue
        for old, new in renames.items():
            if old in table and new not in table:
                table[new] = table.pop(old)
            elif old in table:
                table.pop(old)
    for item in state.get("summary") or []:
        if isinstance(item, dict) and item.get("key") in renames:
            item["key"] = renames[item["key"]]
    return len(renames)
