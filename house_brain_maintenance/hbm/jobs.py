"""Scout job catalog: rotate the Scout inventory key safely, run the Scout once.

Every mutating job follows the same shape: read-only preflight -> owner approval (fixed
template; the manifest's note is shown only as an advisory line) -> journal -> one bounded
change -> read-back. No answer, another user, a lost socket or a timeout is a rejection.

Key rotation never breaks a working Scout (owner decision 2026-09-29, "keep it, fixed"):
1. ``ROTATE_SCOUT_KEY phase=prepare`` (no approval; changes nothing outside this App):
   generate a new key, keep it in this App's /data only, report its SHA-256 fingerprint
   and the fingerprint of the key the Scout uses today.
2. An AI session commits the new fingerprint to the Broker config; the owner taps Deploy.
3. ``ROTATE_SCOUT_KEY phase=activate`` (approval): first proves the Broker already accepts
   the new key (an empty ``POST /v1/inventory`` answers 400 for a known key and 401 for an
   unknown one; nothing is stored either way), then writes it into the Scout's options.
The key itself never leaves this Home Assistant; only fingerprints are reported.
"""
from __future__ import annotations

import os
import re
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field

from . import approval, net
from .ha import AppView, HAError, HomeAssistant, fingerprint
from .journal import Journal
from .manifest import ROTATE_SCOUT_KEY, RUN_SCOUT_ONCE, Manifest
from .web import ApprovalBoard

DONE = "DONE"
DRY_RUN_OK = "DRY_RUN_OK"
REJECTED = "REJECTED"
REFUSED = "REFUSED"
FAILED = "FAILED"
FAILED_MANUAL = "FAILED_MANUAL"

BROKER_HOST_RE = re.compile(r"^house-brain-maintenance-broker\.[a-z0-9-]+\.workers\.dev$")
INVENTORY_PATH = "/v1/inventory"
SCOUT_RESULT_RE = re.compile(r"maintenance_inventory_scout=(?:pass|failed)\b[^\r\n]{0,300}")
SCOUT_PASS_RE = re.compile(
    r"^maintenance_inventory_scout=pass count=([0-9]{1,4}) truncated=false "
    r"disposition=(accept|accept_idempotent) control_authority=none$")
SCOUT_FAIL_RE = re.compile(r"^maintenance_inventory_scout=failed reason=([A-Za-z0-9_]{1,80})$")
SCOUT_OPTION_KEYS = ("broker_inventory_write_key", "broker_url")
STOPPED_STATES = ("stopped", "unknown", "error")
PENDING_DOC = "pending_scout_key"


@dataclass
class Result:
    outcome: str
    reasons: list[str] = field(default_factory=list)
    facts: dict = field(default_factory=dict)


@dataclass
class Settings:
    notify_service: str
    owner_user_id: str
    dry_run: bool
    require_auth: bool
    approval_timeout: float
    board: ApprovalBoard | None = None
    open_url: str | None = None
    scout_run_timeout: float = 600.0
    poll: float = 5.0
    should_stop: object = None


def checked_inventory_url(url: str | None) -> str:
    """The Scout's own Broker URL, accepted only in the exact expected shape."""
    if not isinstance(url, str) or url != url.strip():
        raise HAError("SCOUT_BROKER_URL")
    try:
        parsed = urllib.parse.urlsplit(url)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        raise HAError("SCOUT_BROKER_URL") from None
    if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.query
            or parsed.fragment or port not in (None, 443) or parsed.path != INVENTORY_PATH
            or not host or not BROKER_HOST_RE.fullmatch(host)):
        raise HAError("SCOUT_BROKER_URL")
    return f"https://{host}{INVENTORY_PATH}"


def parse_scout_result(logs: str) -> tuple[str, dict]:
    """(outcome, safe facts) from the Scout's latest-run log; never returns raw log text."""
    lines = [m.group(0).strip() for m in SCOUT_RESULT_RE.finditer(logs)]
    if not lines:
        return "none", {}
    last = lines[-1]
    passed = SCOUT_PASS_RE.fullmatch(last)
    if passed:
        return "pass", {"app_count": int(passed.group(1)), "disposition": passed.group(2)}
    failed = SCOUT_FAIL_RE.fullmatch(last)
    return "failed", {"scout_reason": failed.group(1) if failed else "unparsed_result"}


def broker_accepts(inventory_url: str, key: str, timeout: float = 20.0) -> tuple[bool, str]:
    """Probe: does the Broker accept this inventory key? Sends an empty body (never stored)."""
    try:
        net.request("POST", inventory_url, {"Authorization": f"Bearer {key}"}, body={}, timeout=timeout)
    except net.NetError as err:
        if err.status == 400:
            return True, "accepted (empty test body refused as invalid, nothing stored)"
        if err.status == 401:
            return False, "the Broker does not accept the new key yet (401)"
        return False, f"Broker probe failed (HTTP {err.status})"
    return False, "unexpected: the Broker accepted an empty inventory"


class Engine:
    def __init__(self, ha: HomeAssistant, journal: Journal, settings: Settings) -> None:
        self.ha = ha
        self.j = journal
        self.s = settings

    # -- helpers --------------------------------------------------------------
    def _ask(self, m: Manifest, title: str, lines: list[str]) -> str:
        text = "\n".join(lines)
        if m.note:
            text += f"\n\nAI note (advisory, not part of what you approve):\n{m.note}"
        text += f"\n\nRequest: {m.request_id}"
        self.j.audit(m.request_id, "APPROVAL_REQUESTED", job=m.job)
        decision = approval.ask(self.ha, self.s.notify_service, self.s.owner_user_id, stage="JOB",
                                title=title, message=text, timeout=self.s.approval_timeout,
                                require_auth=self.s.require_auth, board=self.s.board,
                                open_url=self.s.open_url, should_stop=self.s.should_stop)
        self.j.audit(m.request_id, "APPROVAL_" + decision.outcome, channel=decision.channel,
                     ignored=decision.ignored_events)
        return decision.outcome

    def _scout_ready(self, m: Manifest) -> tuple[AppView, str | None]:
        view = self.ha.scout_view()
        if view.version != m.expect_scout_version:
            return view, f"Scout version is {view.version}, request expects {m.expect_scout_version}"
        if view.state not in STOPPED_STATES:
            return view, f"Scout is {view.state}; it must be stopped"
        if tuple(sorted(view.option_keys)) != SCOUT_OPTION_KEYS:
            return view, "the Scout's settings are not exactly broker_url + broker_inventory_write_key"
        return view, None

    def _pending(self) -> dict | None:
        doc = self.j.load_doc(PENDING_DOC, None)
        if isinstance(doc, dict) and isinstance(doc.get("key"), str):
            net.register_secret(doc["key"])
            return doc
        return None

    # -- jobs -----------------------------------------------------------------
    def run(self, m: Manifest) -> Result:
        try:
            if m.job == ROTATE_SCOUT_KEY and m.phase == "prepare":
                return self._prepare(m)
            if m.job == ROTATE_SCOUT_KEY and m.phase == "activate":
                return self._activate(m)
            if m.job == RUN_SCOUT_ONCE:
                return self._run_scout(m)
        except (HAError, net.NetError) as err:
            reason = net.redact(str(err))[:200]
            if self.j.load_txn():
                return Result(FAILED_MANUAL, [f"read-back failed ({reason}); it is retried on the next poll"])
            return Result(FAILED, [f"preflight: {reason}"])
        return Result(REFUSED, ["unknown job"])

    def _prepare(self, m: Manifest) -> Result:
        scout, problem = self._scout_ready(m)
        if problem:
            return Result(REFUSED, [problem])
        url = checked_inventory_url(scout.broker_url)
        facts = {"scout_version": scout.version, "broker_host": urllib.parse.urlsplit(url).hostname,
                 "current_fingerprint": scout.key_fingerprint}
        if self.s.dry_run:
            return Result(DRY_RUN_OK, ["dry run: preflight passed, no key generated"], facts)
        pending = self._pending()
        if pending is None:
            key = secrets.token_urlsafe(48)            # 64 chars, 384 random bits
            net.register_secret(key)
            pending = {"key": key, "fingerprint": fingerprint(key), "created": time.time()}
            self.j.save_doc(PENDING_DOC, pending)
            os.chmod(os.path.join(self.j.dir, f"{PENDING_DOC}.json"), 0o600)
        facts["new_fingerprint"] = pending["fingerprint"]
        return Result(DONE, ["new key generated and kept in this App only; the Scout is unchanged",
                             "next: put new_fingerprint in the Broker config, Deploy, then phase=activate"], facts)

    def _activate(self, m: Manifest) -> Result:
        scout, problem = self._scout_ready(m)
        if problem:
            return Result(REFUSED, [problem])
        pending = self._pending()
        if pending is None or pending.get("fingerprint") != m.expect_new_fingerprint:
            return Result(REFUSED, ["no prepared key with that fingerprint; run phase=prepare first"])
        url = checked_inventory_url(scout.broker_url)
        host = urllib.parse.urlsplit(url).hostname
        facts = {"scout_version": scout.version, "broker_host": host,
                 "old_fingerprint": scout.key_fingerprint, "new_fingerprint": pending["fingerprint"]}
        accepted, why = broker_accepts(url, pending["key"])
        facts["broker_probe"] = why
        if not accepted:
            return Result(REFUSED, [why, "the Scout keeps its current (working) key"], facts)
        lines = [
            "Switch the Scout to its NEW inventory key.",
            f"Scout: {self.ha.scout} {scout.version} (stopped; it is not started)",
            f"Broker: {host} - already accepts the new key (checked just now)",
            f"Key fingerprint: {str(scout.key_fingerprint)[:12]} -> {pending['fingerprint'][:12]}",
            "The key never leaves this Home Assistant. Only fingerprints go to GitHub.",
        ]
        if self.s.dry_run:
            return Result(DRY_RUN_OK, ["dry run: preflight and Broker check passed, nothing changed"],
                          {**facts, "would_ask": lines})
        outcome = self._ask(m, "Maintenance: switch Scout key", lines)
        if outcome != approval.APPROVE:
            return Result(REJECTED, [f"owner decision: {outcome}"], facts)
        self.j.save_txn({"request_id": m.request_id, "job": m.job, "stage": "WRITING", "digest": m.digest,
                         "issue": m.tracking_issue, "fingerprint": pending["fingerprint"], "broker_url": url})
        self.j.audit(m.request_id, "ROTATE_WRITING", fingerprint_prefix=pending["fingerprint"][:12])
        write_error = None
        try:
            self.ha.set_scout_options(url, pending["key"])
        except (HAError, net.NetError) as err:
            write_error = f"write: {net.redact(str(err))[:200]}"
        return self._activate_readback(m.request_id, pending["fingerprint"], url, write_error)

    def _activate_readback(self, rid: str, fp: str, url: str, write_error: str | None) -> Result:
        view = self.ha.scout_view()
        self.j.clear_txn()
        facts = {"new_fingerprint": fp, "scout_version": view.version,
                 "broker_host": urllib.parse.urlsplit(url).hostname}
        if view.key_fingerprint == fp and view.broker_url == url:
            self.j.save_doc(PENDING_DOC, {})
            reasons = ["Scout options hold the new key (read back by fingerprint)"]
            if write_error:
                reasons.append(f"write reported an error but read-back matches: {write_error}")
            self.j.audit(rid, "ROTATE_DONE", fingerprint_prefix=fp[:12])
            return Result(DONE, reasons, facts)
        return Result(FAILED, [write_error or "read-back does not match the new key",
                               "the Scout still holds its previous key; activate again with a new request"], facts)

    def _run_scout(self, m: Manifest) -> Result:
        scout, problem = self._scout_ready(m)
        if problem:
            return Result(REFUSED, [problem])
        if m.expect_key_fingerprint and scout.key_fingerprint != m.expect_key_fingerprint:
            return Result(REFUSED, ["the Scout's key fingerprint does not match the request"])
        checked_inventory_url(scout.broker_url)
        lines = [
            "Run the Scout once. It lists your Apps (names, versions, state) and sends that list to the "
            "Broker. It changes nothing and stops by itself.",
            f"Scout: {self.ha.scout} {scout.version}",
            f"Key fingerprint: {str(scout.key_fingerprint)[:12]}",
        ]
        facts = {"scout_version": scout.version, "fingerprint_prefix": str(scout.key_fingerprint)[:12]}
        if self.s.dry_run:
            return Result(DRY_RUN_OK, ["dry run: preflight passed, nothing changed"],
                          {**facts, "would_ask": lines})
        outcome = self._ask(m, "Maintenance: run the Scout once", lines)
        if outcome != approval.APPROVE:
            return Result(REJECTED, [f"owner decision: {outcome}"], facts)
        self.j.save_txn({"request_id": m.request_id, "job": m.job, "stage": "RUNNING", "digest": m.digest,
                         "issue": m.tracking_issue, "started_at": time.time()})
        self.j.audit(m.request_id, "SCOUT_STARTING")
        try:
            self.ha.start_scout()
        except (HAError, net.NetError) as err:
            self.j.clear_txn()
            return Result(FAILED, [f"start: {net.redact(str(err))[:200]}"], facts)
        return self._scout_readback(m.request_id, time.monotonic() + self.s.scout_run_timeout, facts)

    def _scout_readback(self, request_id: str, deadline: float, facts: dict) -> Result:
        # The Scout is one-shot: once it is stopped again, /logs/latest holds exactly that run.
        while True:
            state = self.ha.scout_view().state
            if state in STOPPED_STATES:
                break
            if time.monotonic() > deadline:
                self.j.clear_txn()
                return Result(FAILED_MANUAL, [f"the Scout did not finish in time (state {state})",
                                              "nothing is stopped automatically; check the Scout log"], facts)
            time.sleep(self.s.poll)
        outcome, parsed = parse_scout_result(self.ha.scout_latest_logs())
        self.j.clear_txn()
        facts = {**facts, **parsed}
        if outcome == "pass":
            self.j.audit(request_id, "SCOUT_PASS", count=parsed["app_count"])
            return Result(DONE, ["the Scout published a complete inventory to the Broker"], facts)
        if outcome == "none":
            return Result(FAILED, ["the Scout stopped without a result line"], facts)
        return Result(FAILED, [f"the Scout reported failure: {parsed['scout_reason']}"], facts)

    # -- recovery ---------------------------------------------------------------
    def recover(self, txn: dict) -> tuple[str, str, Result]:
        rid = str(txn.get("request_id", "unknown"))
        job = str(txn.get("job", "?"))
        try:
            if job == ROTATE_SCOUT_KEY:
                return rid, job, self._activate_readback(rid, str(txn.get("fingerprint", "")),
                                                         str(txn.get("broker_url", "")),
                                                         "the App restarted during the write")
            if job == RUN_SCOUT_ONCE:
                started = float(txn.get("started_at", 0.0))
                remaining = max(0.0, started + self.s.scout_run_timeout - time.time())
                return rid, job, self._scout_readback(rid, time.monotonic() + remaining, {"recovered": True})
        except (HAError, net.NetError) as err:
            return rid, job, Result(FAILED_MANUAL, [f"recovery read-back failed: {net.redact(str(err))[:200]}"])
        self.j.clear_txn()
        return rid, job, Result(FAILED_MANUAL, ["unknown journalled job"])
