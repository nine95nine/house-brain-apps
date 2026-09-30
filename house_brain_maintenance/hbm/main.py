"""Entrypoint: options, poll loop, rate limit, ledger, reporting (Deployer lineage)."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import signal
import sys
import time
from dataclasses import dataclass

from . import VERSION, approval, net
from .github import REQUESTS_DIR, RE_REPO, GitHub
from .ha import RE_SLUG, HomeAssistant
from .jobs import DONE, FAILED_MANUAL, REFUSED, Engine, Result, Settings
from .updates import ASK, AUTO_LOW_RISK, JOB as UPDATE_JOB, RESTORED, Policy, Updater
from .journal import Journal
from .watch import Watcher, WatchPolicy
from .manifest import RE_REQUEST_ID, ManifestError, parse
from .web import INGRESS_PEER, ApprovalBoard, IngressServer

LOG = logging.getLogger("hbm")
RE_NOTIFY = re.compile(r"^mobile_app_[a-z0-9_]{1,80}$")
RE_USERNAME = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
RE_BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")
MAX_REQUESTS_PER_POLL = 10


class OptionsError(ValueError):
    pass


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
    health_check_minutes: int = 3
    report_issue: int = 0
    issue_checks: bool = True
    digest_hour: int = 8


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

    token = raw.get("github_token")
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_]{20,255}", token):
        raise OptionsError("github_token")
    net.register_secret(token)
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
        health_check_minutes=i("health_check_minutes", 1, 30) if "health_check_minutes" in raw else 3,
        report_issue=i("report_issue", 0, 10**7) if "report_issue" in raw else 0,
        issue_checks=raw.get("issue_checks", True) if isinstance(raw.get("issue_checks", True), bool)
        else _bad("issue_checks"),
        digest_hour=i("digest_hour", 0, 23) if "digest_hour" in raw else 8,
    )


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
        self.watcher: Watcher | None = None
        self.health_scale = health_scale
        self.last_update_check = 0.0
        self.last_request_error = ""
        self.missing_branch_logged = False
        self.stop = False

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
                            exclude=frozenset())
            self.updater = Updater(self.ha, self.j, settings, policy)
            watch = WatchPolicy(digest_hour=self.o.digest_hour,
                                max_fix_asks_per_day=self.o.max_approval_requests_per_day,
                                verify_seconds=60.0 * self.health_scale, poll=self.poll)
            self.watcher = Watcher(self.ha, self.j, settings, watch, self.report_text)
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
        elif result.outcome in (DONE, RESTORED) and self.j.frozen_by() == request_id:
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
        self.status("WATCHING", {"version": VERSION, "dry_run": self.o.dry_run, **summary})

    def poll_updates(self) -> None:
        if time.monotonic() - self.last_update_check < self.o.update_check_minutes * 60 and self.last_update_check:
            return
        self.last_update_check = time.monotonic()
        self.ensure_engine()
        out = self.updater.cycle()
        if out is None:
            self.status("IDLE", {"version": VERSION, "dry_run": self.o.dry_run, "pending_updates": 0})
            return
        result = Result(out.result_outcome, out.reasons, out.facts)
        self.finish(out.request_id, UPDATE_JOB, f"update:{out.facts.get('slug')}@{out.facts.get('to')}",
                    self.o.report_issue or None, result)

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
            if self.j.approvals_requested_since(86400) >= self.o.max_approval_requests_per_day:
                self.j.audit(request_id, "RATE_LIMITED")
                return True
            result = self.ensure_engine().run(manifest)
            self.finish(request_id, manifest.job, manifest.digest, manifest.tracking_issue, result)
            return True  # at most one request per poll
        return False


def build_service(options_path: str, data_dir: str) -> Service:
    opts = load_options(options_path)
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        raise OptionsError("SUPERVISOR_TOKEN missing")
    sup = os.environ.get("HBM_SUPERVISOR_URL", "http://supervisor")
    ws = os.environ.get("HBM_CORE_WS_URL", "ws://supervisor/core/websocket")
    api = os.environ.get("HBM_GITHUB_API", "https://api.github.com")
    ha = HomeAssistant(sup, token, ws, opts.scout_slug, opts.observer_slug)
    gh = GitHub(opts.github_repo, opts.github_token, api)
    approval_timeout = opts.approval_timeout_minutes * 60.0
    run_timeout, poll, health_scale = 600.0, 5.0, 1.0
    if os.environ.get("HBM_TEST_MODE") == "1":
        scale = float(os.environ.get("HBM_TEST_TIME_SCALE", "1"))
        approval_timeout *= scale
        run_timeout *= scale
        health_scale = scale
        poll = max(0.05, poll * scale)
    return Service(opts, ha, gh, Journal(data_dir), approval_timeout, board=ApprovalBoard(),
                   scout_run_timeout=run_timeout, poll=poll, health_scale=health_scale)


def start_ingress(service: Service) -> IngressServer | None:
    if service.board is None:
        return None
    port, peer = 8099, INGRESS_PEER
    if os.environ.get("HBM_TEST_MODE") == "1":
        port = int(os.environ.get("HBM_INGRESS_PORT", "8099"))
        peer = os.environ.get("HBM_INGRESS_PEER", INGRESS_PEER)
    server = IngressServer(service.board, port=port, allowed_peer=peer)
    server.start()
    return server


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
            service.poll_once()
        except Exception as err:  # noqa: BLE001 - the loop must survive transient faults
            message = net.redact(f"{type(err).__name__}: {err}")[:300]
            LOG.warning("poll failed: %s", message)
            service.status("ERROR", {"version": VERSION, "error": message})
        if once:
            break
        deadline = time.monotonic() + service.o.poll_seconds
        while not service.stop and time.monotonic() < deadline:
            time.sleep(1)
    LOG.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
