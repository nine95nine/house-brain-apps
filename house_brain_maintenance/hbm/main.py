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
from .scoutmove import JOB as SCOUTMOVE_JOB, ScoutMove
from .observermove import JOB as OBSMOVE_JOB, TXN as OBSMOVE_TXN, ObserverMove, ObserverMovePolicy
from .inspectormove import JOB as INSMOVE_JOB, TXN as INSMOVE_TXN, InspectorMove
from .journal import Journal
from .recovery import HEARTBEAT_SECONDS, Recovery, RecoverySettings, run_forever
from .watch import Watcher, WatchPolicy
from .manifest import (ASK_UPDATE_NOW, ENERGY_AI_SETTINGS, HOLD_UPDATE, NO_APPROVAL_JOBS, RELEASE_UPDATE,
                       RE_REQUEST_ID, ROTATE_KEYS_NOW, STAGE_LOCAL_APP_SOURCE, Manifest, ManifestError, parse)
from .energyai import PENDING_DOC as ENERGY_PENDING_DOC, EnergyDelivery
from .upgradeinfo import UpgradeInfo
from .pifirmware import JOB as PIFW_JOB, FirmwarePolicy, PiFirmware
from .cfrelay import RE_CONTROL_URL, CfRelay, control_url_from_broker, safe_cycle as cf_safe_cycle
from .keys import FAILED_MANUAL as KEY_STUCK, JOB as KEY_JOB, SETUP_JOB as KEY_SETUP_JOB, KeyPolicy, KeyRotator
from .keys import APP_NAMES as KEY_APP_NAMES, ROLES as KEY_ROLES
from .leak import INVENTORY_PATH, LeakResponse
from .stage import PENDING_DOC as STAGE_PENDING_DOC, StagePolicy, Stager
from .holds import Holds
from .aireview import AiReviews, item_app
from .inbox import Inbox
from . import system as system_mod, entities as entities_mod, updates as updates_mod
from .updatequeue import UpdateQueue, rows_from
from .afterupdate import JOB as AFTER_JOB, AfterPolicy, AfterUpdate, after_request_id, loaded_snapshot
from .afterreport import AfterReports, item_of
from .problems import STALL_KEY
from .web import INGRESS_PEER, MAX_WAITING, ApprovalBoard, IngressServer, ReaskQueue
from .writer import SharedWriter

LOG = logging.getLogger("hbm")
NEEDS_REVIEW_SCHEMA = "house_brain_needs_review.v1"      # 0.9.26: the count line on every post
RE_NOTIFY = re.compile(r"^mobile_app_[a-z0-9_]{1,80}$")
RE_USERNAME = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
RE_BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")
RE_LIVENESS_URL = re.compile(r"^https://[a-z0-9-]{1,63}\.[a-z0-9-]{1,63}\.workers\.dev/v1/ping$")
RE_LIVENESS_KEY = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
RE_RELAY_CREDENTIAL_URL = re.compile(r"^https://[a-z0-9-]{1,63}\.[a-z0-9-]{1,63}\.workers\.dev/v1/dispatch-credential$")
MAX_REQUESTS_PER_POLL = 10
PAGE_JOB = "ASK_UPDATE_NOW"          # 0.7.2: "Ask me again" on the page runs exactly this job's path


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
    quiet_hours: str = "22-7"                   # 0.9.18: warning pushes wait until the morning ("" = off)
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
    hold_core_dot_zero: bool = True   # 0.7.3: every Core YYYY.M.0 is held until a later patch is offered
    after_update_minutes: int = 10    # 0.7.3: after-update check delay (0 = off)
    undo_hours: int = 24              # 0.7.3: how long the after-update check offers Undo
    key_rotation: bool = True         # 0.8.0: automatic Broker key changes (owner K1-K4 2026-10-11)
    key_rotate_days: int = 90         # 0.8.0: each Broker key is changed this often (one App per night)
    keep_old_deployer: bool = True    # 0.8.1: owner 2026-10-11 "Keep old Deployer for now" (no removal ask)
    upgrade_checks: bool = True       # 0.8.4: known issues + release notes for your integrations before Core updates
    backup_report: bool = True        # 0.8.5: monthly backup report (findings about backups are always on)
    pi_health: bool = True            # 0.8.6: power/heat, disk errors, App outliers, weekly health score
    pi_firmware_updates: bool = True  # 0.8.6: Pi bootloader firmware updates in the approval flow (never automatic)
    housekeeping: bool = True         # 0.8.7: failing automations, missing entities, error flood, database size
    ai_review: bool = True            # 0.8.9: an AI review on the tracking issue before any update ask/auto install
    cf_control_issue: int = 454       # 0.8.2: Cloudflare control approvals to the phone (0 = off; owner 2026-10-11)
    cf_control_url: str = ""          # 0.8.2: "" = the control on the Broker's own Cloudflare account


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
        quiet_hours=_quiet(raw.get("quiet_hours", "22-7")),
        **_liveness(raw),
        liveness_interval_minutes=i("liveness_interval_minutes", 2, 30) if "liveness_interval_minutes" in raw else 2,
        extender_plug_entity=_plug(raw.get("extender_plug_entity", "")),
        update_reask_hours=i("update_reask_hours", 1, 48) if "update_reask_hours" in raw else 6,
        entity_updates=raw.get("entity_updates", True) if isinstance(raw.get("entity_updates", True), bool)
        else _bad("entity_updates"),
        firmware_wait_days=i("firmware_wait_days", 0, 90) if "firmware_wait_days" in raw else 30,
        update_hold=_hold(raw.get("update_hold", [])),
        hold_core_dot_zero=raw.get("hold_core_dot_zero", True)
        if isinstance(raw.get("hold_core_dot_zero", True), bool) else _bad("hold_core_dot_zero"),
        after_update_minutes=i("after_update_minutes", 0, 120) if "after_update_minutes" in raw else 10,
        undo_hours=i("undo_hours", 1, 72) if "undo_hours" in raw else 24,
        key_rotation=raw.get("key_rotation", True) if isinstance(raw.get("key_rotation", True), bool)
        else _bad("key_rotation"),
        key_rotate_days=i("key_rotate_days", 30, 365) if "key_rotate_days" in raw else 90,
        keep_old_deployer=raw.get("keep_old_deployer", True) if isinstance(raw.get("keep_old_deployer", True), bool)
        else _bad("keep_old_deployer"),
        cf_control_issue=i("cf_control_issue", 0, 10**7) if "cf_control_issue" in raw else 454,
        upgrade_checks=raw.get("upgrade_checks", True) if isinstance(raw.get("upgrade_checks", True), bool)
        else _bad("upgrade_checks"),
        backup_report=raw.get("backup_report", True) if isinstance(raw.get("backup_report", True), bool)
        else _bad("backup_report"),
        pi_health=raw.get("pi_health", True) if isinstance(raw.get("pi_health", True), bool) else _bad("pi_health"),
        pi_firmware_updates=raw.get("pi_firmware_updates", True)
        if isinstance(raw.get("pi_firmware_updates", True), bool) else _bad("pi_firmware_updates"),
        housekeeping=raw.get("housekeeping", True) if isinstance(raw.get("housekeeping", True), bool)
        else _bad("housekeeping"),
        ai_review=raw.get("ai_review", True) if isinstance(raw.get("ai_review", True), bool) else _bad("ai_review"),
        cf_control_url=_cf_url(raw.get("cf_control_url")),
    )


def _cf_url(value) -> str:
    """0.8.2: the Cloudflare control address the phone button opens (empty or missing = worked out from the
    Broker address the Scout already uses: the same Cloudflare account)."""
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or not RE_CONTROL_URL.fullmatch(value):
        raise OptionsError("cf_control_url")
    return value


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


def _quiet(value) -> str:
    """0.9.18: "22-7" (start-end hour, local time), "" or "off" = no quiet hours."""
    from .messages import parse_quiet
    if not isinstance(value, str):
        _bad("quiet_hours")
    try:
        parse_quiet(value)
    except ValueError:
        _bad("quiet_hours")
    return value.strip().lower()


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
        self.step_errors: dict[str, str] = {}           # 0.9.22: shared with the Watcher (a failing step = a problem)
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
        self.holds: Holds | None = None                     # 0.7.3: hold list
        self.after: AfterUpdate | None = None               # 0.7.3: after-update check
        self.stager: Stager | None = None                   # 0.7.4: STAGE_LOCAL_APP_SOURCE
        self.stage_policy = StagePolicy()                   # 0.7.4: /addons, /share (test mode: temp folders)
        self.keys: KeyRotator | None = None                 # 0.8.0: automatic Broker keys
        self.cf_relay: CfRelay | None = None                # 0.8.2: Cloudflare approvals to the phone
        self.pifirmware: PiFirmware | None = None          # 0.8.6: Pi firmware updates
        self.ai: AiReviews | None = None                   # 0.8.9: AI reviews read from the tracking issue
        self.update_queue: UpdateQueue | None = None       # 0.8.9: the "updates waiting" list
        self.rate_noted: set[str] = set()                  # 0.8.9: requests waiting for the ask limit (audited once)
        self.key_broker_base: str | None = None             # 0.8.0: test mode only (a local Broker)
        self.migrator: Migrator | None = None              # 0.6.6: Deployer local -> store switch-over
        self.writer = SharedWriter()                       # 0.9.1: shared writer lock with the Deployer (never created)
        self.scoutmove: ScoutMove | None = None            # 0.9.3: Inventory Scout local -> store switch
        self.obsmove: ObserverMove | None = None           # 0.9.8: Engineering Observer local -> store switch
        self.insmove: InspectorMove | None = None          # 0.9.9: Engineering Inspector local -> store switch
        self._writer_reported = False                      # 0.9.4: lock identity posted on #454 once per start
        self.watcher: Watcher | None = None
        self.after_reports: AfterReports | None = None   # 0.9.26: this App's own after-update reports
        self.connector: ghpage.Connector | None = None   # GitHub connection page (Credential Autopilot)
        self.health_scale = health_scale
        self.last_update_check = 0.0
        self.last_request_error = ""
        self.missing_branch_logged = False
        self.stop = False
        self.recovery: Recovery | None = None
        self._owner_cache: str | None = None
        self.reask = ReaskQueue(self._owner_id)          # 0.7.2: the page's "Ask me again" (owner only)
        # 0.9.13 "Suspected leak?": one owner tap queues every Broker key change; the rest is a checklist.
        self.leak = LeakResponse(self._owner_id, KEY_ROLES, KEY_APP_NAMES, self.o.github_repo,
                                 self.o.report_issue or None)
        self.leak.keys_summary = lambda: self.keys.summary() if self.keys is not None else None
        # 0.9.0 Approve from the page: one inbox for every ask; a page decision wakes the poll loop at once.
        self.wake = threading.Event()
        self.inbox = Inbox(self.j.dir, self._owner_id, wake=self.wake.set)
        approval.INBOX = self.inbox
        # 0.9.18 Messages: every push kept (page + bell), own tags, quiet hours, "Handled" (owner pop-ups 2026-10-18)
        from .messages import Messages, Messenger, parse_quiet
        self.messages = Messages(self.j)
        self.messages.owner = self._owner_id
        self.messages.review = self._problem_review                          # 0.9.23: the page's AI block
        self.messages.fixes = self._page_fixes                               # 0.9.23: the page's "Fix it"
        self.messenger = Messenger(self.ha, self.messages, self.o.notify_service, open_url=self._open_url,
                                   quiet=parse_quiet(self.o.quiet_hours))

    def _problem_review(self, key: str) -> dict | None:
        """0.9.23: the AI review of a problem as the Messages page shows it (None without one)."""
        ai = getattr(self.watcher, "problem_ai", None) if self.watcher is not None else None
        return ai.detail(key) if ai is not None else None

    def _page_fixes(self) -> dict:
        """0.9.23: fix request id (``fix-<problem key>``) -> its "Needs your decision" row (inbox key, label, AI
        verdict, decision). Both an offered fix and a missed phone ask of one count."""
        if self.inbox is None:
            return {}
        return {i["key"].split("|", 1)[0][4:]: i for i in self.inbox.items()
                if i.get("stage") == "FIX" and i.get("resumable") and str(i.get("key", "")).startswith("FIX:")}

    def _open_url(self) -> str:
        """The tap link of every push: the Maintenance page (its Messages list)."""
        return f"homeassistant://navigate/{self.ha.self_slug()}?server=default"

    def _update_note(self, title: str, message: str) -> None:
        """0.8.9 "updates waiting" line, 0.9.18: kept in the Messages list with its own tag."""
        msg = self.messages.add("update", title, happened=message, done="Nothing is installed yet.",
                                todo=("Nothing yet: your phone asks when an update is ready for your OK.",))
        self.messenger.send(msg)

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
            if self.o.upgrade_checks:                   # 0.8.4: Safer upgrades (read-only, cached)
                self.system.upgrade_info = UpgradeInfo(self.j)
            if self.o.pi_firmware_updates:              # 0.8.6: Pi firmware, asked like Core/OS, never automatic
                self.pifirmware = PiFirmware(self.ha, self.j, settings, FirmwarePolicy(
                    lead_seconds=3600.0 * scale, reask_hours=float(self.o.update_reask_hours),
                    reboot_seconds=3600.0 * scale), self.report_text)
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
                watch_seconds=1200.0 * scale, poll=self.poll, reask_hours=float(self.o.update_reask_hours),
                keep_old=self.o.keep_old_deployer))
            self.updater.writer = self.migrator.writer = self.writer     # 0.9.1: the shared writer lock
            self.scoutmove = ScoutMove(self.ha, self.j, settings, reask_hours=float(self.o.update_reask_hours))
            self.engine.writer = self.writer                             # 0.9.4: WRITER_PROBE
            if self.scoutmove.restore():                                 # 0.9.3: the recorded store Scout
                LOG.info("Inventory Scout: the store Scout %s is in use", self.ha.scout)
            self.obsmove = ObserverMove(self.ha, self.j, settings, ObserverMovePolicy(
                watch_seconds=600.0 * scale, poll=min(10.0, self.poll), reask_hours=float(self.o.update_reask_hours)))
            if self.obsmove.restore():                                   # 0.9.8: the recorded store Observer
                LOG.info("Engineering Observer: the store Observer %s is in use", self.ha.observer)
            self.insmove = InspectorMove(self.ha, self.j, settings, ObserverMovePolicy(
                watch_seconds=900.0 * scale, poll=min(10.0, self.poll), reask_hours=float(self.o.update_reask_hours)))
            if self.insmove.restore():                                   # 0.9.9: the recorded store Inspector
                LOG.info("Engineering Inspector: the store Inspector %s is in use", self.ha.inspector)
            watch = WatchPolicy(digest_hour=self.o.digest_hour,
                                max_fix_asks_per_day=self.o.max_approval_requests_per_day,
                                reask_hours=float(self.o.update_reask_hours),
                                verify_seconds=60.0 * self.health_scale, poll=self.poll,
                                power_off_seconds=10.0 * self.health_scale,
                                power_verify_seconds=600.0 * self.health_scale,
                                backup_report=self.o.backup_report, pi_health=self.o.pi_health,
                                housekeeping=self.o.housekeeping)
            self.watcher = Watcher(self.ha, self.j, settings, watch, self.report_text)
            self.watcher.step_errors = self.step_errors                   # 0.9.22
            if self.o.report_issue:                                        # 0.9.26: after-update reports
                self.after_reports = AfterReports(self.j, self.report_text)
                self.watcher.after_report = self.after_reports
            self.watcher.messenger = self.messenger                       # 0.9.18
            if self.o.report_issue and hasattr(self.gh, "issue_url"):     # 0.9.23: links instead of "see GitHub"
                self.watcher.issue_url = self.gh.issue_url(self.o.report_issue)
            if self.o.report_issue and self.o.ai_review:                  # 0.9.6: AI problem reviews
                from .problems import ProblemReviews
                self.watcher.problem_ai = ProblemReviews(self.gh, self.j, self.o.report_issue)
            self.watcher.credentials = self.gh.auth.summary if hasattr(self.gh, "auth") else None
            self.watcher.relay_url = self.o.relay_credential_url
            # 0.7.3: hold list (all update paths) and the after-update check (Core, OS and App updates).
            self.holds = Holds(self.j, dot_zero=self.o.hold_core_dot_zero)
            self.after = AfterUpdate(self.ha, self.j, settings, AfterPolicy(
                settle_seconds=self.o.after_update_minutes * 60.0 * scale, undo_hours=float(self.o.undo_hours),
                reask_hours=float(self.o.update_reask_hours), verify_seconds=120.0 * scale, poll=self.poll,
                enabled=self.o.after_update_minutes > 0), holds=self.holds, system=self.system, updater=self.updater)
            for engine in (self.updater, self.system, self.entities):
                engine.holds = self.holds
            # 0.8.9 Update review queue: the AI review gate (needs the tracking issue) and the waiting list.
            if self.o.report_issue and self.o.ai_review:
                self.ai = AiReviews(self.gh, self.j, self.o.report_issue)
                for engine in (self.updater, self.system, self.entities):
                    engine.ai = self.ai
                self.update_queue = UpdateQueue(self.j, self.report_text, self._update_note)
                self.watcher.review_waits = self.review_waits          # 0.9.12: reviewer watchdog
            for engine in (self.updater, self.system):
                engine.on_done = self.after.start
                engine.snapshot = (lambda: loaded_snapshot(self.ha)) if self.o.after_update_minutes > 0 else None
            self.updater.on_done = self.app_update_done             # 0.9.12: also remembered 24 h (after_update)
            self.watcher.claimed = self.after.claimed
            # 0.7.4: one-tap local App source install; its update step is exactly self.updater's path.
            self.stage_policy.poll = self.poll
            self.stage_policy.reload_seconds = 120.0 * scale
            self.stager = Stager(self.ha, self.j, settings, self.stage_policy, self.gh, self.updater,
                                 after_active=lambda: bool(self.after is not None and self.after.active()))
            # 0.8.0: automatic Broker keys, one App per night inside the automatic-update window.
            self.keys = KeyRotator(self.ha, self.j, settings, KeyPolicy(
                rotate_days=float(self.o.key_rotate_days),
                window=(self.o.auto_window_start_hour, self.o.auto_window_end_hour), enabled=self.o.key_rotation,
                probe_wait=90.0 * scale, verify_seconds=600.0 * scale, restart_seconds=120.0 * scale, poll=self.poll,
                ask_hours=(min(self.o.digest_hour, 20), 21)), self._key_broker_base)
            self.watcher.key_findings = self.keys.findings
            self.watcher.os_findings = self.system.findings    # 0.8.3: an OS update waiting for a restart
        return self.engine

    def _key_broker_base(self) -> str:
        """0.8.0: the Broker the Scout publishes to (its URL is checked to the exact Broker shape)."""
        if self.key_broker_base:
            return self.key_broker_base
        from .jobs import checked_inventory_url
        import urllib.parse
        host = urllib.parse.urlsplit(checked_inventory_url(self.ha.scout_view().broker_url)).hostname
        return f"https://{host}"

    def poll_keys(self) -> bool:
        """0.8.0: one step of automatic key changes. True when something was done (one action per poll)."""
        self.ensure_engine()
        if self.keys is None:
            return False
        try:
            out = self.keys.cycle()
        except (HAError, net.NetError) as err:
            LOG.warning("key check waits: %s", net.redact(str(err))[:200])
            return False
        if out is None:
            return False
        # A key change that could not be undone yet is retried each poll by keys.py itself; it must not freeze
        # the whole App (FAILED_MANUAL would), so it is reported as FAILED.
        outcome = FAILED if out.result_outcome == KEY_STUCK else out.result_outcome
        job = KEY_SETUP_JOB if out.request_id == "keys-setup" else KEY_JOB
        self.finish(out.request_id, job, f"keys:{out.request_id}", self.o.report_issue or None,
                    Result(outcome, out.reasons, out.facts), notify=out.notify)
        return True

    def keys_request(self, m: Manifest) -> None:
        """0.8.0 ROTATE_KEYS_NOW: queue the role(s); the change runs on the next polls (nothing asked)."""
        self.ensure_engine()
        issue = m.tracking_issue or self.o.report_issue or None
        if self.keys is None or not self.o.key_rotation:
            self.finish(m.request_id, m.job, m.digest, issue,
                        Result(REFUSED, ["automatic key changes are turned off (option key_rotation)"]), notify=False)
            return
        queued = self.keys.queue(str(m.key_role))
        self.finish(m.request_id, m.job, m.digest, issue, Result(DONE, [
            "queued; the key change(s) run on the next polls, one App at a time"], {"queue": queued}), notify=False)

    def _keys_attrs(self) -> dict:
        if self.keys is None:
            return {}
        try:
            return self.keys.summary()
        except Exception:  # noqa: BLE001 - visibility only
            return {}

    def poll_cf_relay(self) -> list[str]:
        """0.8.2: one read of the Cloudflare control results; runs every poll, even while frozen or mid-update
        (it only reads GitHub and notifies; it changes nothing)."""
        if not self.o.cf_control_issue:
            return []
        if self.cf_relay is None:
            try:
                url = self.o.cf_control_url or control_url_from_broker(self._key_broker_base())
            except Exception as err:  # noqa: BLE001 - no Broker address yet: try again next poll
                LOG.debug("Cloudflare relay waits for the Broker address: %s", type(err).__name__)
                return []
            if not url:
                return []
            self.cf_relay = CfRelay(self.gh, self.ha, self.j.dir, self.o.notify_service, self.o.cf_control_issue,
                                    url)
        sent = cf_safe_cycle(self.cf_relay, LOG)
        for item in sent:
            self.j.audit(item.split(":", 1)[0], "CF_RELAY", state=item.split(":", 1)[1])
        return sent

    def key(self, request_id: str) -> str:
        return f"{request_id}#dry" if self.o.dry_run else request_id

    def status(self, state: str, attributes: dict) -> None:
        self.ha.publish_status(state, attributes)

    def finish(self, request_id: str, job: str, digest: str, issue: int | None, result: Result,
               notify: bool = True) -> None:
        self.j.record(self.key(request_id), digest, result.outcome)
        self.j.audit(request_id, "OUTCOME", job=job, outcome=result.outcome, reasons=result.reasons[:5])
        if result.outcome == DONE and self.after_reports is not None:   # 0.9.26: its own after-update report in a day
            item = item_of(digest)
            if item:
                try:
                    active = self.after.active() if self.after is not None else None
                    checked = bool(active and active.get("update_request") == request_id)
                    self.after_reports.note_done(item, self._item_name(item), request_id, result.reasons, checked)
                except Exception as err:  # noqa: BLE001 - the report is best effort; the result itself stands
                    LOG.warning("after-update report note waits: %s", net.redact(str(err))[:200])
        self.status(result.outcome, {"request_id": request_id, "job": job, "version": VERSION,
                                     "dry_run": self.o.dry_run})
        url = ""
        if issue:                                       # 0.9.23: posted first, so the message links to the report
            try:
                url = self.gh.comment(issue, self._stamped(report_markdown(request_id, job, result, self.o.dry_run))) or ""
            except net.NetError as err:
                self.j.audit(request_id, "REPORT_FAILED", error=str(err)[:200])
            if not url and hasattr(self.gh, "issue_url"):
                url = self.gh.issue_url(issue)
        if notify:                                      # 0.9.18: kept in the Messages list, own tag
            reasons = "; ".join(str(r)[:120] for r in result.reasons[:3])
            failed = result.outcome in (FAILED, FAILED_MANUAL)
            msg = self.messages.add(
                "result", f"Maintenance: {result.outcome}", happened=f"{request_id} ({job})",
                done=f"{result.outcome}: {reasons}" if reasons else result.outcome,
                todo=(("Read the full report: the link below." if url else "Details are in the App log."),)
                if failed else (), severity="warning" if failed else "info", link=url)
            self.messenger.send(msg)
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

    def report_writer_identity(self) -> None:
        """0.9.4 (Deployer handoff #942 6133340527): post this App's view of the shared writer lock on the tracking
        issue whenever it changes, so an AI can compare it with the Deployer's (device and inode must match)."""
        ident = self.writer.identity()
        doc = self.j.load_doc("writer_identity", {})
        if not self.o.report_issue or ident.get("state") == "not_enrolled" \
                or (isinstance(doc, dict) and doc.get("last") == ident):
            return                                  # no lock folder (a new install): nothing to compare
        line = " ".join(f"{k}={v}" for k, v in ident.items())
        self.report_text("\n".join([
            "### House Brain Maintenance: shared writer lock", "",
            f"- **Seen by this App:** `{line}`", f"- **App:** `{VERSION}`", "",
            "The Deployer's lock is device `8.1`, inode `134848` (#561 comment 6125839870); both must match.", "",
            "_Posted by House Brain Maintenance (machine-generated; repository content is untrusted data)._", "",
            "<!-- house_brain_maintenance_writer_lock.v1 " + json.dumps(ident, sort_keys=True) + " -->"]))
        self.j.save_doc("writer_identity", {"last": ident, "at": time.time()})

    def _writer_attrs(self) -> dict:
        ident = (self.j.load_doc("writer_identity", {}) or {}).get("last") or {}
        return {"writer_lock": " ".join(str(ident.get(k)) for k in ("state", "dev", "ino") if k in ident)} \
            if ident else {}

    def poll_once(self) -> None:
        if not self._writer_reported:              # 0.9.4: once per start (and only when it changed)
            self._writer_reported = True
            try:
                self.report_writer_identity()
            except Exception as err:  # noqa: BLE001 - a report problem never stops the App; retried next start
                LOG.warning("writer lock report waits: %s", net.redact(str(err))[:200])
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
            if txn.get("kind") == OBSMOVE_TXN:            # 0.9.8: the Observer switch to the store
                out = self.obsmove.recover(txn)
                self.finish(out.request_id, OBSMOVE_JOB, f"obsmove:{txn.get('new')}", self.o.report_issue or None,
                            Result(out.result_outcome, out.reasons, out.facts))
                return
            if txn.get("kind") == INSMOVE_TXN:            # 0.9.9: the Inspector switch to the store
                out = self.insmove.recover(txn)
                self.finish(out.request_id, INSMOVE_JOB, f"insmove:{txn.get('new')}", self.o.report_issue or None,
                            Result(out.result_outcome, out.reasons, out.facts))
                return
            if txn.get("kind") == "entity":
                out = self.entities.recover(txn)
                out.facts = {"entity_id": txn.get("entity_id"), "kind": txn.get("entity_kind"), "from": txn.get("from"),
                             "to": txn.get("to"), **out.facts}
                self.finish_entity(out, str(txn.get("entity_kind")))
                return
            if txn.get("kind") == "pifw":                # 0.8.6: after the firmware restart
                out = self.pifirmware.recover(txn) if self.pifirmware is not None else None
                if out is None:
                    if self.pifirmware is None:
                        self.j.clear_txn()
                    else:
                        self.status("PI_FIRMWARE_UPDATING", {"version": VERSION, "to": txn.get("to")})
                    return
                self.finish(out.request_id, PIFW_JOB, f"pifw:{txn.get('to')}", self.o.report_issue or None,
                            Result(out.result_outcome, out.reasons, out.facts))
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
        if self.poll_os_activation():                   # 0.8.3: a staged OS update (restart ask, after-boot check)
            return
        if self.poll_stage_pending():
            return
        if self.poll_energy_pending():                  # 0.9.24: setup_only off again after a restart mid-delivery
            return
        if self.frozen():
            return
        if self.poll_page_reask():
            return
        if self.poll_page_leak():
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
        if self.poll_keys():                            # 0.8.0: at most one key step, then the next poll
            return
        try:                                            # 0.9.22: a failing update step never stops the problem watch
            if not self.poll_after_update():            # an after-update check is open: no new update starts,
                self.poll_updates()                     # but the problem watch keeps running (it skips the
            self.poll_update_queue()                    # 0.8.9: the "updates waiting" list (only when it changed)
            self.poll_after_reports()                   # 0.9.26: this App's own after-update results (no AI)
            self.poll_reviews()                         # 0.9.16: new AI reviews reach the page within READ_EVERY
            if self.engine is not None:                 # 0.9.0: the page's "Needs your decision" list
                self.inbox.set_offered(self.offered_rows())
        except Exception as err:  # noqa: BLE001 - shown as a problem (issues.from_step_errors), retried next poll
            message = net.redact(f"{type(err).__name__}: {err}")[:300]
            LOG.warning("update step failed (problem checks continue): %s", message)
            self.step_errors["updates"] = message
        else:
            self.step_errors.pop("updates", None)
        self.poll_issues()                              # integrations the check is handling)

    def app_update_done(self, kind: str, txn: dict) -> None:
        """0.7.3 after-update check. 0.9.12: the install is remembered 24 h for the problem queue's ``after_update``
        tag: the App, the running Apps that need its services (``deps_started``: the review counts only running ones)
        and the integrations it provides (``mqtt:provide`` -> mqtt)."""
        try:
            slug = str(txn.get("slug"))
            try:
                provides = [s.split(":", 1)[0] for s in self.ha.app_detail(slug).services if s.endswith(":provide")]
            except Exception:  # noqa: BLE001 - unreadable: the App and the Apps that need it still count
                provides = []
            self.watcher.note_install(item_app(slug, str(txn.get("to"))),
                                      [slug, *(txn.get("deps_started") or [])], provides)
        except Exception as err:  # noqa: BLE001 - a note problem never stops the after-update check
            LOG.warning("install note waits: %s", net.redact(str(err))[:200])
        self.after.start(kind, txn)

    def review_waits(self) -> list[tuple[str, float]] | None:
        """0.9.12 reviewer watchdog: (name, first offered) of each waiting update without an AI review; [] in practice
        mode (no list is posted); None when "no review" is not known (the last review read failed, or no update check
        ran since the start, so the waiting list is not built yet)."""
        if self.ai is None or not self.ai.enabled or self.o.dry_run:
            return []
        if not self.ai.readable() or not self.last_update_check:
            return None
        seen = {f"app:{k}": v for k, v in self.updater._mem()["first_seen"].items()}
        seen.update({f"entity:{k}": v for k, v in self.entities._mem()["first_seen"].items()})
        seen.update(self.system._mem()["reviewed"])
        rows = rows_from(*(e.queue for e in (self.system, self.updater, self.entities)))
        return [(f"{r.get('name')} {r.get('to')}", float(seen[r["item"]])) for r in rows
                if r["item"] in seen and self.ai.get(r["item"]) is None]

    def poll_after_update(self) -> bool:
        """0.7.3: one step of an open after-update check. True while one is open (updates wait)."""
        self.ensure_engine()
        if self.after is None or not self.after.active():
            return False
        out = self.after.cycle()
        if out is not None:
            self.finish(out.request_id, AFTER_JOB, f"after:{out.facts.get('update_request')}",
                        self.o.report_issue or None, Result(out.result_outcome, out.reasons, out.facts))
        return self.after.active() is not None

    def hold_request(self, m: Manifest) -> None:
        """0.7.3 HOLD_UPDATE / RELEASE_UPDATE: change the hold list (this App's memory only; nothing asked)."""
        self.ensure_engine()
        if m.job == HOLD_UPDATE:
            ok, why = self.holds.hold(str(m.hold_item), by=m.request_id, note=m.note)
        else:
            ok, why = self.holds.release(str(m.hold_item))
        issue = m.tracking_issue or self.o.report_issue or None
        self.finish(m.request_id, m.job, m.digest, issue, Result(DONE if ok else REFUSED, [why], self.holds.summary()),
                    notify=False)

    def poll_update_queue(self) -> None:
        """0.8.9: post the waiting list when it changed. Never stops the poll (a GitHub fault only waits)."""
        if self.update_queue is None or self.o.dry_run:
            return
        try:
            limit = self.j.approvals_requested_since(86400) >= self.o.max_approval_requests_per_day
            queues = [e.queue if e is not None else {} for e in (self.system, self.updater, self.entities)]
            self.update_queue.cycle(queues, self.ai, limit, VERSION)
        except net.NetError as err:
            LOG.warning("updates-waiting list waits: %s", net.redact(str(err))[:200])

    def offered_rows(self) -> list[dict]:
        """0.9.0: the waiting updates the page can decide (not held, rejected or quarantined), with the AI review."""
        rows = []
        ledger = self.j.ledger()
        engines = (("SYSTEM", self.system), ("UPDATE", self.updater), ("UPDATE", self.entities))
        for stage, engine in engines:
            for row in (getattr(engine, "queue", None) or {}).values():
                state = str(row.get("state") or "")
                if state.startswith(("held", "you rejected", "failed here")):
                    continue
                kind, to = row.get("kind"), str(row.get("to"))
                item = str(row.get("item") or "")
                if kind in ("core", "os"):
                    rid = system_mod.request_id_for(kind, to)
                elif kind == "app":
                    rid = updates_mod.request_id_for(item[4:].rsplit("@", 1)[0], to)
                else:
                    rid = entities_mod.request_id_for(kind, item[7:].rsplit("@", 1)[0], to)
                if (ledger.get(self.key(rid)) or {}).get("outcome") == "DONE":
                    continue                            # installed since the last update check
                review = self.ai.get(item) if self.ai is not None else None
                ai = (f"AI review: {review['verdict']}" + (f": {review['summary']}" if review.get("summary") else "")
                      if review else ("AI review: not posted yet" if self.ai is not None else ""))
                rows.append({"key": f"{stage}:{rid}|*", "stage": stage,
                             "title": f"{row.get('name')} {row.get('from')} → {to}",
                             "detail": "Status: " + (state or "checked at the next update check")
                             + (f"\nThis App's review: {row.get('verdict')}" if row.get("verdict") else ""),
                             "ai": ai, "ai_url": (review or {}).get("url", ""),
                             "ai_verdict": (review or {}).get("verdict", "")})
        if self.watcher is not None and not self.o.dry_run:   # 0.9.23: "Fix it" for problems with a built-in fix
            try:
                rows += self.watcher.fix_rows()
            except Exception as err:  # noqa: BLE001 - the update rows stay; the fix rows return next poll
                LOG.warning("fix rows wait: %s", net.redact(f"{type(err).__name__}: {err}")[:200])
        return rows

    def report_text(self, text: str) -> str:
        """Post on the tracking issue; 0.9.23: returns the comment's address ("" when unknown)."""
        if self.o.report_issue:
            return self.gh.comment(self.o.report_issue, self._stamped(text)) or ""
        return ""

    def _item_name(self, item: str) -> str:
        """0.9.26: a finished update's display name, from the waiting list while it is still there, else its item."""
        try:
            for r in rows_from(*(getattr(e, "queue", None) or {} for e in (self.system, self.updater, self.entities))):
                if r.get("item") == item:
                    return f"{r.get('name')} {r.get('to')}"
        except Exception:  # noqa: BLE001 - a name only
            pass
        kind, _, rest = item.partition(":")
        if kind == "app" and "@" in rest:
            slug, version = rest.rsplit("@", 1)
            names = getattr(self.watcher, "app_names", {}) or {}
            return f"{names.get(slug, slug)} {version}"
        if kind in ("core", "os"):
            return f"Home Assistant {'Core' if kind == 'core' else 'OS'} {item.partition('@')[2]}"
        return item

    def poll_after_reports(self) -> None:
        """0.9.26: about a day after an update this App installed, post its own after-update result (no AI)."""
        if self.after_reports is None or self.watcher is None:
            return
        self.after_reports.cycle(self.j.ledger(), lambda rid: self.key(after_request_id(rid)),
                                 self.watcher._state()["open"], VERSION)

    def needs_review(self) -> dict:
        """0.9.26: how many waiting updates and open problems have no AI review that counts; None = not known
        (a review read failed, or no update check ran yet). Published on the status sensor and on every post."""
        updates = None
        try:
            waits = self.review_waits() if self.engine is not None and self.updater is not None else None
            updates = len(waits) if waits is not None else None
        except Exception:  # noqa: BLE001 - not known this time
            updates = None
        problems = None
        try:
            ai = getattr(self.watcher, "problem_ai", None) if self.watcher is not None else None
            if ai is not None and ai.enabled and ai.readable():
                st = self.watcher._state()
                problems = sum(1 for k, r in st["open"].items() if isinstance(r, dict) and r.get("alerted")
                               and k != STALL_KEY and ai.get(k) is None)
        except Exception:  # noqa: BLE001 - not known this time
            problems = None
        total = None if updates is None and problems is None else (updates or 0) + (problems or 0)
        return {"updates": updates, "problems": problems, "total": total}

    def _stamped(self, text: str) -> str:
        """0.9.26 (reviewer plan step 1): every post on the tracking issue carries one machine line with the
        needs-review count right after its heading, so a reviewer's read of the newest comment is enough to know
        whether anything waits. Nothing about the items themselves is added here."""
        line = (f"<!-- {NEEDS_REVIEW_SCHEMA} "
                + json.dumps({**self.needs_review(), "app": VERSION}, separators=(",", ":")) + " -->")
        head, sep, rest = text.partition("\n")
        return f"{head}\n{line}{sep}{rest}" if sep else f"{text}\n{line}"

    def poll_issues(self) -> None:
        if not self.o.issue_checks:
            return
        self.ensure_engine()
        if self.watcher.problem_ai is not None:            # 0.9.6: AI problem reviews (paged, spaced reads)
            try:
                self.watcher.problem_ai.refresh()
            except Exception as err:  # noqa: BLE001 - the last reviews read stand
                LOG.warning("AI problem review read waits: %s", net.redact(str(err))[:200])
        summary = self.watcher.check()
        self.status("WATCHING", {"version": VERSION, "dry_run": self.o.dry_run, **summary, **self._ledger_attrs(),
                                 **self._writer_attrs(), **self._skipped_attrs(),
                                 **self._waiting_attrs(), **self._keys_attrs(),
                                 "needs_review": self.needs_review()})          # 0.9.26

    def _skipped_attrs(self) -> dict:
        """0.9.15: installed-App rows the last list read skipped, by reason ({} when none)."""
        skipped = getattr(self.ha, "apps_skipped", None) if self.ha is not None else None
        return {"apps_skipped": dict(skipped)} if isinstance(skipped, dict) and skipped else {}

    def _waiting_attrs(self) -> dict:
        """0.7.2: updates asked and not answered (or reviewed and waiting for the lead time), from this App's own
        memory and the versions the last check read: no extra Supervisor call. Also feeds the page's list."""
        items: list[dict] = []
        if self.system is not None:
            items += self.system.waiting()
        if self.updater is not None:
            items += self.updater.waiting()
        self.reask.set_waiting(items)
        return {"pending_updates": len(items), "waiting_updates": items[:MAX_WAITING]}

    def _ledger_attrs(self) -> dict:
        """0.5.2: restart ledger counts and MTBF from the Recovery Report (empty when it is off)."""
        if self.recovery is None:
            return {}
        try:
            return self.recovery.status_attrs()
        except Exception:  # noqa: BLE001 - visibility only
            return {}

    def poll_reviews(self) -> None:
        """0.9.16 (owner pop-up 2026-10-18 "All of the above"): read new AI update reviews on every poll, not only at the
        hourly update check, so the page stops saying "AI review: not posted yet" for up to an hour after one was
        posted (live 2026-10-17: posted 14:29Z, still "not posted yet" at 15:28Z). ``refresh`` itself reads at most
        every READ_EVERY (10 minutes): about 6 GitHub reads an hour. Asks and installs still wait for the update
        check, as before."""
        if self.engine is None or self.ai is None:
            return
        try:
            self.ai.refresh()
        except net.NetError as err:
            LOG.warning("AI review read waits (the last reviews read stand): %s", net.redact(str(err))[:200])

    def poll_updates(self) -> None:
        if time.monotonic() - self.last_update_check < self.o.update_check_minutes * 60 and self.last_update_check \
                and not self.inbox.has_decisions():     # 0.9.0: a page decision runs on this poll
            return
        self.last_update_check = time.monotonic()
        self.ensure_engine()
        if self.ai is not None:                         # 0.8.9: new AI reviews before anything is asked
            try:
                self.ai.refresh()
            except net.NetError as err:
                LOG.warning("AI review read waits (the last reviews read stand): %s", net.redact(str(err))[:200])
        out = self.updater.cycle()
        if out is None:
            # 0.6.6: the Deployer switch-over, only when no App update ran in this cycle.
            mig = self.migrator.cycle() if self.migrator is not None else None
            if mig is not None:
                self.finish(mig.request_id, MIGRATE_JOB, f"migrate:{mig.facts.get('new') or mig.facts.get('old')}",
                            self.o.report_issue or None, Result(mig.result_outcome, mig.reasons, mig.facts))
                return
            sm = self.scoutmove.cycle() if self.scoutmove is not None else None
            if sm is not None:                          # 0.9.3: Inventory Scout from the store
                self.finish(sm.request_id, SCOUTMOVE_JOB, f"scoutmove:{sm.facts.get('new')}",
                            self.o.report_issue or None, Result(sm.result_outcome, sm.reasons, sm.facts))
                return
            om = self.obsmove.cycle() if self.obsmove is not None else None
            if om is not None:                          # 0.9.8: Engineering Observer from the store
                self.finish(om.request_id, OBSMOVE_JOB, f"obsmove:{om.facts.get('new')}",
                            self.o.report_issue or None, Result(om.result_outcome, om.reasons, om.facts))
                return
            im = self.insmove.cycle() if self.insmove is not None else None
            if im is not None:                          # 0.9.9: Engineering Inspector from the store
                self.finish(im.request_id, INSMOVE_JOB, f"insmove:{im.facts.get('new')}",
                            self.o.report_issue or None, Result(im.result_outcome, im.reasons, im.facts))
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
            # 0.8.6: Pi firmware, only when nothing else ran in this cycle.
            fw_out = self.pifirmware.cycle() if self.pifirmware is not None else None
            if fw_out is not None:
                self.finish(fw_out.request_id, PIFW_JOB, f"pifw:{fw_out.facts.get('to')}", self.o.report_issue or None,
                            Result(fw_out.result_outcome, fw_out.reasons, fw_out.facts))
                return
            if (self.j.load_txn() or {}).get("kind") == "pifw":
                return                                  # the firmware restart is under way
            self.status("IDLE", {"version": VERSION, "dry_run": self.o.dry_run, **self._waiting_attrs(),
                                 **self._ledger_attrs(), **self._keys_attrs()})
            return
        result = Result(out.result_outcome, out.reasons, out.facts)
        self.finish(out.request_id, UPDATE_JOB, f"update:{out.facts.get('slug')}@{out.facts.get('to')}",
                    self.o.report_issue or None, result)

    def finish_entity(self, out, kind: str) -> None:
        self.finish(out.request_id, ENTITY_JOBS.get(kind, "UPDATE_HACS"),
                    f"entity:{out.facts.get('entity_id')}@{out.facts.get('to')}", self.o.report_issue or None,
                    Result(out.result_outcome, out.reasons, out.facts))

    def poll_os_activation(self) -> bool:
        """0.8.3: one step of a staged OS update. True when it reported something, or while a restart this App
        started is under way (nothing else may start then)."""
        self.ensure_engine()
        if self.system is None:
            return False
        out, blocking = self.system.os_step()
        if out is not None:
            self.finish_system("os", str(out.facts.get("to") or ""), out)
            return True
        if blocking:
            self.status("OS_RESTARTING", {"version": VERSION})
        return blocking

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
                if request_id not in self.rate_noted:  # once per request (the poll now goes on every minute)
                    self.rate_noted.add(request_id)
                    self.j.audit(request_id, "RATE_LIMITED")
                # 0.8.9: not "handled". Returning True here ended every poll at this request until the ask limit
                # freed, so neither update checks nor the problem watch ran meanwhile (nor queued read-only jobs).
                continue
            if manifest.job in (HOLD_UPDATE, RELEASE_UPDATE):
                self.hold_request(manifest)
                return True
            if manifest.job == ASK_UPDATE_NOW:
                self.ask_update_now(manifest)
                return True
            if manifest.job == STAGE_LOCAL_APP_SOURCE:
                self.stage_request(manifest)
                return True
            if manifest.job == ROTATE_KEYS_NOW:
                self.keys_request(manifest)
                return True
            if manifest.job == ENERGY_AI_SETTINGS:
                self.energy_request(manifest, head)
                return True
            result = self.ensure_engine().run(manifest)
            self.finish(request_id, manifest.job, manifest.digest, manifest.tracking_issue, result)
            return True  # at most one request per poll
        return False

    def stage_request(self, m: Manifest) -> None:
        """0.7.4 STAGE_LOCAL_APP_SOURCE: verify the pinned carrier, ask once, stage, update, report on #454.
        The request is marked STAGING first, so a restart mid-job never asks twice (``poll_stage_pending``)."""
        self.ensure_engine()
        issue = m.tracking_issue or self.o.report_issue or None
        self.j.record(self.key(m.request_id), m.digest, "STAGING")
        try:
            result = self.stager.run(m)
        except (HAError, net.NetError) as err:
            # 0.8.1 (found by #975): after the Approve the source may already be swapped; never say "nothing changed".
            if self.j.load_doc(STAGE_PENDING_DOC, {}):
                result = Result(FAILED_MANUAL, [
                    "the install was interrupted after your Approve; its recovery record is kept and it is checked "
                    f"again on the next poll ({net.redact(str(err))[:200]})"], {"slug": m.stage_slug, "to": m.stage_to})
            else:
                result = Result(FAILED, [f"preflight: {net.redact(str(err))[:200]}; nothing asked or changed"],
                                {"slug": m.stage_slug, "to": m.stage_to})
        self.finish(m.request_id, m.job, m.digest, issue, result)

    def energy(self) -> EnergyDelivery:
        engine = self.ensure_engine()
        return EnergyDelivery(self.ha, self.j, engine.s, self.gh, engine._ask)

    def energy_request(self, m: Manifest, head: str) -> None:
        """0.9.24 ENERGY_AI_SETTINGS: verify the pinned settings file and the App, ask once, deliver, report."""
        issue = m.tracking_issue or self.o.report_issue or None
        self.j.record(self.key(m.request_id), m.digest, "DELIVERING")
        try:
            result = self.energy().run(m, head)
        except (HAError, net.NetError) as err:
            reason = net.redact(str(err))[:200]
            if self.j.load_doc(ENERGY_PENDING_DOC, {}):
                result = Result(FAILED, [f"interrupted after your Approve ({reason}); setup_only is switched "
                                         "off on the next poll"])
            else:
                result = Result(FAILED, [f"preflight: {reason}; nothing asked or changed"])
        self.finish(m.request_id, m.job, m.digest, issue, result)

    def poll_energy_pending(self) -> bool:
        doc = self.j.load_doc(ENERGY_PENDING_DOC, {})
        if not isinstance(doc, dict) or not doc.get("request_id"):
            return False
        try:
            out = self.energy().recover(doc)
        except (HAError, net.NetError) as err:
            LOG.warning("Energy AI setup_only restore waits: %s", net.redact(str(err))[:200])
            return True
        if out is not None:
            rid, result = out
            previous = self.j.ledger().get(self.key(rid), {}).get("digest") or "recovered"
            self.finish(rid, ENERGY_AI_SETTINGS, previous, None, result)
        return True

    def poll_stage_pending(self) -> bool:
        """0.7.4: after a restart mid-install, read what is true and undo a staged source the App does not run.
        True when one was handled."""
        doc = self.j.load_doc(STAGE_PENDING_DOC, {})
        if not isinstance(doc, dict) or not doc.get("request_id"):
            return False
        if doc.get("manual_recovery_hold"):
            # 0.8.1 (found by #975): an install whose recovery needed the owner is never retried by itself. The
            # owner's clear_freeze_for = <request id> (the same switch that lifts the pause) releases it.
            if self.o.clear_freeze_for and self.o.clear_freeze_for == doc.get("request_id"):
                self.j.save_doc(STAGE_PENDING_DOC, {})
                self.j.audit(str(doc.get("request_id")), "STAGE_HOLD_CLEARED_BY_OWNER")
                return False
            return True
        self.ensure_engine()
        try:
            rid, result = self.stager.recover(doc)
        except (HAError, net.NetError) as err:
            LOG.warning("install recovery waits (GitHub or Supervisor not answering): %s",
                        net.redact(str(err))[:200])
            return True                     # 0.8.1 (#975): nothing new starts while an install is unresolved
        # 0.8.1 (#975): a recovery that needs the owner keeps its record (no repeat without evidence).
        self.j.save_doc(STAGE_PENDING_DOC, {**doc, "manual_recovery_hold": True} if result.outcome == FAILED_MANUAL
                        else {})
        issue = doc.get("issue") if isinstance(doc.get("issue"), int) else None
        self.finish(rid, STAGE_LOCAL_APP_SOURCE, str(doc.get("digest") or "recovered"),
                    issue or self.o.report_issue or None, result)
        return True

    def ask_update_now(self, m: Manifest) -> None:
        """0.6.5 ASK_UPDATE_NOW: the same ask -> backup -> update -> health -> restore path as the update check,
        without the re-ask wait. The request is marked ASKING first, so a restart mid-update never asks twice
        (the update's own result then follows under its update request id, as for any update)."""
        self._ask_now(m.request_id, m.job, m.digest, m.tracking_issue or self.o.report_issue or None,
                      str(m.update_kind), m.update_slug, str(m.update_version), {})

    def _ask_now(self, rid: str, job: str, digest: str, issue: int | None, kind: str, slug: str | None,
                 version: str, extra: dict) -> None:
        self.ensure_engine()
        if self.after is not None and self.after.active():
            self.finish(rid, job, digest, issue, Result(REFUSED, [
                "an after-update check is still open (Reload/Undo); no new update starts until it closes"], dict(extra)))
            return
        self.j.record(self.key(rid), digest, "ASKING")
        try:
            if kind == "app":
                out = self.updater.ask_now(str(slug), version)
            else:
                out = self.system.ask_now(kind, version)
        except (HAError, net.NetError) as err:
            reason = net.redact(str(err))[:200]
            result = (Result(FAILED_MANUAL, [f"read-back failed ({reason}); it is re-checked on the next poll"])
                      if self.j.load_txn() else Result(FAILED, [f"preflight: {reason}; nothing asked or changed"]))
            result.facts.update(extra)
            self.finish(rid, job, digest, issue, result)
            return
        if out is None:                              # OS update started: the Pi reboots, the result follows
            self.finish(rid, job, digest, issue, Result(DONE, [
                "approved; the OS update started (the Pi reboots); its result is posted when the Pi is back"],
                {"kind": "OS", "to": version, "automatic": False, **extra}))
            return
        self.finish(rid, job, digest, issue,
                    Result(out.result_outcome, out.reasons, {**out.facts, "update_request": out.request_id, **extra}))

    def poll_page_leak(self) -> bool:
        """0.9.13 "Suspected leak?": refresh the credential list once a day (read-only) and run the owner's tap
        through the ROTATE_KEYS_NOW path for every role. True when a tap was handled."""
        if self.leak.refresh_due():
            try:
                self.leak.refresh(lambda: self.gh.read_file(INVENTORY_PATH, "main"))
            except Exception as err:  # noqa: BLE001 - the page says the list could not be read; never stops a poll
                LOG.info("credential list read waits: %s", net.redact(str(err))[:200])
        item, lapsed = self.leak.take()
        if lapsed:
            self.j.audit("page-leak", "PAGE_LEAK_EXPIRED")
        if item is None:
            return False
        self.ensure_engine()
        rid = "leak-" + time.strftime("%Y%m%d%H%M%S", time.gmtime(item["at"]))
        issue = self.o.report_issue or None
        if self.keys is None or not self.o.key_rotation:
            self.finish(rid, "ROTATE_KEYS_NOW", f"page-leak:{rid}", issue, Result(REFUSED, [
                ("automatic key changes are turned off (option key_rotation); change the keys by hand from the "
                 "Suspected leak? page")], {"asked_via": "page_leak"}), notify=False)
            return True
        queued = self.keys.queue("all")
        self.leak.started()
        self.finish(rid, "ROTATE_KEYS_NOW", f"page-leak:{rid}", issue, Result(DONE, [
            ("suspected leak: the owner started a change of every Broker key from the page; they run on the next "
             "polls, one App at a time"), "the rest of the credentials are listed on the page with what to do"],
            {"asked_via": "page_leak", "queue": queued}), notify=False)
        return True

    def poll_page_reask(self) -> bool:
        """0.7.2 "Ask me again" (the page): at most one queued owner request, run through exactly the
        ASK_UPDATE_NOW path (every refusal applies) and counted against max_approval_requests_per_day. A queued
        request older than one hour lapses unused. True when one was handled."""
        item, lapsed = self.reask.take()
        if lapsed:
            self.j.audit("page-reask", "PAGE_REASK_EXPIRED")
        if item is None:
            return False
        kind, slug, version = item["kind"], item.get("slug"), item["version"]
        stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime(item["queued_at"]))
        rid = re.sub(r"[^a-z0-9-]+", "-", f"page-{kind}-{(slug or '')[:20]}-{version[:16]}".lower()).strip("-")
        rid = re.sub(r"-+", "-", f"{rid[:48]}-{stamp}")
        digest = f"page:{kind}:{slug or ''}@{version}"
        issue = self.o.report_issue or None
        facts = {"asked_via": "page", "kind": kind.upper(), "to": version, **({"slug": slug} if slug else {})}
        if self.j.approvals_requested_since(86400) >= self.o.max_approval_requests_per_day:
            self.j.audit(rid, "RATE_LIMITED")
            self.finish(rid, PAGE_JOB, digest, issue, Result(REFUSED, [
                "today's approval-ask limit (max_approval_requests_per_day) is reached; nothing asked. The App asks "
                "again by itself at the re-ask time"], facts), notify=False)
            return True
        self._ask_now(rid, PAGE_JOB, digest, issue, kind, slug, version, {"asked_via": "page"})
        return True


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
    if os.environ.get("HBM_TEST_MODE") == "1":       # 0.7.4: real-container test against fake folders
        service.stage_policy.addons = os.environ.get("HBM_ADDONS_DIR", service.stage_policy.addons)
        service.stage_policy.share = os.environ.get("HBM_SHARE_DIR", service.stage_policy.share)
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
                           connector=getattr(service, "connector", None), reask=getattr(service, "reask", None),
                           inbox=getattr(service, "inbox", None), leak=getattr(service, "leak", None),
                           messages=getattr(service, "messages", None))
    server.start()
    return server


def poll_cycle(service: Service) -> None:
    """One loop step: R2 old-token retire (no token in the log, ever), then the poll."""
    if service.gh.auth.maybe_retire() == "RETIRED":
        LOG.info("the old hand-made GitHub token was retired on GitHub (the GitHub App signs in now)")
    service.poll_cf_relay()
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
    if not service.o.dry_run:                       # 0.9.18: the push button "Handled" (marks a message only)
        from .messages import HandledListener
        HandledListener(service.ha, service.messages, service._owner_id, lambda: service.stop).start()
    LOG.info("House Brain Maintenance %s started (dry_run=%s)", VERSION, service.o.dry_run)
    LOG.info("HBM-NATIVE writer_lock %s", " ".join(f"{k}={v}" for k, v in service.writer.identity().items()))
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
        while not service.stop and time.monotonic() < deadline and not service.wake.is_set():
            time.sleep(1)                       # 0.9.0: a decision on the page wakes the loop at once
        service.wake.clear()
    service.stop = True
    if rec_thread is not None:
        rec_thread.join(timeout=10)   # writes the clean-stop heartbeat
    service.ha.close_shared_ws()      # 0.6.1: the one shared Core socket
    LOG.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
