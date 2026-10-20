"""0.9.24 ENERGY_AI_SETTINGS: deliver Energy AI's private household settings once, owner-approved.

The settings file sits next to the request manifest on this private repository's request branch, pinned by its
SHA-256 and source commit (the commit must be on main). The Energy AI App must be the store App, at the expected
version and stopped. After the owner approves on the phone: ``setup_only`` on, start (the App waits 60 s for one
frame), build and send the frame here (it expires 5 minutes after it is issued), read the App's one-line receipt
from its log and compare it with what was sent, then ``setup_only`` off again whatever happened. The App starts no
runtime and exits by itself. Bundle contents are never logged, reported or kept.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from collections.abc import Callable

from . import approval, net
from .ha import HAError, HomeAssistant
from .jobs import DONE, DRY_RUN_OK, FAILED, REFUSED, REJECTED, Result
from .manifest import Manifest

REQUESTS_DIR = "maint/requests"
BUNDLE_NAME = "household.bundle"
BUNDLE_MAX_BYTES = 2_000_000              # contracts/house_brain_energy_ai_delivery.v1.json
FRAME_MAX_BYTES = 4_016_384
FRAME_LIFETIME_SECONDS = 300
RECEIPT_WAIT_SECONDS = 75.0               # the App waits 60 s for the frame; allow its start and log flush
RECEIPT_POLL_SECONDS = 3.0
RECEIPT_FRESH_SECONDS = 60.0              # the delivery contract's receipt freshness
FINAL_STATE_WAIT_SECONDS = 30.0           # the App exits by itself after its receipt
RECEIPT_MAX_BYTES = 4096
PENDING_DOC = "energy_ai_settings_pending"
STOPPED = ("stopped", "unknown", "error")
RECEIPT_FIELDS = frozenset({"schema_version", "request_id", "operation", "manifest_sha256", "active_sha256",
                            "source_commit", "mode", "physical_actions", "runtime_started", "at", "status"})
READBACK_STATUSES = ("ACTIVE", "SETUP_REQUIRED")
ACTIVATE_STATUSES = ("ACTIVATED", "ALREADY_ACTIVE")
RE_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def canonical(value: dict) -> bytes:
    """Energy AI's own canonical form (installation.canonical)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def build_frame(m: Manifest, request_id: str, now: float, bundle: bytes | None) -> bytes:
    """The one stdin frame (no trailing newline: the Supervisor appends it)."""
    frame = {"schema_version": 1, "request_id": request_id, "operation": m.energy_operation,
             "manifest_sha256": m.energy_manifest_sha256, "issued_at": now,
             "expires": now + FRAME_LIFETIME_SECONDS}
    if m.energy_operation == "ACTIVATE":
        if bundle is None:
            raise ValueError("ENERGY_BUNDLE_MISSING")
        frame.update(bundle_text=bundle.decode("utf-8"), bundle_sha256=m.energy_bundle_sha256,
                     expected_current_sha256=m.energy_expected_current, source_commit=m.energy_source_commit)
    elif bundle is not None:
        raise ValueError("ENERGY_READBACK_WITH_BUNDLE")
    raw = canonical(frame)
    if len(raw) >= FRAME_MAX_BYTES:
        raise ValueError("ENERGY_FRAME_LIMIT")
    return raw


def _receipts(log_text: str):
    for line in log_text.splitlines():
        start = line.find("{")
        if start < 0 or len(line) - start > RECEIPT_MAX_BYTES:
            continue
        try:
            obj = json.loads(line[start:])
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("mode") == "SETUP_ONLY":
            yield obj


def refusals(log_text: str) -> int:
    return sum(1 for r in _receipts(log_text) if r.get("status") == "REFUSED" and "request_id" not in r)


def find_receipt(log_text: str, request_id: str, refusals_before: int) -> dict | None:
    """The App's receipt for this frame: its own request id, or a refusal newer than the ones already in the log
    before the send (a refusal names no request id). Log lines carry a timestamp prefix; the JSON is the rest."""
    for r in _receipts(log_text):
        if r.get("request_id") == request_id:
            return r
    if refusals(log_text) > refusals_before:
        return {"status": "REFUSED", "reason": "PRIVATE_SETUP_INVALID_OR_UNAVAILABLE", "mode": "SETUP_ONLY"}
    return None


def check_receipt(m: Manifest, receipt: dict, request_id: str, sent_at: float) -> list[str]:
    """Empty when the receipt proves the delivery; otherwise the reasons it does not. HTTP success is not proof."""
    if receipt.get("status") == "REFUSED":
        return ["Energy AI refused the frame (" + str(receipt.get("reason", "no reason"))[:80] + ")"]
    problems = []
    if set(receipt) != RECEIPT_FIELDS or receipt.get("schema_version") != 1:
        problems.append("receipt fields")
    if receipt.get("request_id") != request_id or receipt.get("operation") != m.energy_operation:
        problems.append("receipt request")
    if receipt.get("manifest_sha256") != m.energy_manifest_sha256:
        problems.append("receipt App source")
    if receipt.get("physical_actions") is not False or receipt.get("runtime_started") is not False:
        problems.append("receipt authority")
    at = receipt.get("at")
    if isinstance(at, bool) or not isinstance(at, int | float) or abs(at - sent_at) > RECEIPT_FRESH_SECONDS:
        problems.append("receipt time")
    if m.energy_operation == "READBACK":
        if receipt.get("status") not in READBACK_STATUSES:
            problems.append("receipt status")
    else:
        if receipt.get("status") not in ACTIVATE_STATUSES:
            problems.append("receipt status")
        if receipt.get("active_sha256") != m.energy_bundle_sha256 \
                or receipt.get("source_commit") != m.energy_source_commit:
            problems.append("receipt active settings")
    return problems


class EnergyDelivery:
    def __init__(self, ha: HomeAssistant, journal, settings, gh, ask: Callable[[Manifest, str, list[str]], str],
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> None:
        self.ha, self.j, self.s, self.gh, self.ask = ha, journal, settings, gh, ask
        self.clock, self.sleep = clock, sleep

    def _bundle(self, m: Manifest, head: str) -> tuple[bytes | None, str | None]:
        if m.energy_operation != "ACTIVATE":
            return None, None
        try:
            on_main = self.gh.is_ancestor(m.energy_source_commit, "main")
        except net.NetError as err:
            if err.status not in (404, 422):              # an unknown commit: never on main
                raise
            on_main = False
        if not on_main:
            return None, "the settings' source commit is not on main"
        raw = self.gh.read_file(f"{REQUESTS_DIR}/{m.request_id}/{BUNDLE_NAME}", head)
        if len(raw) > BUNDLE_MAX_BYTES:
            return None, "the settings file is larger than 2 MB"
        if hashlib.sha256(raw).hexdigest() != m.energy_bundle_sha256:
            return None, "the settings file does not match the request's SHA-256"
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            return None, "the settings file is not UTF-8 text"
        return raw, None

    def run(self, m: Manifest, head: str) -> Result:
        facts = {"operation": m.energy_operation, "expect_version": m.energy_version}
        slug = self.ha.energy_slug()
        if slug is None:
            return Result(REFUSED, ["no store Energy AI App can be named from this App's store"], facts)
        if slug not in {a.slug for a in self.ha.installed_apps()}:
            return Result(REFUSED, [f"{slug} is not installed"], facts)
        facts["slug"] = slug
        try:
            bundle, problem = self._bundle(m, head)
        except net.NetError as err:
            return Result(FAILED, [f"preflight: settings file not readable ({err.status}); nothing asked or changed"],
                          facts)
        if problem:
            return Result(REFUSED, [problem + "; nothing asked or changed"], facts)
        if bundle is not None:
            facts.update(bundle_sha256=m.energy_bundle_sha256, source_commit=m.energy_source_commit)
        with self.ha.energy_target(slug):
            state, version, options = self.ha.energy_read()
            facts.update(state=state, version=version)
            if version != m.energy_version:
                return Result(REFUSED, [f"Energy AI is {version or 'unknown'}, request expects {m.energy_version}"],
                              facts)
            if state not in STOPPED:
                return Result(REFUSED, [f"Energy AI is {state}; it must be stopped"], facts)
            if options.get("setup_only") is not False:
                return Result(REFUSED, ["Energy AI's setup_only is already on; inspect it first"], facts)
            if self.s.dry_run:
                facts["would_ask"] = True
                return Result(DRY_RUN_OK, ["dry run: nothing asked or changed"], facts)
            lines = [f"Energy AI {version}: {m.energy_operation.lower()} its private household settings."]
            if bundle is not None:
                lines.append(f"Settings {m.energy_bundle_sha256[:12]}… from commit {m.energy_source_commit[:12]}.")
            lines.append("It starts in setup-only mode, receives one frame, reports and stops by itself. "
                         "No runtime, no device control.")
            outcome = self.ask(m, "Maintenance: Energy AI settings", lines)
            if outcome != approval.APPROVE:
                return Result(REJECTED, [f"owner decision: {outcome}"], facts)
            self.j.save_doc(PENDING_DOC, {"request_id": m.request_id, "slug": slug})
            try:
                return self._deliver(m, bundle, facts)
            finally:
                self._restore(m.request_id, facts)

    def _deliver(self, m: Manifest, bundle: bytes | None, facts: dict) -> Result:
        try:
            before = refusals(self.ha.app_log_boot(facts["slug"], 0, lines=200))
        except (HAError, net.NetError):
            before = 0
        self.ha.energy_set_setup_only(True)
        self.ha.energy_start()
        request_id = secrets.token_hex(16)
        sent_at = self.clock()
        frame = build_frame(m, request_id, sent_at, bundle)
        self.ha.energy_pin_frame(frame)
        self.ha.energy_send_frame(frame)
        self.j.audit(m.request_id, "ENERGY_FRAME_SENT", operation=m.energy_operation)
        deadline = sent_at + RECEIPT_WAIT_SECONDS
        receipt = None
        while receipt is None and self.clock() < deadline:
            self.sleep(RECEIPT_POLL_SECONDS)
            try:
                text = self.ha.app_log_boot(facts["slug"], 0, lines=200)
            except (HAError, net.NetError):
                continue
            receipt = find_receipt(text, request_id, before)
        if receipt is None:
            return Result(FAILED, ["no receipt from Energy AI within 75 s; file a READBACK request to see what is "
                                   "active before retrying"], facts)
        problems = check_receipt(m, receipt, request_id, sent_at)
        facts.update(status=receipt.get("status"), active_sha256=receipt.get("active_sha256"))
        facts["final_state"] = self._final_state()
        if problems:
            return Result(FAILED, problems + ["file a READBACK request to see what is active"], facts)
        lines = [f"Energy AI reported {receipt['status']}"]
        if facts["final_state"] not in STOPPED:
            lines.append(f"Energy AI is still {facts['final_state']}; it stops by itself after setup (check it)")
        return Result(DONE, lines, facts)

    def _final_state(self) -> str:
        """The contract's final stopped state: read only, never a stop."""
        state = "unknown"
        deadline = self.clock() + FINAL_STATE_WAIT_SECONDS
        while True:
            try:
                state = self.ha.energy_read()[0]
            except (HAError, net.NetError):
                state = "unknown"
            if state in ("stopped", "error") or self.clock() >= deadline:
                return state
            self.sleep(RECEIPT_POLL_SECONDS)

    def _restore(self, request_id: str, facts: dict) -> None:
        """setup_only off again, whatever happened. A failure here leaves the pending record for the next poll."""
        try:
            self.ha.energy_set_setup_only(False)
            self.j.save_doc(PENDING_DOC, {})
            facts["setup_only_restored"] = True
        except (HAError, net.NetError) as err:
            facts["setup_only_restored"] = False
            self.j.audit(request_id, "ENERGY_RESTORE_DEFERRED", reason=net.redact(str(err))[:120])

    def recover(self, doc: dict) -> tuple[str, Result] | None:
        """After a restart mid-delivery: switch setup_only off (the only thing this job ever turns on)."""
        slug = doc.get("slug")
        rid = str(doc.get("request_id") or "")
        if not isinstance(slug, str) or slug != self.ha.energy_slug():
            self.j.save_doc(PENDING_DOC, {})
            return None
        with self.ha.energy_target(slug):
            _, _, options = self.ha.energy_read()
            if options.get("setup_only") is True:
                self.ha.energy_set_setup_only(False)
        self.j.save_doc(PENDING_DOC, {})
        return rid, Result(FAILED, ["interrupted mid-delivery; setup_only switched off again. Check Energy "
                                           "AI's log and file a READBACK request to see what is active"],
                           {"slug": slug, "setup_only_restored": True})
