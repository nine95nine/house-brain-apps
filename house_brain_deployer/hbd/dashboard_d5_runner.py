"""Isolated fake-HA-qualified Dashboard D5 transaction runner (NOT wired into live main).

It reuses Deployer's authenticated owner _ask, partial backup, dashboard
read/save, and Undo custody; it never accepts a model's claim as a
substitute for the authenticated DEPLOY approval.  The dispatcher must
later surround this with the existing shared writer lease and the
startup-history/ledger compatibility guards before release.

The create path is fail-closed and deletes only the exact newly created
id if its config write/readback fails; it never deletes a preexisting
dashboard, changes a YAML-mode dashboard, or restarts Home Assistant.
"""
from __future__ import annotations

from . import approval, dashboard_d5, dashboard_d5_recovery, dashboards
from .engine import Result, REJECTED, TIMED_OUT, FAILED_MANUAL
from .ha import HAError, dashboard_storage_id_matches

PATCHED = "D5_DASHBOARD_PATCHED"
CREATED = "D5_DASHBOARD_CREATED"
FAILED = "D5_DASHBOARD_FAILED"
ROLLBACK_FAILED = "D5_DASHBOARD_MANUAL_RECOVERY"


# Checked against five public Home Assistant Core tags (2026.11.2 added
# 2026-11-22: the owner's installed Core; its lovelace dashboard/const/websocket
# and helpers/collection blobs equal 2026.11.1's). Runtime Core must
# match an examined tag exactly; an unknown version is a no-write HOLD, never
# an assumption that a newer or older API remains compatible.
QUALIFIED_CORE_VERSIONS = frozenset({
    "2026.8.3", "2026.9.2", "2026.10.1", "2026.11.1", "2026.11.2",
})


def _qualified_core(ha) -> None:
    """Preflight existing Supervisor GET /core/info; no extra authority."""
    try:
        info = ha.core_info()
        ver = info.get("version") if type(info) is dict else None
    except Exception as err:  # noqa: BLE001 - no runtime detail leaves device
        raise dashboard_d5.D5Refused("HA_CORE_VERSION_UNAVAILABLE") from err
    if type(ver) is not str or ver not in QUALIFIED_CORE_VERSIONS:
        raise dashboard_d5.D5Refused("HA_CORE_VERSION_NOT_QUALIFIED")


def _existing(ha, path: str):
    """Read raw storage registry rows; legacy list_dashboards intentionally removes IDs.

    A rollback MUST prove exact ID and URL ownership from fresh Core data.
    No registry contents are reported to the caller or leaked into audit.
    """
    rows = ha.ws_once({"type": "lovelace/dashboards/list"}, timeout=30)
    if type(rows) is not list or len(rows) > dashboards.MAX_DASHBOARDS:
        raise dashboard_d5.D5Refused("DASHBOARD_LIST_UNVERIFIED")
    found = []
    for row in rows:
        if type(row) is not dict:
            raise dashboard_d5.D5Refused("DASHBOARD_LIST_UNVERIFIED")
        url = row.get("url_path")
        if url is not None and type(url) is not str:
            raise dashboard_d5.D5Refused("DASHBOARD_LIST_UNVERIFIED")
        if url == path:
            found.append(row)
    if len(found) > 1:
        raise dashboard_d5.D5Refused("DASHBOARD_DUPLICATE")
    return found[0] if found else None


def _preview_live(engine, req):
    current = _existing(engine.ha, req.dashboard)
    if req.kind == "patch":
        if current is None or current.get("mode") != "storage":
            raise dashboard_d5.D5Refused("STORAGE_DASHBOARD_REQUIRED")
        before = dashboards.read_config(engine.ha, req.dashboard)
        candidate = dashboard_d5.preview(req, before)
        return before, candidate
    if current is not None:
        raise dashboard_d5.D5Refused("CREATE_TARGET_EXISTS")
    return None, dashboard_d5.preview(req, None)


def _errcode(err):
    return getattr(err, "code", getattr(err, "reason", type(err).__name__))


def _rollback_created(ha, dashboard_id: str | None, url_path: str) -> bool:
    """Delete only an unchanged, empty, exact-ID dashboard we just created.

    On an uncertain owner/concurrent edit, never delete the dashboard.
    """
    if not dashboard_storage_id_matches(dashboard_id, url_path):
        return False
    try:
        row = _existing(ha, url_path)
        if row is None:
            return True  # already absent: no deletion required
        if row.get("id") != dashboard_id or row.get("mode") != "storage":
            return False
        try:
            current = dashboards.read_config(ha, url_path)
        except HAError as err:
            # Core reports an existing storage dashboard with no saved config
            # separately from a dashboard URL that it cannot resolve.
            if err.code != "WS_LOVELACE_CONFIG_EMPTY":
                return False
        else:
            if current != {"views": []}:
                return False
        if _existing(ha, url_path) != row:
            return False
        ha.ws_once({"type": "lovelace/dashboards/delete", "dashboard_id": dashboard_id}, timeout=60)
        return _existing(ha, url_path) is None
    except Exception:  # noqa: BLE001 - ambiguous ownership always manual
        return False


def run(engine, req: dashboard_d5.D5Request, data_dir: str, dry_run: bool, *,
        ledger_key: str | None = None, reask_of: str | None = None) -> Result:
    """Call only under the Deployer's exclusive writer lease when wired.

    Source-only test runner: no runtime dispatcher imports or calls this.
    """
    engine.j.audit(req.request_id, "RECEIVED", digest=req.digest,
                   requested_by=req.requested_by, kind="dashboard_d5_" + req.kind)
    try:
        _qualified_core(engine.ha)
        before, candidate = _preview_live(engine, req)
    except Exception as err:  # noqa: BLE001 - fail closed before owner prompt
        return Result(FAILED, ["D5 preliminary validation: " + str(_errcode(err))[:80]])
    target = req.dashboard
    message = (
        f"{req.request_id} by {req.requested_by}\n"
        f"Dashboard: {target}\nOperation: {req.kind.upper()} display-only\n"
        f"Proposal: {req.proposal_id}\nCandidate SHA: {req.expect_candidate_sha256}\n"
        + (("It can show these, which can change the house (their own Home Assistant controls): "
            + ", ".join(req.allowed_controls) + "\n") if req.allowed_controls
           else "It adds nothing that can change the house.\n")
        + "This changes ONLY the chosen storage dashboard after a backup. "
        "No Home Assistant restart, package install or physical control. "
        "An AI's claimed approval is not trusted."
    )
    outcome = engine._ask(req.request_id, "DEPLOY", "House Brain: apply D5 dashboard?",
                          message, engine.s.timing.approval_timeout)
    if outcome != approval.APPROVE:
        return Result(REJECTED if outcome == approval.REJECT else TIMED_OUT, [outcome])
    if dry_run:
        return Result("DRY_RUN_OK", ["No dashboard changed in dry run."])
    # TOCTOU: always verify current config and exact candidate AGAIN after owner action.
    try:
        _qualified_core(engine.ha)
        next_before, next_candidate = _preview_live(engine, req)
        if next_before != before or next_candidate != candidate:
            raise dashboard_d5.D5Refused("LIVE_CHANGED_AFTER_APPROVAL")
    except Exception as err:  # noqa: BLE001
        return Result(FAILED, ["D5 post-approval drift: " + str(_errcode(err))[:80]])
    try:
        engine.ha.backup_partial(f"house-brain-d5-{req.request_id}"[:60])
    except Exception as err:  # noqa: BLE001
        return Result(FAILED, ["D5 backup not proven: " + str(_errcode(err))[:80]])
    if req.kind == "patch":
        # Undo is an exact before/after hash, compatible with the older
        # separately owner-approved dashboard undo request type.
        try:
            dashboards._write_undo(
                data_dir, req.request_id,
                {"dashboard": target, "config": before,
                 "sha_before": req.expect_config_sha256,
                 "sha_after": req.expect_candidate_sha256},
            )
        except Exception as err:  # noqa: BLE001
            return Result(FAILED, ["D5 Undo custody failed: " + str(_errcode(err))[:80]])
        try:
            dashboard_d5_recovery.begin(data_dir, req, ledger_key=ledger_key, reask_of=reask_of)
        except Exception:  # noqa: BLE001 - no write if durable admission fails
            return Result(FAILED_MANUAL, ["D5 durable intent unavailable; no dashboard write"])
        # A write is not an atomic compare-and-swap against Core's dashboard
        # editor. If a third party edits between our save and readback, blindly
        # restoring 'before' would destroy that owner's newer changes. Never
        # write over an unknown post-save state; force manual recovery instead.
        try:
            if dashboards.config_sha256(dashboards.read_config(engine.ha, target)) != req.expect_config_sha256:
                return Result(FAILED, ["D5 stale just before save; nothing changed"])
            dashboards.save_config(engine.ha, target, candidate)
            current_hash = dashboards.config_sha256(dashboards.read_config(engine.ha, target))
        except Exception:  # noqa: BLE001 - save may or may not have committed
            try:
                current_hash = dashboards.config_sha256(dashboards.read_config(engine.ha, target))
            except Exception:  # noqa: BLE001 - unknown outcome: preserve remote state
                return Result(FAILED_MANUAL, [
                    "D5 post-save state unreadable; inspect backup and dashboard manually."])
            if current_hash == req.expect_config_sha256:
                return Result(FAILED, ["D5 save did not apply; original remains unchanged."])
            # Even if it matches our candidate, the server write acknowledgement
            # failed; do not fabricate a successful execution receipt.
            return Result(FAILED_MANUAL, [
                "D5 save acknowledgement failed; preserve current dashboard and inspect manually."])
        if current_hash == req.expect_candidate_sha256:
            return Result(PATCHED, ["D5 patched exact dashboard; Undo is available."],
                          {"dashboard": target, "config_sha256_after": req.expect_candidate_sha256})
        if current_hash == req.expect_config_sha256:
            return Result(FAILED, ["D5 write not applied; original remains unchanged."])
        return Result(FAILED_MANUAL, [
            "D5 post-save content differs; preserve concurrent changes and review backup manually."])

    try:
        dashboard_d5_recovery.begin(data_dir, req, ledger_key=ledger_key, reask_of=reask_of)
    except Exception:  # noqa: BLE001
        return Result(FAILED_MANUAL, ["D5 durable create intent unavailable; no dashboard write"])
    # CREATE: only a brand-new, admin-only, not-in-sidebar storage dashboard.
    new_id = None
    try:
        if _existing(engine.ha, target) is not None:
            return Result(FAILED, ["D5 target appeared before create; no change"])
        created = engine.ha.ws_once({
            "type": "lovelace/dashboards/create",
            "title": req.title,
            "url_path": target,
            "mode": "storage",
            "require_admin": True,
            "show_in_sidebar": False,
        }, timeout=60)
        new_id = created.get("id") if isinstance(created, dict) else None
        if not dashboard_storage_id_matches(new_id, target):
            # An unrelated or unverified ID cannot justify a save or cleanup.
            return Result(FAILED_MANUAL, [
                "D5 create returned no verified dashboard id; inspect manually."])
        row = _existing(engine.ha, target)
        if row is None or row.get("id") != new_id or row.get("mode") != "storage":
            return Result(FAILED_MANUAL, [
                "D5 created dashboard registry identity changed; inspect manually."])
        dashboard_d5_recovery.created(data_dir, req.request_id, new_id)
        engine.ha.ws_once({"type": "lovelace/config/save",
                           "url_path": target, "config": candidate}, timeout=60)
        readback = dashboards.config_sha256(dashboards.read_config(engine.ha, target))
        if readback == req.expect_candidate_sha256:
            return Result(CREATED, ["D5 created admin-only storage dashboard; no restart."],
                          {"dashboard": target, "config_sha256_after": readback})
    except Exception:  # noqa: BLE001 - rollback below
        pass
    if _rollback_created(engine.ha, new_id, target):
        return Result(FAILED, ["D5 create/config readback failed; new dashboard removed."])
    return Result(FAILED_MANUAL, [
        "D5 create rollback unverified; check dashboard listing and backup manually."])
