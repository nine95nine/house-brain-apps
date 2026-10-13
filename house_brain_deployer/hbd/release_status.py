"""Read-only local observations; no qualification or execution entitlement."""
from __future__ import annotations

import hashlib
import os
import stat

from . import VERSION

MANIFEST_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "SOURCE_MANIFEST.sha256")


def manifest_identity(path: str = MANIFEST_PATH) -> str:
    """Identify the packaged manifest, not authenticate the running source.

    No directories, symlinks, special files or unbounded reads. The normal path
    is fixed in the image; an argument exists only for isolated fixture tests.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            st = os.fstat(stream.fileno())
            if not stat.S_ISREG(st.st_mode) or not 0 < st.st_size <= 65_536:
                return "UNKNOWN"
            content = stream.read(65_537)
            if len(content) != st.st_size:
                return "UNKNOWN"
            after = os.fstat(stream.fileno())
            if (st.st_size, st.st_mtime_ns, st.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                return "UNKNOWN"
        return hashlib.sha256(content).hexdigest()
    except (OSError, ValueError):
        return "UNKNOWN"


def observations(journal, manifest_digest: str, standby: bool) -> tuple[tuple[str, str], ...]:
    """Separate local observations from still-unverified release/history gates."""
    transaction = "UNKNOWN — journal could not be read"
    try:
        txn = journal.load_txn()
        if txn is None:
            transaction = "None observed in this instance"
        elif isinstance(txn, dict) and txn:
            transaction = "OPEN — recovery or installation is pending"
    except Exception:  # noqa: BLE001, S110 -- do not expose file contents or exceptions on the page
        pass
    freeze = "UNKNOWN — freeze state could not be read"
    try:
        frozen = journal.frozen_by()
        if frozen is None:
            freeze = "None reported in this instance; legacy state is not checked"
        elif isinstance(frozen, str) and frozen:
            freeze = "ACTIVE — new work remains subject to the existing freeze guard"
    except Exception:  # noqa: BLE001, S110 -- display must not turn a read failure into clearance
        pass
    return (
        ("Running App version", VERSION),
        ("Packaged manifest SHA-256", manifest_digest),
        ("Release qualification", "NOT VERIFIED — trusted qualification evidence is not connected"),
        ("Legacy history continuity", "NOT VERIFIED — keep the stopped legacy Deployer installed"),
        ("Writer coordination", "STANDBY" if standby else "NOT VERIFIED — shared writer protocol is pending"),
        ("Current transaction", transaction),
        ("Current freeze", freeze),
        ("Rollback readiness", "Checked per operation; legacy Undo eligibility is NOT VERIFIED"),
    )
