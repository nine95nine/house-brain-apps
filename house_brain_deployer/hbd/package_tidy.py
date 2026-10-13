"""Fixed package-folder tidy (0.3.24). No caller-supplied path, no YAML, no restart.

Owner pop-ups 2026-10-13 (".bak rename" -> "Build now, install later"; D1 2026-10-07: the tap-gated
Deployer is the package installer). One operation only: rename the inert, space-named backup
``house_energy_brain_v1_1.yaml copy.bak`` to ``house_energy_brain_v1_1.yaml.copy.bak``. Both names are
literals here; a request can name neither. The bytes are kept (link, verify, unlink; the existing retire
steps), never edited or deleted. Home Assistant does not load ``.bak`` files, so nothing is restarted.
"""
from __future__ import annotations

from .manifest import Manifest, ManifestError, RetireOp, _obj, _str, RE_REQUEST_ID, RE_SHA256, REQUESTERS

SCHEMA = "house_brain_package_tidy_request.v1"
OPERATION = "rename_space_named_backup"
SOURCE_NAME = "house_energy_brain_v1_1.yaml copy.bak"
SOURCE = "/config/packages/" + SOURCE_NAME
DEST = "/config/packages/house_energy_brain_v1_1.yaml.copy.bak"


def request(data: dict, digest: str) -> Manifest:
    obj = _obj(data, "package tidy", {"schema", "operation", "request_id", "requested_by", "tracking_issue"},
               {"expect_current_sha256"})
    if obj["operation"] != OPERATION:
        raise ManifestError("TIDY_OPERATION")
    rid = _str(obj["request_id"], "request_id", RE_REQUEST_ID, 64)
    who = obj["requested_by"]
    if who not in REQUESTERS:
        raise ManifestError("REQUESTER")
    issue = obj["tracking_issue"]
    if isinstance(issue, bool) or not isinstance(issue, int) or not 1 <= issue <= 10_000_000:
        raise ManifestError("RANGE", "tracking_issue")
    expect = None
    if "expect_current_sha256" in obj:
        expect = _str(obj["expect_current_sha256"], "expect_current_sha256", RE_SHA256, 64)
    return Manifest(rid, "Rename the space-named package backup", who, "main", "0" * 40, issue, "", (),
                    (RetireOp(SOURCE, DEST, expect),), (), (), (), (), ("ERROR", "CRITICAL"), 0,
                    digest=digest, summary="Same bytes, new name without the space; nothing is loaded or restarted",
                    tidy_operation=OPERATION)
