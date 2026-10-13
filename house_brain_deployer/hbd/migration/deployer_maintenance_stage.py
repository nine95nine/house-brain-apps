"""Inactive Maintenance-stage to Deployer byte-contract adapter, no App route.

The reader is an explicit unavailable-by-default local dependency. Matching hashes
and preparation receipts never establish source provenance or runtime authority.
"""
from dataclasses import dataclass, field
import hashlib
import re

from .. import history
from ..writer import WriterLease

FALSE_FLAGS = ("verified", "stopped_verified", "backup_verified", "undo_verified",
               "applied", "native_execution", "uninstall_allowed")


class BridgeError(ValueError):
    """Fixed sanitized reason only."""


class BorrowedCaptureLease:
    """Adapt the held Deployer fence to Maintenance's True-or-refuse check.

    No acquisition, release, root selection or source attestation occurs here.
    The enclosing coordinator owns the fence and must qualify all participants.
    A successful check proves only the existing cooperative lease's identity.
    """
    def __init__(self, fence):
        _require(type(fence) is WriterLease, "CAPTURE_FENCE_TYPE")
        self._fence = fence
        self._entered = False
        self._used = False

    def __enter__(self):
        _require(not self._used, "CAPTURE_FENCE_REUSE")
        self._used = True
        self._fence.check()
        self._entered = True
        return self

    def check(self):
        _require(self._entered, "CAPTURE_FENCE_NOT_ENTERED")
        self._fence.check()
        return True

    def __exit__(self, *_exc):
        try:
            self.check()
        finally:
            self._entered = False


def _require(condition, reason):
    if not condition:
        raise BridgeError(reason)


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _sha(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


@dataclass(frozen=True)
class PreparedSource:
    _files: tuple = field(repr=False)
    component_sha256: str
    snapshot_sha256: str

    def private_files(self):
        return dict(self._files)

    def receipt(self):
        return {"schema": "hbd.maintenance_stage_bridge.preparation.v1",
                "component_sha256": self.component_sha256, "snapshot_sha256": self.snapshot_sha256,
                "history_sha256": history.fingerprint(self.private_files()),
                "files": len(self._files), "history_bytes": sum(len(v) for _, v in self._files),
                "provenance": "UNATTESTED_INPUT", "capture_trusted": False,
                "applied": False, "runtime_verified": False, "undo_verified": False,
                "stopped_verified": False, "uninstall_allowed": False}


def read_preparation(directory_fd, stage_name, *, expected_component_sha256,
                     expected_snapshot_sha256, expected_files, reader=None):
    """Validate one already selected private stage; no network, writes or issuer.

    Expected identities are comparison inputs, not proof of their trusted origin.
    The reader must remain Pi-local in a future qualified implementation; this
    adapter deliberately supplies neither runtime discovery nor source capture.
    """
    _require(callable(reader), "READER_UNAVAILABLE")
    _require(type(directory_fd) is int and directory_fd >= 0
             and type(stage_name) is str and re.fullmatch(r"stage-[0-9a-f]{32}", stage_name), "STAGE_SELECTION")
    _require(_sha(expected_component_sha256) and _sha(expected_snapshot_sha256), "EXPECTED_IDENTITY")
    _require(type(expected_files) is dict and set(expected_files) == set(history.FILES)
             and all(v is None or _sha(v) for v in expected_files.values()), "EXPECTED_INVENTORY")
    expected_files = dict(expected_files)
    try:
        snapshot = reader(directory_fd, stage_name)
    except Exception:  # noqa: BLE001 - fail closed to a fixed refusal code
        raise BridgeError("STAGE_READ_REFUSED") from None
    return validate_snapshot(snapshot, expected_component_sha256=expected_component_sha256,
                             expected_snapshot_sha256=expected_snapshot_sha256,
                             expected_files=expected_files)


def validate_snapshot(snapshot, *, expected_component_sha256, expected_snapshot_sha256, expected_files):
    """Validate the same private byte contract for component or stage readers."""
    _require(_sha(expected_component_sha256) and _sha(expected_snapshot_sha256), "EXPECTED_IDENTITY")
    _require(type(expected_files) is dict and set(expected_files) == set(history.FILES)
             and all(v is None or _sha(v) for v in expected_files.values()), "EXPECTED_INVENTORY")
    expected_files = dict(expected_files)
    try:
        files = snapshot.private_files()
        receipt = snapshot.receipt()
    except Exception:  # noqa: BLE001 - fail closed to a fixed refusal code
        raise BridgeError("STAGE_READ_REFUSED") from None
    _require(type(files) is dict and not set(files) - set(history.FILES)
             and all(type(v) is bytes for v in files.values()), "PRIVATE_BYTES")
    files = dict(files)
    _require(sum(len(v) for v in files.values()) <= history.MAX_BYTES, "HISTORY_LIMIT")
    actual = {key: _digest(files[key]) if key in files else None for key in history.FILES}
    _require(actual == expected_files, "HISTORY_INVENTORY")
    identity = {"component_sha256": expected_component_sha256,
                "source_slug": "local_house_brain_deployer", "snapshot_version": "0.3.6", "files": actual}
    digest = _digest(history._canonical(identity))
    _require(digest == expected_snapshot_sha256, "SNAPSHOT_IDENTITY")
    expected = {"schema": "hbm.legacy_snapshot.preparation.v1", **identity,
                "snapshot_sha256": digest, "history_bytes": sum(len(v) for v in files.values()),
                "provenance": "UNATTESTED_INPUT", **{key: False for key in FALSE_FLAGS}}
    _require(type(receipt) is dict and set(receipt) == set(expected)
             and all(type(receipt.get(k)) is bool and receipt[k] is False for k in FALSE_FLAGS)
             and type(receipt.get("history_bytes")) is int
             and receipt == expected, "PREPARATION_RECEIPT")
    # Destination qualification still separately checks all semantic history,
    # chronology/conflict and selected recovery-file rules before any staging.
    return PreparedSource(tuple(sorted(files.items())), expected_component_sha256, digest)
