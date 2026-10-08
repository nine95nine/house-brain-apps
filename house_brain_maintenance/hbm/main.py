"""Entrypoint: options, poll loop, rate limit, ledger, reporting (Deployer lineage)."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import signal
import sys
import threading
import time
from dataclasses import dataclass

from . import VERSION, approval, ghauth, ghpage, net
from .github import REQUESTS_DIR, RE_REPO, GitHub
from .ha import RE_SLUG, RE_UPDATE_ENTITY, HAError, HomeAssistant, RE_SWITCH
from .jobs import DONE, FAILED, FAILED_MANUAL, REFUSED, Engine, Result, Settings
from .updates import ASK, AUTO_LOW_RISK, JOB as UPDATE_JOB, RESTORED, Policy, Updater
from .system import JOBS as SYSTEM_JOBS, ROLLED_BACK, SystemPolicy, SystemUpdater
from .entities import JOBS as ENTITY_JOBS, EntityPolicy, EntityUpdater
from .migrate import JOB as MIGRATE_JOB, MigratePolicy, Migrator
from .journal import Journal
from .recovery import HEARTBEAT_SECONDS, Recovery, RecoverySettings, run_forever
from .watch import Watcher, WatchPolicy
from .manifest import ASK_UPDATE_NOW, NO_APPROVAL_JOBS, RE_REQUEST_ID, Manifest, ManifestError, parse
from .web import INGRESS_PEER, ApprovalBoard, IngressServer

LOG = logging.getLogger("hbm")
RE_NOTIFY = re.compile(r"^mobile_app_[a-z0-9_]{1,80}$")
RE_USERNAME = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
RE_BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")
RE_LIVENESS_URL = re.compile(r"^https://[a-z0-9-]{1,63}\.[a-z0-9-]{1,63}\.workers\.dev/v1/ping$")
RE_LIVENESS_KEY = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
RE_RELAY_CREDENTIAL_URL = re.compile(r"^https://[a-z0-9-]{1,63}\.[a-z0-9-]{1,63}\.workers\.dev/v1/dispatch-credential$")
MAX_REQUESTS_PER_POLL = 10


class OptionsError(ValueError):
    pass


# Credential Autopilot: the GitHub App's fixed, minimal permissions (Metadata: read is automatic).
GITHUB_PERMISSIONS = {"contents": "read", "issues": "write"}


class OwnerId:
    """The owner's Home Assistant user id, looked up once (for the GitHub connection page)."""

    def __init__(self, resolve) -> None:
        self._resolve = resolve
        self._value = ""

    def __call__(self) -> str:
        if not self._value:
            self._value = self._resolve()
        return self._value


@dataclass
class Options:
    github_repo: str
    github_token: str
    requests_branch: str
    notify_service: str
    owner_username: str
    dry_run: bool
    poll_seconds: int
    max_approval_requests_per_day: int
    approval_timeout_minutes: int
    require_phone_unlock: bool
    scout_slug: str
    observer_slug: str
    clear_freeze_for: str
    update_mode: str = ASK
    update_check_minutes: int = 60
    auto_window_start_hour: int = 2
    auto_window_end_hour: int = 5
    auto_wait_days: int = 3
    health_check_minutes: int = 3
    report_issue: int = 0
    issue_checks: bool = True
    digest_hour: int = 8
    recovery_report: bool = True
    safety_notify_services: tuple[str, ...] = ()
    liveness_url: str = ""
    liveness_key: str = ""
    liveness_interval_minutes: int = 2
    extender_plug_entity: str = ""
    github_auth: str = "auto"         # Credential Autopilot (0.6.3): auto | github_app | pat
    retire_old_token: bool = True     # R2: retire the old hand-made token after 24 h of GitHub App sign-ins
    relay_credential_url: str = ""    # 0.6.3: the relay's public dispatch-key self-test view ("" = off)
    update_reask_hours: int = 6       # 0.6.5: an update ask with no answer is asked again after this (was 24 h)
    entity_updates: bool = True       # 0.7.0: HACS and device-firmware updates (owner 2026-10-08)
    firmware_wait_days: int = 30      # 0.7.0: firmware is asked only after it was offered this long
    update_hold: tuple[str, ...] = () # 0.7.0: update entities the owner holds (never asked, never installed)


def load_options(path: str) -> Options:
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)

    def s(key: str, pattern: re.Pattern) -> str:
        value = raw.get(key)
        if not isinstance(value, str) or not pattern.fullmatch(value):
            raise OptionsError(key)
        return value

    def i(key: str, lo: int, hi: int) -> int:
        value = raw.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
            raise OptionsError(key)
        return value

    # Credential Autopilot: the hand-made token is optional once a GitHub App is connected on the App's page.
    token = raw.get("github_token")
    if token in (None, ""):
        token = ""
    elif not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_]{20,255}", token):
        raise OptionsError("github_token")
    net.register_secret(token)
    github_auth = raw.get("github_auth", ghauth.AUTO)
    if github_auth not in ghauth.MODES:
        raise OptionsError("github_auth")
    retire_old_token = raw.get("retire_old_token", True)
    if not isinstance(retire_old_token, bool):
        raise OptionsError("retire_old_token")
    for key in ("dry_run", "require_phone_unlock"):
        if not isinstance(raw.get(key), bool):
            raise OptionsError(key)
    clear = raw.get("clear_freeze_for", "")
    if clear in (None, ""):
        clear = ""
    elif not isinstance(clear, str) or not RE_REQUEST_ID.fullmatch(clear):
        raise OptionsError("clear_freeze_for")
    return Options(
        github_repo=s("github_repo", RE_REPO),
        github_token=token,
        github_auth=github_auth,
        retire_old_token=retire_old_token,
        relay_credential_url=_relay_url(raw.get("relay_credential_url", ""), raw.get("liveness_url") or ""),
        requests_branch=s("requests_branch", RE_BRANCH),
        notify_service=s("notify_service", RE_NOTIFY),
        owner_username=s("owner_username", RE_USERNAME),
        dry_run=raw["dry_run"],
        poll_seconds=i("poll_seconds", 60, 3600),
        max_approval_requests_per_day=i("max_approval_requests_per_day", 1, 12),
        approval_timeout_minutes=i("approval_timeout_minutes", 2, 60),
        require_phone_unlock=raw["require_phone_unlock"],
        scout_slug=s("scout_slug", RE_SLUG),
        observer_slug=s("observer_slug", RE_SLUG),
        clear_freeze_for=clear,
        update_mode=raw.get("update_mode", ASK) if raw.get("update_mode", ASK) in (ASK, AUTO_LOW_RISK)
        else _bad("update_mode"),
        update_check_minutes=i("update_check_minutes", 15, 1440) if "update_check_minutes" in raw else 60,
        auto_window_start_hour=i("auto_window_start_hour", 0, 23) if "auto_window_start_hour" in raw else 2,
        auto_window_end_hour=i("auto_window_end_hour", 0, 23) if "auto_window_end_hour" in raw else 5,
        auto_wait_days=i("auto_wait_days", 0, 30) if "auto_wait_days" in raw else 3,
        health_check_minutes=i("health_check_minutes", 1, 30) if "health_check_minutes" in raw else 3,
        report_issue=i("report_issue", 0, 10**7) if "report_issue" in raw else 0,
        issue_checks=raw.get("issue_checks", True) if isinstance(raw.get("issue_checks", True), bool)
        else _bad("issue_checks"),
        digest_hour=i("digest_hour", 0, 23) if "digest_hour" in raw else 8,
        recovery_report=raw.get("recovery_report", True) if isinstance(raw.get("recovery_report", True), bool)
        else _bad("recovery_report"),
        safety_notify_services=_safety(raw.get("safety_notify_services", [])),
        **_liveness(raw),
        liveness_interval_minutes=i("liveness_interval_minutes", 2, 30) if "liveness_interval_minutes" in raw else 2,
        extender_plug_entity=_plug(raw.get("extender_plug_entity", "")),
        update_reask_hours=i("update_reask_hours", 1, 48) if "update_reask_hours" in raw else 6,
        entity_updates=raw.get("entity_updates", True) if isinstance(raw.get("entity_updates", True), bool)
        else _bad("entity_updates"),
        firmware_wait_days=i("firmware_wait_days", 0, 90) if "firmware_wait_days" in raw else 30,
        update_hold=_hold(raw.get("update_hold", [])),
    )


RELAY_WORKER = "house-brain-live-read-relay"


def _relay_url(value, liveness_url: str = "") -> str:
    """0.6.3: "" = derive from liveness_url (same Cloudflare account subdomain; nothing for the owner to type; the
    public store may not carry the concrete host); "off" = disabled; else an explicit relay URL."""
    if value == "off":
        return ""
    if value in (None, ""):
        if isinstance(liveness_url, str) and RE_LIVENESS_URL.fullmatch(liveness_url):
            account = liveness_url.split("/")[2].split(".")[1]
            return f"https://{RELAY_WORKER}.{account}.workers.dev/v1/dispatch-credential"
        return ""
    if not isinstance(value, str) or not RE_RELAY_CREDENTIAL_URL.fullmatch(value):
        raise OptionsError("relay_credential_url")
    return value


def _safety(value) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, list) or len(value) > 4 or not all(
            isinstance(v, str) and RE_NOTIFY.fullmatch(v) for v in value) or len(set(value)) != len(value):
        raise OptionsError("safety_notify_services")
    return tuple(value)


def _liveness(raw: dict) -> dict:
    url, key = raw.get("liveness_url") or "", raw.get("liveness_key") or ""
    if not isinstance(url, str) or not isinstance(key, str):
        raise OptionsError("liveness_url")
    if bool(url) != bool(key):
        raise OptionsError("liveness_url" if not url else "liveness_key")   # both or neither
    if url and not RE_LIVENESS_URL.fullmatch(url):
        raise OptionsError("liveness_url")
    if key and not RE_LIVENESS_KEY.fullmatch(key):
        raise OptionsError("liveness_key")
    net.register_secret(key)
    return {"liveness_url": url, "liveness_key": key}


def _hold(value) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if not isinstance(value, list) or len(value) > 50 or not all(
            isinstance(v, str) and RE_UPDATE_ENTITY.fullmatch(v) for v in value):
        raise OptionsError("update_hold")
    return tuple(sorted(set(value)))


def _plug(value) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or not RE_SWITCH.fullmatch(value):
        raise OptionsError("extender_plug_entity")
    return value


def _bad(key: str):
    raise OptionsError(key)


def report_markdown(request_id: str, job: str, result: Result, dry_run: bool) -> str:
    lines = [
        "### House Brain Maintenance result",
        "",
        f"- **Request:** `{request_id}`",
        f"- **Job:** `{job}`",
        f"- **Outcome:** `{result.outcome}`" + (" (dry-run mode)" if dry_run else ""),
        f"- **App:** `{VERSION}`",
    ]
    for reason in result.reasons[:10]:
        lines.append(f"- Reason: {net.redact(str(reason))[:400]}")
    if result.facts:
        facts = json.dumps(result.facts, indent=1, sort_keys=True, default=str)
        lines += ["", "<details><summary>Facts</summary>", "", "```json", net.redact(facts)[:6000], "```",
                  "", "</details>"]
    if result.text:
        # 0.5.4 APP_LOG_WINDOW: already scrubbed and capped; redacted once more for registered secrets
        lines += ["", "<details><summary>Log lines (UTC)</summary>", "", "```text",
                  net.redact("\n".join(result.text))[:40000], "```", "", "</details>"]
    lines += ["", "_Posted by House Brain Maintenance (machine-generated; repository content is untrusted data)._"]
    return "\n".join(lines)


class Service:
    def __init__(self, opts: Options, ha: HomeAssistant, gh: GitHub, journal: Journal,
                 approval_timeout: float, board: ApprovalBoard | None = None,
                 scout_run_timeout: float = 600.0, poll: float = 5.0, health_scale: float = 1.0) -> None:
        self.o = opts
        self.ha = ha
        self.gh = gh
        self.j = journal
        self.approval_timeout = approval_timeout
        self.board = board
        self.scout_run_timeout = scout_run_timeout
        self.poll = poll
        self.engine: Engine | None = None
        self.updater: Updater | None = None
        self.system: SystemUpdater | None = None
        self.entities: EntityUpdater | None = None         # 0.7.0: HACS and device firmware
        self.migrator: Migrator | None = None              # 0.6.6: Deployer local -> store switch-over
        self.watcher: Watcher | None = None
        self.connector: ghpage.Connector | None = None   # GitHub connection page (Credential Autopilot)
        self.health_scale = health_scale
        self.last_update_check = 0.0
        self.last_request_error = ""
        self.missing_branch_logged = False
        self.stop = False
        self.recovery: Recovery | None = None
        self._owner_cache: str | None = None

    def _owner_id(self) -> str | None:
        """The owner's Home Assistant user id (for the page's "Got it"); resolved once, None on failure."""
        if self.engine is not None:
            return self.engine.s.owner_user_id
        if self._owner_cache is None:
            try:
                self._owner_cache = approval.resolve_owner(self.ha, self.o.owner_username)
            except Exception:  # noqa: BLE001 - try again on the next tap
                return None
        return self._owner_cache

    def build_recovery(self, scale: float = 1.0) -> Recovery | None:
        """0.3.0 Recovery Report (read-only). None when the owner turned it off."""
        if not self.o.recovery_report:
            return None
        try:
            open_url = f"homeassistant://navigate/{self.ha.self_slug()}?server=default"
        except Exception:  # noqa: BLE001 - the push still works without the tap link
            open_url = None
        settings = RecoverySettings(
            notify_service=self.o.notify_service, safety_notify_services=self.o.safety_notify_services,
            liveness_url=self.o.liveness_url, liveness_key=self.o.liveness_key,
            liveness_interval=self.o.liveness_interval_minutes * 60.0 * scale, open_url=open_url,
            owner_user_id=self._owner_id, scale=scale)
        self.recovery = Recovery(self.ha, self.j, settings)
        return self.recovery

    def ensure_engine(self) -> Engine:
        if self.engine is None:
            owner_id = approval.resolve_owner(self.ha, self.o.owner_username)
            open_url = f"homeassistant://navigate/{self.ha.self_slug()}?server=default"
            settings = Settings(notify_service=self.o.notify_service, owner_user_id=owner_id,
                                dry_run=self.o.dry_run, require_auth=self.o.require_phone_unlock,
                                approval_timeout=self.approval_timeout, board=self.board, open_url=open_url,
                                scout_run_timeout=self.scout_run_timeout, poll=self.poll,
                                should_stop=lambda: self.stop)
            self.engine = Engine(self.ha, self.j, settings)
            health = self.o.health_check_minutes * 60.0 * self.health_scale
            policy = Policy(mode=self.o.update_mode,
                            window=(self.o.auto_window_start_hour, self.o.auto_window_end_hour),
                            health_seconds=health, grace_seconds=min(45.0, health / 4), poll=self.poll,
                            exclude=frozenset(), wait_days=float(self.o.auto_wait_days),
                            reask_hours=float(self.o.update_reask_hours))
            self.updater = Updater(self.ha, self.j, settings, policy)
            # 0.6.0 Core/OS update gate: never automatic (update_mode does not apply), times scale in test mode.
            scale = self.health_scale
            self.system = SystemUpdater(self.ha, self.j, settings, SystemPolicy(
                lead_seconds=3600.0 * scale, reask_hours=float(self.o.update_reask_hours), boot_seconds=900.0 * scale,
                settle_seconds=60.0 * scale, reboot_seconds=3600.0 * scale, poll=self.poll), self.report_text)
            # 0.7.0 HACS and device firmware: cards/themes may install by themselves (update_mode), integrations
            # and firmware always ask; firmware only after firmware_wait_days. Times scale in test mode.
            self.entities = EntityUpdater(self.ha, self.j, settings, EntityPolicy(
                mode=self.o.update_mode, window=(self.o.auto_window_start_hour, self.o.auto_window_end_hour),
                wait_days=float(self.o.auto_wait_days), firmware_wait_days=float(self.o.firmware_wait_days),
                reask_hours=float(self.o.update_reask_hours), health_seconds=health, boot_seconds=900.0 * scale,
                firmware_seconds=3600.0 * scale, poll=self.poll, hold=frozenset(self.o.update_hold),
                enabled=self.o.entity_updates),
                app_successes=lambda: int(self.j.load_doc("updates", {}).get("successes", 0)))
            self.migrator = Migrator(self.ha, self.j, settings, MigratePolicy(
                watch_seconds=1200.0 * scale, poll=self.poll, reask_hours=24.0))
            watch = WatchPolicy(digest_hour=self.o.digest_hour,
                                max_fix_asks_per_day=self.o.max_approval_requests_per_day,
                                verify_seconds=60.0 * self.health_scale, poll=self.poll,
                                power_off_seconds=10.0 * self.health_scale,
                                power_verify_seconds=600.0 * self.health_scale)
            self.watcher = Watcher(self.ha, self.j, settings, watch, self.report_text)
            self.watcher.credentials = self.gh.auth.summary if hasattr(self.gh, "auth") else None
            self.watcher.relay_url = self.o.relay_credential_url
        return self.engine

    def key(self, request_id: str) -> str:
        return f"{request_id}#dry" if self.o.dry_run else request_id

    def status(self, state: str, attributes: dict) -> None:
        self.ha.publish_status(state, attributes)

    def finish(self, request_id: str, job: str, digest: str, issue: int | None, result: Result,
               notify: bool = True) -> None:
        self.j.record(self.key(request_id), digest, result.outcome)
        self.j.audit(request_id, "OUTCOME", job=job, outcome=result.outcome, reasons=result.reasons[:5])
        self.status(result.outcome, {"request_id": request_id, "job": job, "version": VERSION,
                                     "dry_run": self.o.dry_run})
        if notify:
            approval.inform(self.ha, self.o.notify_service, f"Maintenance: {result.outcome}",
                            f"{request_id} ({job})\n" + "\n".join(str(r)[:120] for r in result.reasons[:3]))
        if issue:
            try:
                self.gh.comment(issue, report_markdown(request_id, job, result, self.o.dry_run))
            except net.NetError as err:
                self.j.audit(request_id, "REPORT_FAILED", error=str(err)[:200])
        if result.outcome == FAILED_MANUAL:
            self.j.freeze(request_id)
        elif result.outcome in (DONE, RESTORED, ROLLED_BACK) and self.j.frozen_by() == request_id:
            self.j.clear_freeze()
            self.j.audit(request_id, "FREEZE_CLEARED_BY_RECOVERY")

    def frozen(self) -> str | None:
        rid = self.j.frozen_by()
        if rid and self.o.clear_freeze_for == rid and not self.j.load_txn():
            self.j.clear_freeze()
            self.j.audit(rid, "FREEZE_CLEARED_BY_OWNER")
            return None
        return rid

    def poll_once(self) -> None:
        txn = self.j.load_txn()
        if txn:
            self.ensure_engine()
            if txn.get("kind") == "update":
                out = self.updater.recover(txn)
                result = Result(out.result_outcome, out.reasons, out.facts)
                self.finish(out.request_id, UPDATE_JOB, f"update:{txn.get('slug')}@{txn.get('to')}",
                            self.o.report_issue or None, result)
                return
            if txn.get("kind") == "migrate":
                out = self.migrator.recover(txn)
                self.finish(out.request_id, MIGRATE_JOB, f"migrate:{txn.get('new')}", self.o.report_issue or None,
                            Result(out.result_outcome, out.reasons, out.facts))
                return
            if txn.get("kind") == "entity":
                out = self.entities.recover(txn)
                out.facts = {"entity_id": txn.get("entity_id"), "kind": txn.get("entity_kind"), "from": txn.get("from"),
                             "to": txn.get("to"), **out.facts}
                self.finish_entity(out, str(txn.get("entity_kind")))
                return
            if txn.get("kind") == "system":
                out = self.system.recover(txn)
                if out is None:                       # OS update: waiting for the host to reboot
                    self.status("SYSTEM_UPDATING", {"version": VERSION, "kind": txn.get("system"),
                                                    "to": txn.get("to"), "request_id": txn.get("request_id")})
                    return
                self.finish_system(str(txn.get("system")), str(txn.get("to")), out)
                return
            rid, job, result = self.engine.recover(txn)
            previous = self.j.ledger().get(self.key(rid), {}).get("digest")
            self.finish(rid, job, str(txn.get("digest") or previous or "recovered"), txn.get("issue"), result)
            return
        if self.frozen():
            return
        try:
            handled = self.poll_requests()
        except net.NetError as err:
            # A GitHub fault (network, rate limit, token) must never stop update checks.
            handled = False
            message = net.redact(f"NetError: {err}")[:300]
            if message != self.last_request_error:
                LOG.warning("request check failed (update checks continue): %s", message)
            self.last_request_error = message
        if handled:
            return
        self.poll_updates()
        self.poll_issues()

    def report_text(self, text: str) -> None:
        if self.o.report_issue:
            self.gh.comment(self.o.report_issue, text)

    def poll_issues(self) -> None:
        if not self.o.issue_checks:
            return
        self.ensure_engine()
        summary = self.watcher.check()
        self.status("WATCHING", {"version": VERSION, "dry_run": self.o.dry_run, **summary, **self._ledger_attrs()})

    def _ledger_attrs(self) -> dict:
        """0.5.2: restart ledger counts and MTBF from the Recovery Report (empty when it is off)."""
        if self.recovery is None:
            return {}
        try:
            return self.recovery.status_attrs()
        except Exception:  # noqa: BLE001 - visibility only
            return {}

    def poll_updates(self) -> None:
        if time.monotonic() - self.last_update_check < self.o.update_check_minutes * 60 and self.last_update_check:
            return
        self.last_update_check = time.monotonic()
        self.ensure_engine()
        out = self.updater.cycle()
        if out is None:
            # 0.6.6: the Deployer switch-over, only when no App update ran in this cycle.
            mig = self.migrator.cycle() if self.migrator is not None else None
            if mig is not None:
                self.finish(mig.request_id, MIGRATE_JOB, f"migrate:{mig.facts.get('new') or mig.facts.get('old')}",
                            self.o.report_issue or None, Result(mig.result_outcome, mig.reasons, mig.facts))
                return
            # 0.6.0: Core/OS only when no App update ran in this cycle (never in the same run).
            sys_out = self.system.cycle()
            if sys_out is not None:
                self.finish_system(str(sys_out.facts.get("kind", "")).lower(), str(sys_out.facts.get("to")), sys_out)
                return
            if (self.j.load_txn() or {}).get("kind") == "system":
                return                                  # OS update started; the result follows after the reboot
            # 0.7.0: HACS and device firmware, only when nothing else ran in this cycle.
            ent_out = self.entities.cycle() if self.entities is not None else None
            if ent_out is not None:
                self.finish_entity(ent_out, str(ent_out.facts.get("kind", "")))
                return
            self.status("IDLE", {"version": VERSION, "dry_run": self.o.dry_run, "pending_updates": 0,
                                 **self._ledger_attrs()})
            return
        result = Result(out.result_outcome, out.reasons, out.facts)
        self.finish(out.request_id, UPDATE_JOB, f"update:{out.facts.get('slug')}@{out.facts.get('to')}",
                    self.o.report_issue or None, result)

    def finish_entity(self, out, kind: str) -> None:
        self.finish(out.request_id, ENTITY_JOBS.get(kind, "UPDATE_HACS"),
                    f"entity:{out.facts.get('entity_id')}@{out.facts.get('to')}", self.o.report_issue or None,
                    Result(out.result_outcome, out.reasons, out.facts))

    def finish_system(self, kind: str, to: str, out) -> None:
        self.finish(out.request_id, SYSTEM_JOBS.get(kind, "UPDATE_SYSTEM"), f"system:{kind}@{to}",
                    self.o.report_issue or None, Result(out.result_outcome, out.reasons, out.facts))

    def poll_requests(self) -> bool:
        """Handle at most one new manifest request. True when one was handled."""
        try:
            head = self.gh.branch_head(self.o.requests_branch)
        except net.NetError as err:
            if err.status != 404:
                raise
            # No request branch yet means no requests; say so once, not every poll.
            if not self.missing_branch_logged:
                LOG.info("request branch %s not found: no requests", self.o.requests_branch)
                self.missing_branch_logged = True
            return False
        self.missing_branch_logged = False
        self.last_request_error = ""
        ledger = self.j.ledger()
        ids = [r for r in self.gh.list_request_ids(head) if RE_REQUEST_ID.fullmatch(r)]
        # Skip handled ids first, then cap: an old backlog can never hide new requests.
        unseen = [r for r in ids if self.key(r) not in ledger]
        for request_id in unseen[:MAX_REQUESTS_PER_POLL]:
            try:
                raw = self.gh.read_file(f"{REQUESTS_DIR}/{request_id}/manifest.json", head)
            except net.NetError:
                continue
            digest = hashlib.sha256(raw).hexdigest()
            try:
                manifest = parse(raw)
            except ManifestError as err:
                # No phone push for malformed manifests (anyone who can push could flood them).
                self.finish(request_id, "?", digest, None, Result(REFUSED, [f"manifest: {err.code}"]),
                            notify=False)
                continue
            if manifest.request_id != request_id:
                self.finish(request_id, manifest.job, digest, manifest.tracking_issue,
                            Result(REFUSED, ["request_id does not match its directory"]))
                continue
            if (manifest.job not in NO_APPROVAL_JOBS
                    and self.j.approvals_requested_since(86400) >= self.o.max_approval_requests_per_day):
                self.j.audit(request_id, "RATE_LIMITED")
                return True
            if manifest.job == ASK_UPDATE_NOW:
                self.ask_update_now(manifest)
                return True
            result = self.ensure_engine().run(manifest)
            self.finish(request_id, manifest.job, manifest.digest, manifest.tracking_issue, result)
            return True  # at most one request per poll
        return False

    def ask_update_now(self, m: Manifest) -> None:
        """0.6.5 ASK_UPDATE_NOW: the same ask -> backup -> update -> health -> restore path as the update check,
        without the re-ask wait. The request is marked ASKING first, so a restart mid-update never asks twice
        (the update's own result then follows under its update request id, as for any update)."""
        self.ensure_engine()
        issue = m.tracking_issue or self.o.report_issue or None
        self.j.record(self.key(m.request_id), m.digest, "ASKING")
        try:
            if m.update_kind == "app":
                out = self.updater.ask_now(str(m.update_slug), str(m.update_version))
            else:
                out = self.system.ask_now(str(m.update_kind), str(m.update_version))
        except (HAError, net.NetError) as err:
            reason = net.redact(str(err))[:200]
            result = (Result(FAILED_MANUAL, [f"read-back failed ({reason}); it is re-checked on the next poll"])
                      if self.j.load_txn() else Result(FAILED, [f"preflight: {reason}; nothing asked or changed"]))
            self.finish(m.request_id, m.job, m.digest, issue, result)
            return
        if out is None:                              # OS update started: the Pi reboots, the result follows
            self.finish(m.request_id, m.job, m.digest, issue, Result(DONE, [
                "approved; the OS update started (the Pi reboots); its result is posted when the Pi is back"],
                {"kind": "OS", "to": m.update_version, "automatic": False}))
            return
        self.finish(m.request_id, m.job, m.digest, issue,
                    Result(out.result_outcome, out.reasons, {**out.facts, "update_request": out.request_id}))


HANDOFF_PORT = 8097                 # R2 (owner 2026-10-07): the GitHub hand-off address; config.yaml "ports"


def handoff_port() -> int:
    if os.environ.get("HBM_TEST_MODE") == "1":
        return int(os.environ.get("HBM_HANDOFF_PORT", "0"))
    return HANDOFF_PORT


def build_service(options_path: str, data_dir: str) -> Service:
    opts = load_options(options_path)
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        raise OptionsError("SUPERVISOR_TOKEN missing")
    sup = os.environ.get("HBM_SUPERVISOR_URL", "http://supervisor")
    ws = os.environ.get("HBM_CORE_WS_URL", "ws://supervisor/core/websocket")
    api = os.environ.get("HBM_GITHUB_API", "https://api.github.com")
    ha = HomeAssistant(sup, token, ws, opts.scout_slug, opts.observer_slug,
                       power_cycle_entity=opts.extender_plug_entity)
    store = ghauth.KeyStore(data_dir)
    provider = ghauth.TokenProvider(store, opts.github_repo, GITHUB_PERMISSIONS, api, f"house-brain-maintenance/{VERSION}")
    auth = ghauth.Auth(opts.github_auth, opts.github_token, provider, retire=opts.retire_old_token, api=api,
                       user_agent=f"house-brain-maintenance/{VERSION}")
    gh = GitHub(opts.github_repo, opts.github_token, api, auth=auth)
    approval_timeout = opts.approval_timeout_minutes * 60.0
    run_timeout, poll, health_scale = 600.0, 5.0, 1.0
    if os.environ.get("HBM_TEST_MODE") == "1":
        scale = float(os.environ.get("HBM_TEST_TIME_SCALE", "1"))
        approval_timeout *= scale
        run_timeout *= scale
        health_scale = scale
        poll = max(0.05, poll * scale)
    service = Service(opts, ha, gh, Journal(data_dir), approval_timeout, board=ApprovalBoard(),
                      scout_run_timeout=run_timeout, poll=poll, health_scale=health_scale)
    service.connector = ghpage.Connector(
        app_title="House Brain Maintenance", bot_name="House Brain Maintenance Bot",
        bot_description="House Brain Maintenance App on the owner's Home Assistant: reads maint/requests and "
                        "comments results. Created by the App's Connect button.",
        repo=opts.github_repo, permissions=GITHUB_PERMISSIONS, api=api,
        user_agent=f"house-brain-maintenance/{VERSION}", store=store, auth=auth,
        handoff_port=handoff_port(),
        owner_id=OwnerId(lambda: approval.resolve_owner(ha, opts.owner_username)))
    return service


def start_recovery(service: Service) -> threading.Thread | None:
    scale = float(os.environ.get("HBM_TEST_TIME_SCALE", "1")) if os.environ.get("HBM_TEST_MODE") == "1" else 1.0
    rec = service.build_recovery(scale)
    if rec is None:
        return None
    rec.start()
    thread = threading.Thread(target=run_forever, args=(rec, lambda: service.stop, HEARTBEAT_SECONDS * scale),
                              name="recovery", daemon=True)
    thread.start()
    return thread


def start_ingress(service: Service) -> IngressServer | None:
    if service.board is None:
        return None
    port, peer = 8099, INGRESS_PEER
    if os.environ.get("HBM_TEST_MODE") == "1":
        port = int(os.environ.get("HBM_INGRESS_PORT", "8099"))
        peer = os.environ.get("HBM_INGRESS_PEER", INGRESS_PEER)
    server = IngressServer(service.board, port=port, allowed_peer=peer, recovery=service.recovery,
                           connector=getattr(service, "connector", None))
    server.start()
    return server


def poll_cycle(service: Service) -> None:
    """One loop step: R2 old-token retire (no token in the log, ever), then the poll."""
    if service.gh.auth.maybe_retire() == "RETIRED":
        LOG.info("the old hand-made GitHub token was retired on GitHub (the GitHub App signs in now)")
    service.poll_once()


def main() -> int:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(net.RedactingFilter())
    logging.basicConfig(level=logging.INFO, handlers=[handler], format="%(asctime)s %(levelname)s %(message)s")
    try:
        service = build_service(os.environ.get("HBM_OPTIONS", "/data/options.json"),
                                os.environ.get("HBM_DATA", "/data"))
    except (OptionsError, ValueError, OSError) as err:
        LOG.error("configuration refused: %s", err)
        return 2
    rec_thread = None
    try:
        rec_thread = start_recovery(service)
    except Exception as err:  # noqa: BLE001 - the rest of the App must run without the report
        LOG.warning("recovery report unavailable: %s", net.redact(f"{type(err).__name__}: {err}")[:200])
    try:
        start_ingress(service)
    except OSError as err:
        LOG.warning("approval page unavailable: %s", err)
    LOG.info("House Brain Maintenance %s started (dry_run=%s)", VERSION, service.o.dry_run)
    service.status("STARTED", {"version": VERSION, "dry_run": service.o.dry_run})

    def _term(signum, frame):  # noqa: ARG001
        service.stop = True

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    once = os.environ.get("HBM_ONCE") == "1"
    while not service.stop:
        try:
            poll_cycle(service)
        except Exception as err:  # noqa: BLE001 - the loop must survive transient faults
            message = net.redact(f"{type(err).__name__}: {err}")[:300]
            LOG.warning("poll failed: %s", message)
            service.status("ERROR", {"version": VERSION, "error": message})
        if once:
            break
        deadline = time.monotonic() + service.o.poll_seconds
        while not service.stop and time.monotonic() < deadline:
            time.sleep(1)
    service.stop = True
    if rec_thread is not None:
        rec_thread.join(timeout=10)   # writes the clean-stop heartbeat
    service.ha.close_shared_ws()      # 0.6.1: the one shared Core socket
    LOG.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
