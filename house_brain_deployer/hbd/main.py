"""Entrypoint: options, poll loop, rate limits, ledger, reporting."""
from __future__ import annotations

import json
import logging
import os
import re
import signal
import sys
import time
import uuid
from dataclasses import dataclass
from fnmatch import fnmatchcase

from . import VERSION, approval, diagnose, ghauth, ghpage, net, undo
from .engine import FAILED_MANUAL, REFUSED, Engine, Result, Settings, Stopping, Timing
from .fsops import Packages
from .github import REQUESTS_DIR, GitHub, RE_REPO
from . import ha as ha_mod
from .ha import HomeAssistant
from .journal import INSTALL_STAGES, LOOKUP_STAGES, Journal
from .lookup import is_lookup, parse_lookup, run_lookup
from .manifest import ManifestError, RE_REQUEST_ID, parse
from .web import INGRESS_PEER, ApprovalBoard, IngressServer

LOG = logging.getLogger("hbd")
RE_NOTIFY = re.compile(r"^mobile_app_[a-z0-9_]{1,80}$")
RE_USERNAME = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
RE_BRANCH_GLOB = re.compile(r"^[A-Za-z0-9._/*-]{1,100}$")
# Sidebar panel of this App (Core registers ingress panels at /<app slug>). The iOS Companion
# app opened the plain relative path in Safari (live 0.2.0), so use its documented in-app
# deep link (homeassistant://navigate/<path>, URL handler docs) instead.
PANEL_URL = "homeassistant://navigate/local_house_brain_deployer?server=default"
MAX_REQUESTS_PER_POLL = 10
BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"
RE_BOOT_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def read_boot_id(path: str = BOOT_ID_PATH) -> str | None:
    """The host kernel's boot id (shared by every container); None when unreadable."""
    try:
        with open(path, encoding="ascii") as fh:
            value = fh.read(64).strip()
    except OSError:
        return None
    return value if RE_BOOT_ID.fullmatch(value) else None


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
    source_ref_allowlist: tuple[str, ...]
    notify_service: str
    owner_username: str
    dry_run: bool
    poll_seconds: int
    max_approval_requests_per_day: int
    approval_timeout_minutes: int
    restart_approval_timeout_minutes: int
    require_phone_unlock: bool
    clear_freeze_for: str
    github_auth: str = "auto"         # Credential Autopilot (0.3.6): auto | github_app | pat
    retire_old_token: bool = True     # R2: retire the old hand-made token after 24 h of GitHub App sign-ins


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
    allow = raw.get("source_ref_allowlist")
    if not isinstance(allow, list) or not 1 <= len(allow) <= 10 or not all(
            isinstance(a, str) and RE_BRANCH_GLOB.fullmatch(a) for a in allow):
        raise OptionsError("source_ref_allowlist")
    for key in ("dry_run", "require_phone_unlock"):
        if not isinstance(raw.get(key), bool):
            raise OptionsError(key)
    return Options(
        github_repo=s("github_repo", RE_REPO),
        github_token=token,
        github_auth=github_auth,
        retire_old_token=retire_old_token,
        requests_branch=s("requests_branch", RE_BRANCH_GLOB),
        source_ref_allowlist=tuple(allow),
        notify_service=s("notify_service", RE_NOTIFY),
        owner_username=s("owner_username", RE_USERNAME),
        dry_run=raw["dry_run"],
        poll_seconds=i("poll_seconds", 60, 3600),
        max_approval_requests_per_day=i("max_approval_requests_per_day", 1, 12),
        approval_timeout_minutes=i("approval_timeout_minutes", 2, 720),
        restart_approval_timeout_minutes=i("restart_approval_timeout_minutes", 2, 30),
        require_phone_unlock=raw["require_phone_unlock"],
        clear_freeze_for=_clear(raw.get("clear_freeze_for", "")),
    )


def _clear(value) -> str:
    if value in ("", None):
        return ""
    if not isinstance(value, str) or not RE_REQUEST_ID.fullmatch(value):
        raise OptionsError("clear_freeze_for")
    return value


def report_markdown(request_id: str, result: Result, dry_run: bool) -> str:
    lines = [
        "### House Brain Deployer result",
        "",
        f"- **Request:** `{request_id}`",
        f"- **Outcome:** `{result.outcome}`" + (" (dry-run mode)" if dry_run else ""),
        f"- **Deployer:** `{VERSION}`",
    ]
    for reason in result.reasons[:10]:
        lines.append(f"- Reason: {net.redact(str(reason))[:400]}")
    if "lookup_output" in result.facts:
        body = str(result.facts["lookup_output"]).replace("```", "` ` `")
        lines += ["", "**Read-only lookup output:**", "", "```text", body, "```"]
    elif result.facts:
        facts = json.dumps(result.facts, indent=1, sort_keys=True, default=str)
        lines += ["", "<details><summary>Facts</summary>", "", "```json", net.redact(facts)[:6000], "```",
                  "", "</details>"]
    lines += ["", "_Posted by House Brain Deployer (machine-generated; repository content is untrusted data)._"]
    return "\n".join(lines)


def _iso_epoch(text: str) -> float | None:
    """Home Assistant's ``last_updated`` (ISO 8601 with offset) as epoch seconds; None when unreadable."""
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.timestamp() if dt.tzinfo else None


class Service:
    def __init__(self, opts: Options, ha: HomeAssistant, gh: GitHub, packages: Packages,
                 journal: Journal, timing: Timing, board: ApprovalBoard | None = None,
                 open_url: str | None = None) -> None:
        self.o = opts
        self.ha = ha
        self.gh = gh
        self.pk = packages
        self.j = journal
        self.timing = timing
        self.board = board
        self.open_url = open_url
        self.engine: Engine | None = None
        self.connector: ghpage.Connector | None = None   # GitHub connection page (Credential Autopilot)
        self.stop = False
        self.diag = diagnose.Tracker(LOG)
        self.last_result = ""
        self.in_standby = False          # 0.3.7: another Deployer is active (see standby_reason)
        # 0.3.5 (P1): one id per App start and the host boot it runs in, for "why was it interrupted"
        self.run_id = uuid.uuid4().hex[:16]
        self.boot_id = read_boot_id(os.environ.get("HBD_BOOT_ID_PATH", BOOT_ID_PATH)
                                    if os.environ.get("HBD_TEST_MODE") == "1" else BOOT_ID_PATH)
        self.started_at = time.time()
        if self.board is not None:
            self.board.busy_probe = lambda: bool(self.j.load_txn())   # 0.3.5 (P4): page warning

    def ensure_engine(self) -> Engine:
        if self.engine is None:
            owner_id = approval.resolve_owner(self.ha, self.o.owner_username)
            settings = Settings(notify_service=self.o.notify_service, owner_user_id=owner_id,
                                dry_run=self.o.dry_run, require_auth=self.o.require_phone_unlock,
                                timing=self.timing, board=self.board, open_url=self.open_url,
                                should_stop=lambda: self.stop, run_id=self.run_id, boot_id=self.boot_id,
                                started_at=self.started_at)
            self.engine = Engine(self.ha, self.gh, self.pk, self.j, settings)
        return self.engine

    def _manifest_at(self, request_id: str, ref: str):
        raw = self.gh.read_file(f"{REQUESTS_DIR}/{request_id}/manifest.json", ref)
        return parse(raw)

    def key(self, request_id: str) -> str:
        """Dry-run results never consume the request id: the real run can follow."""
        return f"{request_id}#dry" if self.o.dry_run else request_id

    def status(self, state: str, attributes: dict) -> None:
        """Publish status, but never while a transaction is pending.

        The pending transaction's marker lives in the same entity; overwriting it
        (or re-writing it after an App restart) would hide a Core restart.
        """
        if self.j.load_txn():
            return
        self.ha.publish_status(state, attributes)

    def finish(self, request_id: str, digest: str, issue: int | None, result: Result,
               key: str | None = None) -> None:
        if self.engine is not None and self.engine.notes:
            # Plain-English explanations first, so they survive the push's length limit.
            result.reasons = list(self.engine.notes) + list(result.reasons)
            self.engine.notes = []
        self.last_result = f"{request_id}: {result.outcome}"
        self.j.record(key or self.key(request_id), digest, result.outcome)
        self.j.audit(request_id, "OUTCOME", outcome=result.outcome, reasons=result.reasons[:5])
        self.status(result.outcome, {"request_id": request_id, "version": VERSION,
                                     "dry_run": self.o.dry_run})
        approval.inform(self.ha, self.o.notify_service, f"Deployer: {result.outcome}",
                        f"{request_id}\n" + "\n".join(str(r)[:300] for r in result.reasons[:3]))
        if issue:
            try:
                self.gh.comment(issue, report_markdown(request_id, result, self.o.dry_run))
            except net.NetError as err:
                self.j.audit(request_id, "REPORT_FAILED", error=str(err)[:200])

    def recover(self, head: str) -> None:
        engine = self.ensure_engine()

        def manifest_for(rid: str):
            try:
                if rid.endswith(undo.SUFFIX):
                    record = self.j.install(rid[: -len(undo.SUFFIX)])
                    return undo.build(record)[0] if record else None
                return self._manifest_at(rid, head)
            except Exception:  # noqa: BLE001
                return None

        recovered = engine.recover(manifest_for)
        if recovered:
            rid, result = recovered
            m = manifest_for(rid)
            self.finish(rid, m.digest if m else "unknown", m.tracking_issue if m else None, result)
            if result.outcome == FAILED_MANUAL:
                self.j.freeze(rid)

    def _lookup(self, request_id: str, raw: bytes, digest: str) -> None:
        try:
            lr = parse_lookup(raw)
        except ManifestError as err:
            self.finish(request_id, digest, None, Result(REFUSED, [f"lookup: {err.code}"]), key=request_id)
            return
        if lr.request_id != request_id:
            self.finish(request_id, digest, lr.tracking_issue,
                        Result(REFUSED, ["request_id does not match its directory"]), key=request_id)
            return
        limited = self._limit_reason(request_id, lookup=True)
        if limited:
            self.j.audit(request_id, "RATE_LIMITED")
            self.diag.add(limited)
            self._announce_hold(limited, lr.tracking_issue)
            self._offer_lift()
            return
        result = run_lookup(self.ensure_engine(), lr)
        self.finish(request_id, lr.digest, lr.tracking_issue, result, key=request_id)

    def frozen(self) -> str | None:
        rid = self.j.frozen_by()
        if rid and self.o.clear_freeze_for == rid and not self.j.load_txn():
            self.j.clear_freeze()
            self.j.audit(rid, "FREEZE_CLEARED_BY_OWNER")
            return None
        return rid

    # ------------------------------------------------------------------ diagnostics (0.3.1)
    def _limit_reason(self, request_id: str, lookup: bool = False) -> diagnose.Reason | None:
        """Daily limit (0.3.2): counts installs (first approval of a request); lookups have their own
        allowance; restart approvals never count; the owner's "Allow more today" lifts it until midnight."""
        if self.j.limit_lifted_until() > time.time():
            return None
        times = self.j.approval_times_since(86400, LOOKUP_STAGES if lookup else INSTALL_STAGES)
        limit = self.o.max_approval_requests_per_day
        if len(times) < limit:
            return None
        frees_at = sorted(times)[len(times) - limit] + 86400
        return diagnose.rate_limited(request_id, len(times), limit, frees_at, lookup)

    def _offer_lift(self) -> None:
        if self.board is None:
            return
        try:
            owner = self.ensure_engine().s.owner_user_id
        except Exception:  # noqa: BLE001 - no owner id, no button (the reason is shown anyway)
            return
        self.board.offer_lift(owner)

    def _run_requested_undo(self) -> bool:
        """0.3.3: run an owner-tapped Undo (one per poll). Returns True if one ran."""
        rid = self.board.take_undo() if self.board is not None else None
        if not rid:
            return False
        record = self.j.install(rid)
        undo_id = rid + undo.SUFFIX
        if record is None:
            self.finish(undo_id, "undo", None, Result(REFUSED, ["no install record for this request"]), key=undo_id)
            return True
        LOG.info("owner requested undo of %s", rid)
        result = self.ensure_engine().run_undo(record)
        self.finish(undo_id, "undo:" + str(record.get("at")), record.get("tracking_issue") or None, result,
                    key=undo_id)
        if result.outcome == FAILED_MANUAL:
            self.j.freeze(undo_id)
        return True

    def _offer_undo(self) -> None:
        if self.board is None or self.o.dry_run:
            return
        try:
            owner = self.ensure_engine().s.owner_user_id
            self.board.set_undo_offers(owner, undo.offers(self.j, self.pk))
        except Exception:  # noqa: BLE001 - no owner/unreadable files -> no Undo buttons this time
            self.board.set_undo_offers("", [])

    def _apply_lift(self) -> None:
        by = self.board.take_lift() if self.board is not None else None
        if not by:
            return
        until = diagnose.next_local_midnight(time.time())
        self.j.lift_limit(until, by)
        LOG.info("daily limit lifted by the owner until %s", diagnose.local_time(until))

    def _announce_hold(self, reason: diagnose.Reason, issue: int | None) -> None:
        """Tell the owner (push) and the AIs (tracking issue) once per hold, never again."""
        if self.j.noted(reason.key):
            return
        self.j.note(reason.request_id, reason.key)
        approval.inform(self.ha, self.o.notify_service, "House Brain Deployer: waiting", reason.line())
        if issue:
            body = "\n".join([
                "### House Brain Deployer: request held (not a failure)", "",
                f"- **Request:** `{reason.request_id or 'all requests'}`",
                f"- **Why:** {reason.text}",
                f"- **What to do:** {reason.fix}" if reason.fix else "",
                f"- **Deployer:** `{VERSION}`", "",
                "_Posted once by House Brain Deployer (machine-generated)._"])
            try:
                self.gh.comment(issue, net.redact(body))
            except net.NetError as err:
                self.j.audit(reason.request_id, "REPORT_FAILED", error=str(err)[:200])

    def health(self) -> dict:
        last = self.diag.last_check
        lifted = self.j.limit_lifted_until()
        return {
            "version": VERSION, "dry_run": self.o.dry_run,
            "last_check": diagnose.local_time(last) if last else "never",
            "next_check": diagnose.local_time(last + self.o.poll_seconds) if last else "soon",
            "reasons": self.diag.lines() or ["All clear: nothing is held and no error."],
            "installs_asked_last_24h": len(self.j.approval_times_since(86400, INSTALL_STAGES)),
            "lookups_asked_last_24h": len(self.j.approval_times_since(86400, LOOKUP_STAGES)),
            "approval_request_limit": self.o.max_approval_requests_per_day,
            "limit_lifted_until": diagnose.local_time(lifted) if lifted > time.time() else "not lifted",
            "last_result": self.last_result or "none since start",
            # Credential Autopilot (0.3.6): dates and reason codes only; read by the Maintenance App's
            # daily credential check (sensor.house_brain_deployer_status), never a token or the key.
            "github_auth": self.gh.auth.summary() if hasattr(self.gh, "auth") else {},
        }

    def _close_poll(self) -> None:
        due = self.diag.end(time.time())
        h = self.health()
        self.status(self.diag.state(), h)
        if self.board is not None:
            self.board.set_health(h["last_check"], self.diag.lines(), h["last_result"])
            if not self.j.load_txn():
                self._offer_undo()
            if not any(r.code == "DAILY_APPROVAL_LIMIT" for r in self.diag.reasons):
                lifted = self.j.limit_lifted_until()
                self.board.withdraw_lift(f"Daily limit lifted until {diagnose.local_time(lifted)}."
                                         if lifted > time.time() else "")
        for reason in due:   # an error that persisted ~15 minutes: one push until it clears
            approval.inform(self.ha, self.o.notify_service, "House Brain Deployer: problem", reason.line())

    # -- 0.3.7 (R2): never two Deployers at once -------------------------------------------------
    OTHER_ACTIVE_SECONDS = 900

    def standby_reason(self) -> str:
        """Non-empty while ANOTHER Deployer (the local one before the store switch-over, or the reverse) wrote the
        status in the last 15 minutes and did not say STOPPED. This one then does nothing and publishes nothing."""
        if not ha_mod.INSTANCE or self.j.load_txn():
            return ""
        try:
            st = self.ha.get_state(ha_mod.STATUS_ENTITY)
        except Exception:  # noqa: BLE001 - unreadable: Core may be restarting; decide on the next poll
            return "the status entity cannot be read right now" if self.in_standby else ""
        if not st:
            return ""
        attrs = st.get("attributes") or {}
        other = attrs.get("instance")
        if other == ha_mod.INSTANCE or str(st.get("state") or "").upper() in ("STOPPED", "STANDBY"):
            return ""
        updated = _iso_epoch(str(st.get("last_updated") or ""))
        if updated is None or time.time() - updated > self.OTHER_ACTIVE_SECONDS:
            return ""
        who = other if isinstance(other, str) and ha_mod.RE_INSTANCE.fullmatch(other) else "an older version"
        return (f"Standby: another House Brain Deployer ({who}) is active. This one starts by itself within 15 "
                "minutes after that one stops (the Maintenance App's switch-over does that).")

    def poll_once(self) -> None:
        reason = self.standby_reason()
        if reason:
            if not self.in_standby:
                LOG.info("%s", reason)
            self.in_standby = True
            if self.board is not None:
                self.board.set_health(diagnose.local_time(time.time()), [reason], self.last_result or "standby")
            return
        if self.in_standby:
            LOG.info("standby ended: this Deployer is in charge now")
        self.in_standby = False
        self.diag.begin()
        try:
            self._poll()
        except Stopping:
            raise                 # 0.3.5 (P3): not an error; main() logs it and exits
        except Exception as err:
            self.diag.add(diagnose.classify(err, branch=self.o.requests_branch, repo=self.o.github_repo,
                                            notify_service=self.o.notify_service))
            raise
        finally:
            self._close_poll()

    def _poll(self) -> None:
        self._apply_lift()
        if self.o.dry_run:
            self.diag.add(diagnose.dry_run())
        head = self.gh.branch_head(self.o.requests_branch)
        txn = self.j.load_txn()
        if txn:
            self.diag.add(diagnose.recovering(str(txn.get("request_id") or "")))
            self.recover(head)
            return
        rid_frozen = self.frozen()
        if rid_frozen:
            reason = diagnose.frozen(rid_frozen)
            self.diag.add(reason)
            self._announce_hold(reason, None)
            return
        if self._run_requested_undo():
            return  # at most one change per poll
        ledger = self.j.ledger()
        ids = [r for r in self.gh.list_request_ids(head) if RE_REQUEST_ID.fullmatch(r)]
        for request_id in ids[:MAX_REQUESTS_PER_POLL]:
            try:
                raw = self.gh.read_file(f"{REQUESTS_DIR}/{request_id}/manifest.json", head)
            except net.NetError as err:
                self.diag.add(diagnose.unreadable(request_id, err))
                continue
            import hashlib

            digest = hashlib.sha256(raw).hexdigest()
            lookup = is_lookup(raw)
            key = request_id if lookup else self.key(request_id)  # lookups are read-only in both modes
            seen = ledger.get(key)
            if seen:
                # Idempotent: a processed id is never run again. A changed manifest under a
                # used id is refused (logged once); the AI must use a new request_id.
                if seen.get("digest") != digest and seen.get("reuse_digest") != digest:
                    self.j.mark_reuse(key, digest)
                    self.j.audit(request_id, "ID_REUSE_REFUSED", new_digest=digest)
                if seen.get("digest") != digest:
                    reason = diagnose.id_reused(request_id)
                    self.diag.add(reason)
                    self._announce_hold(reason, _issue_of(raw))
                continue
            self.diag.checked_requests += 1
            if lookup:
                self._lookup(request_id, raw, digest)
                return  # at most one request per poll
            try:
                manifest = parse(raw)
            except ManifestError as err:
                self.finish(request_id, digest, None, Result(REFUSED, [f"manifest: {err.code}"]))
                continue
            if manifest.request_id != request_id:
                self.finish(request_id, digest, manifest.tracking_issue,
                            Result(REFUSED, ["request_id does not match its directory"]))
                continue
            if not any(fnmatchcase(manifest.source_ref, g) for g in self.o.source_ref_allowlist):
                self.finish(request_id, digest, manifest.tracking_issue,
                            Result(REFUSED, ["source_ref not in allowlist"]))
                continue
            limited = self._limit_reason(request_id)
            if limited:
                self.j.audit(request_id, "RATE_LIMITED")
                self.diag.add(limited)
                self._announce_hold(limited, manifest.tracking_issue)
                self._offer_lift()
                return  # try again on a later poll; not recorded in the ledger
            engine = self.ensure_engine()
            result = engine.run(manifest)
            self.finish(request_id, manifest.digest, manifest.tracking_issue, result)
            if result.outcome == FAILED_MANUAL:
                self.j.freeze(request_id)  # persists across restarts until the owner clears it
            return  # at most one request per poll


def _issue_of(raw: bytes) -> int | None:
    try:
        issue = json.loads(raw).get("tracking_issue")
    except (ValueError, AttributeError):
        return None
    return issue if isinstance(issue, int) and not isinstance(issue, bool) and 0 < issue < 10**7 else None


HANDOFF_PORT = 8096                 # R2 (owner 2026-10-07): the GitHub hand-off address; config.yaml "ports"


def handoff_port() -> int:
    if os.environ.get("HBD_TEST_MODE") == "1":
        return int(os.environ.get("HBD_HANDOFF_PORT", "0"))
    return HANDOFF_PORT


def build_service(options_path: str, data_dir: str) -> Service:
    opts = load_options(options_path)
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        raise OptionsError("SUPERVISOR_TOKEN missing")
    sup = os.environ.get("HBD_SUPERVISOR_URL", "http://supervisor")
    ws = os.environ.get("HBD_CORE_WS_URL", "ws://supervisor/core/websocket")
    api = os.environ.get("HBD_GITHUB_API", "https://api.github.com")
    packages = os.environ.get("HBD_PACKAGES_ROOT", "/homeassistant/packages")
    ha = HomeAssistant(sup, token, ws)
    store = ghauth.KeyStore(data_dir)
    provider = ghauth.TokenProvider(store, opts.github_repo, GITHUB_PERMISSIONS, api, f"house-brain-deployer/{VERSION}")
    auth = ghauth.Auth(opts.github_auth, opts.github_token, provider, retire=opts.retire_old_token, api=api,
                       user_agent=f"house-brain-deployer/{VERSION}")
    gh = GitHub(opts.github_repo, opts.github_token, api, auth=auth)
    timing = Timing(approval_timeout=opts.approval_timeout_minutes * 60.0,
                    restart_approval_timeout=opts.restart_approval_timeout_minutes * 60.0)
    if os.environ.get("HBD_TEST_MODE") == "1":
        scale = float(os.environ.get("HBD_TEST_TIME_SCALE", "1"))
        timing = Timing(approval_timeout=timing.approval_timeout * scale,
                        restart_approval_timeout=timing.restart_approval_timeout * scale,
                        precondition_wait=900 * scale, running_timeout=900 * scale,
                        poll=max(0.05, 10 * scale), settle_scale=scale)
    service = Service(opts, ha, gh, Packages(packages), Journal(data_dir), timing,
                      board=ApprovalBoard(), open_url=PANEL_URL)
    service.connector = ghpage.Connector(
        app_title="House Brain Deployer", bot_name="House Brain Deployer Bot",
        bot_description="House Brain Deployer App on the owner's Home Assistant: reads deploy/requests and "
                        "comments results. Created by the App's Connect button.",
        repo=opts.github_repo, permissions=GITHUB_PERMISSIONS, api=api,
        user_agent=f"house-brain-deployer/{VERSION}", store=store, auth=auth,
        handoff_port=handoff_port(),
        owner_id=OwnerId(lambda: approval.resolve_owner(ha, opts.owner_username)))
    return service


def start_ingress(service: Service) -> IngressServer | None:
    """Serve the approval page to the Supervisor ingress proxy only."""
    if service.board is None:
        return None
    port, peer = 8099, INGRESS_PEER
    if os.environ.get("HBD_TEST_MODE") == "1":
        port = int(os.environ.get("HBD_INGRESS_PORT", "8099"))
        peer = os.environ.get("HBD_INGRESS_PEER", INGRESS_PEER)
    server = IngressServer(service.board, port=port, allowed_peer=peer,
                           connector=getattr(service, "connector", None))
    server.start()
    return server


def poll_safely(service: Service, last_error: list[str]) -> bool:
    """One poll; the loop survives transient faults. False = stop requested while waiting (0.3.5, P3)."""
    try:
        if service.gh.auth.maybe_retire() == "RETIRED":       # R2: no token in the log, ever
            LOG.info("the old hand-made GitHub token was retired on GitHub (the GitHub App signs in now)")
        service.poll_once()
    except Stopping as err:
        LOG.warning("stop requested while waiting (%s); the transaction is left for recovery on next start", err)
        return False
    except Exception as err:  # noqa: BLE001 - loop must survive transient faults
        txn = service.j.load_txn()
        if txn:   # 0.3.5 (P2): the next poll's recovery names this as an internal error
            service.j.audit(str(txn.get("request_id") or "-"), "TXN_EXCEPTION", type=type(err).__name__,
                            phase=txn.get("phase"), run=service.run_id)
        # The plain-English reason is logged by the tracker (on change) and published in the status.
        message = net.redact(f"{type(err).__name__}: {err}")[:300]
        if message != last_error[0]:
            LOG.warning("poll failed: %s", message)
        last_error[0] = message
    else:
        last_error[0] = ""
    return True


def main() -> int:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(net.RedactingFilter())
    logging.basicConfig(level=logging.INFO, handlers=[handler],
                        format="%(asctime)s %(levelname)s %(message)s")
    options_path = os.environ.get("HBD_OPTIONS", "/data/options.json")
    data_dir = os.environ.get("HBD_DATA", "/data")
    try:
        service = build_service(options_path, data_dir)
    except (OptionsError, ValueError, OSError) as err:
        LOG.error("configuration refused: %s", err)
        return 2
    try:
        start_ingress(service)
    except OSError as err:  # the push buttons still work without the page
        LOG.warning("approval page unavailable: %s", err)
    LOG.info("House Brain Deployer %s started (dry_run=%s)", VERSION, service.o.dry_run)
    o = service.o
    LOG.info("settings: repo=%s branch=%s sources=%s notify=%s owner=%s check every %ss; daily limit: "
             "%s of %s installs asked in the last 24 h", o.github_repo, o.requests_branch,
             ",".join(o.source_ref_allowlist), o.notify_service, o.owner_username, o.poll_seconds,
             len(service.j.approval_times_since(86400, INSTALL_STAGES)), o.max_approval_requests_per_day)
    service.status("STARTED", {"version": VERSION, "dry_run": service.o.dry_run})
    service.j.audit("-", "APP_STARTED", version=VERSION, boot_id=service.boot_id, run=service.run_id)

    def _term(signum, frame):  # noqa: ARG001
        if not service.stop:
            # 0.3.5 (P1): record the stop and the phase it hit, so a later recovery can say why
            try:
                txn = service.j.load_txn() or {}
                service.j.audit(str(txn.get("request_id") or "-"), "STOP_REQUESTED",
                                phase=txn.get("phase"), run=service.run_id)
            except Exception:  # noqa: BLE001, S110 - a stop must never fail on its own record
                pass
        service.stop = True

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    once = os.environ.get("HBD_ONCE") == "1"
    last_error = [""]
    while not service.stop:
        if not poll_safely(service, last_error):
            break
        if once:
            break
        deadline = time.monotonic() + service.o.poll_seconds
        while not service.stop and time.monotonic() < deadline:
            time.sleep(1)
    if not service.in_standby:
        # 0.3.7: say STOPPED (never while a transaction is pending: status() keeps the marker), so a store
        # Deployer waiting in standby takes over at once instead of after 15 minutes.
        service.status("STOPPED", {"version": VERSION, "dry_run": service.o.dry_run})
    if service.engine is None or not service.j.load_txn():
        LOG.info("stopped")
        return 0
    LOG.warning("stopped with a transaction pending; it will be recovered on next start")
    return 0


if __name__ == "__main__":
    sys.exit(main())
