"""Entrypoint: options, poll loop, rate limits, ledger, reporting."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import re
import signal
import sys
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from fnmatch import fnmatchcase
from functools import wraps

from . import (
    VERSION,
    approval,
    diagnose,
    ghauth,
    ghpage,
    http_cleanup,
    legacy_migrate,
    net,
    release_status,
    selfreport,
    undo,
    writer_enroll,
    writer_probe,
)
from . import ha as ha_mod
from .engine import FAILED_MANUAL, REFUSED, Engine, Result, Settings, Stopping, Timing
from .fsops import Packages
from .github import RE_REPO, REQUESTS_DIR, GitHub
from .ha import HomeAssistant
from .journal import INSTALL_STAGES, LOOKUP_STAGES, Journal
from .lookup import is_lookup, parse_lookup, run_lookup
from .manifest import RE_REQUEST_ID, Manifest, ManifestError, parse
from .migration.deployer_startup_service import startup_hold
from .reask import check_ledger, is_reask, parse_reask
from .web import INGRESS_PEER, ApprovalBoard, IngressServer
from .migration.deployer_working_journal import WorkingError
from .writer import WriterError, WriterLease


def _writer_guarded(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self._writer_operation():
            return method(self, *args, **kwargs)
    return guarded


LOG = logging.getLogger("hbd")
RE_NOTIFY = re.compile(r"^mobile_app_[a-z0-9_]{1,80}$")
RE_USERNAME = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")
RE_BRANCH_GLOB = re.compile(r"^[A-Za-z0-9._/*-]{1,100}$")
# Sidebar panel of this App (Core registers ingress panels at /<app slug>). The iOS Companion
# app opened the plain relative path in Safari (live 0.2.0), so use its documented in-app
# deep link (homeassistant://navigate/<path>, URL handler docs) instead.
# 0.3.14 (ported from #985, credited): the slug is derived from THIS App's container hostname. A store
# install is <8-hex>_house_brain_deployer; the old fixed local_house_brain_deployer link opened the
# stopped legacy copy's panel instead of the running Deployer's.
RE_DEPLOYER_HOSTNAME = re.compile(r"(?:local|[0-9a-f]{8})-house-brain-deployer")


def panel_url(instance: str) -> str | None:
    """Route to THIS Deployer's ingress panel, never a guessed/legacy instance.

    Core maps an add-on slug to its hostname by replacing underscores with dashes
    and registers its panel under the original slug. Only this App's two known
    slug shapes are reversible; a generic container hostname is not an App slug.
    Keep the proven Companion deep-link format and default-server selection.
    An unknown identity leaves native approval actions / Open Web UI available.
    """
    if not isinstance(instance, str) or not RE_DEPLOYER_HOSTNAME.fullmatch(instance):
        return None
    return f"homeassistant://navigate/{instance.replace('-', '_')}?server=default"


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
    return_to_previous_history: bool = False  # 0.3.23 (#985): emergency return from the moved history


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
    return_previous = raw.get("return_to_previous_history", False)
    if not isinstance(return_previous, bool):
        raise OptionsError("return_to_previous_history")
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
        return_to_previous_history=return_previous,
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
    if result.facts.get("reask_of"):   # 0.3.8: a re-ask links its original request
        lines.append(f"- **Re-ask of:** `{result.facts['reask_of']}` (that request's approval expired unanswered)")
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
                 open_url: str | None = None,
                 writer_factory: Callable[[], WriterLease] | None = None,
                 expected_owner_id: str | None = None) -> None:
        self.o = opts
        self.ha = ha
        self.gh = gh
        self.pk = packages
        self.j = journal
        self.timing = timing
        self.board = board
        self.open_url = open_url
        # Staged binding only: normal build_service does not enable this until
        # both Apps' shared-mount/UID/confinement contract is qualified.
        self.writer_factory = writer_factory
        self.expected_owner_id = expected_owner_id
        self.writer_lease: WriterLease | None = None
        self.writer_busy = False
        self.contained: str | None = None   # D6(a): set once the migrated runtime refuses; this run stays held
        self.pending_undo: str | None = None
        if writer_factory is not None:
            self.pk.mutation_guard = self._check_writer
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
            manifest_digest = release_status.manifest_identity()
            self.board.release_probe = lambda: release_status.observations(
                self.j, manifest_digest, self.in_standby)

    def ensure_engine(self) -> Engine:
        if self.engine is None:
            owner_id = approval.resolve_owner(self.ha, self.o.owner_username)
            if self.expected_owner_id is not None and owner_id != self.expected_owner_id:
                raise WriterError("WRITER_OWNER_CHANGED")
            settings = Settings(notify_service=self.o.notify_service, owner_user_id=owner_id,
                                dry_run=self.o.dry_run, require_auth=self.o.require_phone_unlock,
                                timing=self.timing, board=self.board, open_url=self.open_url,
                                should_stop=lambda: self.stop, run_id=self.run_id, boot_id=self.boot_id,
                                started_at=self.started_at)
            self.engine = Engine(self.ha, self.gh, self.pk, self.j, settings)
        return self.engine

    def _check_writer(self) -> None:
        if self.writer_lease is None:
            raise WriterError("WRITER_NOT_HELD")
        self.writer_lease.check()

    @contextmanager
    def _writer_operation(self):
        if self.writer_factory is None:
            yield
            return
        try:
            with self.writer_factory() as lease:
                self.writer_lease = lease
                yield
                lease.check()
        except WriterError as err:
            self.writer_busy = str(err) == "WRITER_BUSY"
            if not self.writer_busy:
                # A lost writer cannot read or freeze through a migrated Journal.
                # Retain pending bytes and contain this process in memory.
                self.contained = str(err)[:120] or "WRITER_UNSAFE"
            raise
        except WorkingError as err:
            # D6(a): the migrated runtime refused or was revoked. Contain in memory for the rest of this run.
            # Never freeze or write through the selected shared Journal without a fresh permit: that would
            # bypass the writer. A durable containment marker needs the separate private-state boundary.
            self.contained = str(err)[:120] or "FORWARD_HELD"
            raise
        finally:
            self.writer_lease = None

    def _manifest_at(self, request_id: str, ref: str):
        raw = self.gh.read_file(f"{REQUESTS_DIR}/{request_id}/manifest.json", ref)
        if is_reask(raw):   # 0.3.8: recovery of a re-ask resolves to its original (digest checked by the engine)
            rq = parse_reask(raw)
            original = parse(self.gh.read_file(f"{REQUESTS_DIR}/{rq.reask_of}/manifest.json", ref))
            return self._reask_run(original, rq.request_id, rq.tracking_issue, rq.reask_of)
        return parse(raw)

    @staticmethod
    def _reask_run(original: Manifest, request_id: str, issue: int | None, reask_of: str) -> Manifest:
        """0.3.8: the ORIGINAL manifest's content under the re-ask's id (journal, approval, install record and
        result all belong to the re-ask). Files, hashes, checks and digest are the original's, unchanged."""
        return dataclasses.replace(original, request_id=request_id, tracking_issue=issue or original.tracking_issue,
                                   reask_of=reask_of)

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
        if self.writer_factory is None:
            self.ha.publish_status(state, attributes)
        elif self.writer_lease is not None:
            self._check_writer()
            self.ha.publish_status(state, attributes)
        else:
            # Reads do not hold the writer lease. Only the shared status update
            # takes a short lease, so it cannot erase another writer's marker.
            try:
                with self._writer_operation():
                    self._check_writer()
                    self.ha.publish_status(state, attributes)
            except WriterError as err:
                self.writer_busy = True
                self.diag.add(diagnose.classify(err, branch=self.o.requests_branch,
                                                repo=self.o.github_repo,
                                                notify_service=self.o.notify_service))

    def finish(self, request_id: str, digest: str, issue: int | None, result: Result,
               key: str | None = None, writer_result: bool = False) -> None:
        if self.writer_factory is not None and (writer_result or self.writer_lease is not None):
            self._check_writer()
        if self.engine is not None and self.engine.notes:
            # Plain-English explanations first, so they survive the push's length limit.
            result.reasons = list(self.engine.notes) + list(result.reasons)
            self.engine.notes = []
        self.last_result = f"{request_id}: {result.outcome}"
        self.j.record(key or self.key(request_id), digest, result.outcome)
        if result.restart_timed_out:   # 0.3.11: lets a chat re-ask it (reask.reaskable)
            self.j.annotate(key or self.key(request_id), restart_timed_out=True)
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

    @_writer_guarded
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
            if m is not None and m.reask_of:
                self._finish_reask(rid, self._raw_digest(rid, head), m.tracking_issue, result, m.reask_of,
                                   writer_result=True)
            else:
                self.finish(rid, m.digest if m else "unknown", m.tracking_issue if m else None, result,
                            writer_result=True)
            if result.outcome == FAILED_MANUAL:
                self.j.freeze(rid)

    def _raw_digest(self, request_id: str, ref: str) -> str:
        try:
            return hashlib.sha256(self.gh.read_file(f"{REQUESTS_DIR}/{request_id}/manifest.json", ref)).hexdigest()
        except Exception:  # noqa: BLE001 - recorded as unknown; the id is final either way
            return "unknown"

    # ------------------------------------------------------------------ re-ask (0.3.8)
    def _finish_reask(self, request_id: str, digest: str, issue: int | None, result: Result, reask_of: str,
                      writer_result: bool = False) -> None:
        result.facts = dict(result.facts or {})
        result.facts["reask_of"] = reask_of
        self.finish(request_id, digest, issue, result, writer_result=writer_result)
        self.j.annotate(self.key(request_id), reask_of=reask_of)

    def _reask_refused(self, request_id: str, digest: str, reask_of: str, code: str, detail: str,
                       issue: int | None) -> None:
        reason = diagnose.reask_refused(code, request_id, reask_of, detail)
        self.j.audit(request_id, "REASK_REFUSED", code=code, reask_of=reask_of)
        text = f"{code}: {reason.text}" + (f" What to do: {reason.fix}" if reason.fix else "")
        self._finish_reask(request_id, digest, issue, Result(REFUSED, [text]), reask_of)

    @_writer_guarded
    def _reask(self, request_id: str, raw: bytes, digest: str, head: str) -> bool:
        """0.3.8 (owner pop-up 2026-10-08): ask a TIMED_OUT request again, by reference. Returns True when the
        poll must end (an approval was asked or the daily limit holds it). Never approves anything itself."""
        try:
            rq = parse_reask(raw)
        except ManifestError as err:
            self.finish(request_id, digest, None, Result(REFUSED, [f"reask: {err.code}"]))
            return False
        if rq.request_id != request_id:
            self.finish(request_id, digest, rq.tracking_issue,
                        Result(REFUSED, ["request_id does not match its directory"]))
            return False
        original_raw: bytes | None = None
        try:   # re-read at the current branch head (it is pinned to the ledger digest below)
            original_raw = self.gh.read_file(f"{REQUESTS_DIR}/{rq.reask_of}/manifest.json", head)
        except net.NetError as err:
            if err.status != 404:
                self.diag.add(diagnose.unreadable(request_id, err))
                return False
        # Results go to the re-ask's tracking issue, else the original's.
        issue = rq.tracking_issue or (_issue_of(original_raw) if original_raw is not None else None)
        ledger = self.j.ledger()
        refused = check_ledger(ledger, rq)
        if refused:
            self._reask_refused(request_id, digest, rq.reask_of, refused[0], refused[1], issue)
            return False
        if original_raw is None:
            self._reask_refused(request_id, digest, rq.reask_of, "REASK_ORIGINAL_CHANGED", "no longer there", issue)
            return False
        if hashlib.sha256(original_raw).hexdigest() != ledger[rq.reask_of].get("digest"):
            self._reask_refused(request_id, digest, rq.reask_of, "REASK_ORIGINAL_CHANGED", "changed", issue)
            return False
        if is_lookup(original_raw):
            self._reask_refused(request_id, digest, rq.reask_of, "REASK_LOOKUP", "", issue)
            return False
        try:
            original = parse(original_raw)
        except ManifestError as err:
            self._finish_reask(request_id, digest, issue, Result(REFUSED, [f"manifest: {err.code}"]), rq.reask_of)
            return False
        if not any(fnmatchcase(original.source_ref, g) for g in self.o.source_ref_allowlist):
            self._finish_reask(request_id, digest, issue, Result(REFUSED, ["source_ref not in allowlist"]),
                               rq.reask_of)
            return False
        limited = self._limit_reason(request_id)
        if limited:
            self.j.audit(request_id, "RATE_LIMITED")
            self.diag.add(limited)
            self._announce_hold(limited, issue)
            self._offer_lift()
            return True   # try again on a later poll; not recorded in the ledger
        if not self.o.dry_run:   # practice-mode re-asks change nothing and never use up a re-ask
            self.j.link_reask(rq.reask_of, request_id)
        self.j.audit(request_id, "REASK", reask_of=rq.reask_of, requested_by=rq.requested_by)
        result = self.ensure_engine().run(self._reask_run(original, request_id, issue, rq.reask_of))
        self._finish_reask(request_id, digest, issue, result, rq.reask_of, writer_result=True)
        if result.outcome == FAILED_MANUAL:
            self.j.freeze(request_id)
        return True

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

    def _http_inspect(self, request_id: str, raw: bytes, digest: str) -> None:
        try:
            m = parse(raw)
            if m.request_id != request_id or m.http_operation != "inspect":
                raise ManifestError("HTTP_REQUEST_ID")
        except ManifestError as err:
            self.finish(request_id, digest, None, Result(REFUSED, [err.code]), key=request_id)
            return
        limited = self._limit_reason(request_id, lookup=True)
        if limited:
            self.diag.add(limited)
            self._announce_hold(limited, m.tracking_issue)
            return
        engine = self.ensure_engine()
        decision = engine._ask(m, "LOOKUP", "House Brain: inspect HTTP migration?",
                               "Read only configuration.yaml and confirmed HTTP UI settings locally. "
                               "Return hashes and readiness only; no file contents, changes or restart.",
                               self.timing.approval_timeout)
        if decision != approval.APPROVE:
            result = Result("REJECTED" if decision == approval.REJECT else "TIMED_OUT", [decision])
        else:
            try:
                _, facts = http_cleanup.inspect(self.ha, self.pk)
                result = Result("HTTP_INSPECTION_OK", [], facts)
            except Exception as err:  # noqa: BLE001 - no config/error text may leave the house
                code = str(err) if isinstance(err, http_cleanup.CleanupError) else type(err).__name__
                result = Result(REFUSED, [f"HTTP_INSPECTION:{code}"])
        self.finish(request_id, digest, m.tracking_issue, result, key=request_id)

    def _writer_enroll(self, request_id: str, raw: bytes, digest: str) -> None:
        """0.3.19 (owner pop-up 2026-10-12, #985 plan R1 E3): create the shared writer lock once."""
        from .writer import WriterError, enroll_writer
        try:
            rid, issue = writer_enroll.parse(raw)
            if rid != request_id:
                raise ManifestError("ENROLL_REQUEST_ID")
        except ManifestError as err:
            self.finish(request_id, digest, None, Result(REFUSED, [err.code]), key=request_id)
            return
        limited = self._limit_reason(request_id, lookup=True)
        if limited:
            self.diag.add(limited)
            self._announce_hold(limited, issue)
            return
        engine = self.ensure_engine()
        target = writer_enroll.root()
        decision = engine._ask(request_id, "LOOKUP", "House Brain: create the shared writer lock?",
                               f"Creates only the empty folder {target}/ (private) and its empty lock file, "
                               "so the Deployer and Maintenance can never write at the same time. Changes "
                               "nothing else. Anything already there in another shape is left as is.",
                               self.timing.approval_timeout)
        if decision != approval.APPROVE:
            result = Result("REJECTED" if decision == approval.REJECT else "TIMED_OUT", [decision])
        else:
            try:
                evidence = enroll_writer(target)
                LOG.info("HBD-NATIVE writer_enroll created=%s dir_ino=%s lock_ino=%s dev=%s.%s",
                         ",".join(evidence["created"]) or "none", evidence["directory"]["ino"],
                         evidence["lock"]["ino"], os.major(evidence["lock"]["dev"]), os.minor(evidence["lock"]["dev"]))
                result = Result("WRITER_ENROLLED", [], {"writer_enrollment": evidence})
            except WriterError as err:
                result = Result(REFUSED, [f"WRITER_ENROLL:{err}"])
            except Exception as err:  # noqa: BLE001 - fixed code only; no path text leaves the house
                result = Result(REFUSED, [f"WRITER_ENROLL:{type(err).__name__}"])
        self.finish(request_id, digest, issue, result, key=request_id)

    def _writer_probe(self, request_id: str, raw: bytes, digest: str) -> None:
        """0.3.21 (owner pop-up 2026-10-12, #985 plan R1 E6): try or hold the shared writer lock, after Approve."""
        from .writer import WriterError
        try:
            rid, issue, operation = writer_probe.parse(raw)
            if rid != request_id:
                raise ManifestError("PROBE_REQUEST_ID")
        except ManifestError as err:
            self.finish(request_id, digest, None, Result(REFUSED, [err.code]), key=request_id)
            return
        limited = self._limit_reason(request_id, lookup=True)
        if limited:
            self.diag.add(limited)
            self._announce_hold(limited, issue)
            return
        engine = self.ensure_engine()
        hold = int(writer_probe.hold_seconds())
        what = ("try the shared writer lock once and let it go at once" if operation == "try" else
                f"hold the shared writer lock for {hold // 60} minutes" if hold >= 120 else
                f"hold the shared writer lock for {hold} seconds")
        decision = engine._ask(request_id, "LOOKUP", "House Brain: test the shared writer lock?",
                               f"The Deployer will {what}. While it holds the lock the Maintenance App cannot "
                               "write. Nothing is created, changed or deleted.",
                               self.timing.approval_timeout)
        if decision != approval.APPROVE:
            result = Result("REJECTED" if decision == approval.REJECT else "TIMED_OUT", [decision])
        else:
            try:
                evidence = writer_probe.run(writer_enroll.root(), operation, should_stop=lambda: self.stop)
                if evidence["result"] == "ACQUIRED":
                    LOG.info("HBD-NATIVE writer_probe op=%s result=ACQUIRED held=%s ino=%s dev=%s.%s", operation,
                             evidence["held_seconds"], evidence["ino"], os.major(evidence["dev"]),
                             os.minor(evidence["dev"]))
                    result = Result("WRITER_PROBE_ACQUIRED", [], {"writer_probe": evidence})
                else:
                    LOG.info("HBD-NATIVE writer_probe op=%s result=WRITER_BUSY", operation)
                    result = Result("WRITER_PROBE_BUSY", [], {"writer_probe": evidence})
            except WriterError as err:
                result = Result(REFUSED, [f"WRITER_PROBE:{err}"])
            except Exception as err:  # noqa: BLE001 - fixed code only; no path text leaves the house
                result = Result(REFUSED, [f"WRITER_PROBE:{type(err).__name__}"])
        self.finish(request_id, digest, issue, result, key=request_id)

    def _legacy(self, request_id: str, raw: bytes, digest: str) -> None:
        """0.3.23 (#985 plan M4/M5; owner pop-ups 2026-10-13): Prepare, Select or Unselect the moved history."""
        from .writer import WriterError
        try:
            schema, rid, issue, fields = legacy_migrate.parse(raw)
            if rid != request_id:
                raise ManifestError("LEGACY_REQUEST_ID")
        except ManifestError as err:
            self.finish(request_id, digest, None, Result(REFUSED, [err.code]), key=request_id)
            return
        limited = self._limit_reason(request_id, lookup=True)
        if limited:
            self.diag.add(limited)
            self._announce_hold(limited, issue)
            return
        engine = self.ensure_engine()
        data_dir, now = self.j.dir, time.time()
        runtime = getattr(self, "migrated_runtime", None)
        if (runtime is not None and schema == legacy_migrate.SELECT
                and runtime.scope.operation_id == legacy_migrate.make_scope(
                    fields["prepared_request_id"], runtime.scope.owner_id, 0).operation_id):
            # Review finding 2: the Select's own result was written after the merge, so the moved history does
            # not hold it. It is this run's selection: record it as applied instead of refusing it again.
            self.finish(request_id, digest, issue, Result("LEGACY_SELECTED", ["applied at this start"]),
                        key=request_id)
            return
        try:
            if runtime is not None:
                raise legacy_migrate.LegacyError("LEGACY_ALREADY_RUNNING_MOVED_HISTORY")
            if schema == legacy_migrate.PREPARE:
                shown = legacy_migrate.inspect_prepare(data_dir, fields["stage"], fields["snapshot_sha256"], now)
                c = shown["counts"]
                title = "House Brain: prepare the old Deployer's history?"
                message = (f"Copies the old Deployer's history from Maintenance's stage into the Deployer's private "
                           f"folder, merged with today's: {c['installs']} installs ({c['undone']} undone), "
                           f"{c['ledger']} results, {c['audit']} log lines, freeze {'yes' if c['frozen'] else 'no'}. "
                           f"Fingerprint {shown['planned'].result_sha256[:12]}. Nothing is selected yet; the old "
                           "Deployer and your packages are not changed.")
            elif schema == legacy_migrate.SELECT:
                if self.o.return_to_previous_history:
                    raise legacy_migrate.LegacyError("LEGACY_RETURN_OPTION_ON")
                shown = legacy_migrate.inspect_select(data_dir, fields["prepared_request_id"], fields["result_sha256"],
                                                      now)
                c, since = shown["counts"], shown["since"]
                title = "House Brain: use the moved history from the next start?"
                message = (f"From the next Deployer restart it runs on the moved history: {c['installs']} installs, "
                           f"{c['ledger']} results, {c['audit']} log lines (including {since['ledger']} results "
                           f"recorded since Prepare). Fingerprint {shown['planned'].result_sha256[:12]}. Until the "
                           "restart other requests wait. To undo before the restart, send Unselect; after it, turn "
                           "on return_to_previous_history.")
            else:
                if not legacy_migrate.selection_present(data_dir):
                    raise legacy_migrate.LegacyError("LEGACY_NOT_SELECTED")
                shown = None
                title = "House Brain: cancel the moved-history selection?"
                message = ("The selection file is renamed aside (kept). The next start keeps using the current "
                           "history. Nothing else changes.")
        except legacy_migrate.LegacyError as err:
            self.finish(request_id, digest, issue, Result(REFUSED, [str(err)]), key=request_id)
            return
        except Exception as err:  # noqa: BLE001 - fixed code only; no path text leaves the house
            self.finish(request_id, digest, issue, Result(REFUSED, [f"LEGACY:{type(err).__name__}"]), key=request_id)
            return
        decision = engine._ask(request_id, "LOOKUP", title, message, self.timing.approval_timeout)
        if decision != approval.APPROVE:
            result = Result("REJECTED" if decision == approval.REJECT else "TIMED_OUT", [decision])
        else:
            owner = engine.s.owner_user_id
            try:
                if schema == legacy_migrate.PREPARE:
                    facts = legacy_migrate.run_prepare(data_dir=data_dir, packages_root=self.pk.root,
                                                       request_id=request_id, stage=fields["stage"],
                                                       snapshot_sha256=fields["snapshot_sha256"], shown=shown,
                                                       owner_id=owner, should_stop=lambda: self.stop)
                    result = Result("LEGACY_PREPARED", [], {"legacy_prepare": facts})
                elif schema == legacy_migrate.SELECT:
                    facts = legacy_migrate.run_select(data_dir=data_dir, packages_root=self.pk.root,
                                                      request_id=request_id,
                                                      prepared_request_id=fields["prepared_request_id"],
                                                      result_sha256=fields["result_sha256"], shown=shown,
                                                      owner_id=owner, should_stop=lambda: self.stop)
                    result = Result("LEGACY_SELECTED", [], {"legacy_select": facts})
                else:
                    facts = legacy_migrate.run_unselect(data_dir=data_dir, marker_sha256=fields["marker_sha256"])
                    result = Result("LEGACY_UNSELECTED", [], {"legacy_unselect": facts})
                LOG.info("HBD-NATIVE legacy %s result=%s", schema.split("_")[3], result.outcome)
            except (legacy_migrate.LegacyError, WriterError) as err:
                result = Result("LEGACY_FAILED", [f"LEGACY:{err}"])
            except ValueError as err:   # StoreError / CutoverError / WorkingError: fixed codes
                result = Result("LEGACY_FAILED", [f"LEGACY:{type(err).__name__}:{str(err)[:60]}"])
            except Exception as err:  # noqa: BLE001 - fixed code only
                result = Result("LEGACY_FAILED", [f"LEGACY:{type(err).__name__}"])
        self.finish(request_id, digest, issue, result, key=request_id)

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
        rid = self.pending_undo or (self.board.take_undo() if self.board is not None else None)
        if not rid:
            return False
        self.pending_undo = rid
        with self._writer_operation():
            # Consume only after admission. Contention retains the owner's tap
            # for a later poll; restart persistence remains the existing board limit.
            self.pending_undo = None
            return self._execute_undo(rid)

    def _execute_undo(self, rid: str) -> bool:
        record = self.j.install(rid)
        undo_id = rid + undo.SUFFIX
        if record is None:
            self.finish(undo_id, "undo", None, Result(REFUSED, ["no install record for this request"]), key=undo_id)
            return True
        LOG.info("owner requested undo of %s", rid)
        result = self.ensure_engine().run_undo(record)
        self.finish(undo_id, "undo:" + str(record.get("at")), record.get("tracking_issue") or None, result,
                    key=undo_id, writer_result=True)
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
        if self.writer_busy:
            # The other instance may own the shared status marker. A contended
            # poll must not overwrite it, offer Undo or send a second push.
            if self.board is not None:
                self.board.set_health(h["last_check"], self.diag.lines(), h["last_result"])
            return
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
        if self.contained:
            # D6(a): nothing more runs in this App run; no Journal, status or GitHub effect.
            if self.board is not None:
                self.board.set_health(diagnose.local_time(time.time()),
                                      [diagnose.contained(self.contained)], self.last_result or "held")
            return
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
        self.writer_busy = False
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
        # 0.3.23: while a moved-history selection waits for the restart, nothing may change the live history it
        # left behind (review finding 1): a tapped Undo stays pending and runs after the restart.
        selected = getattr(self, "migrated_runtime", None) is None and legacy_migrate.selection_present(self.j.dir)
        if not selected and self._run_requested_undo():
            return  # at most one change per poll
        ledger = self.j.ledger()
        ids = [r for r in self.gh.list_request_ids(head) if RE_REQUEST_ID.fullmatch(r)]
        # 0.3.12: unhandled ids first, then handled ones (still read for the id-reuse check), then the
        # cap. Before, the cap came first, so an old backlog hid every request after the 10th folder.
        handled = {r for r in ids if r in ledger or self.key(r) in ledger}
        ids = [r for r in ids if r not in handled] + [r for r in ids if r in handled]
        for request_id in ids[:MAX_REQUESTS_PER_POLL]:
            try:
                raw = self.gh.read_file(f"{REQUESTS_DIR}/{request_id}/manifest.json", head)
            except net.NetError as err:
                self.diag.add(diagnose.unreadable(request_id, err))
                continue
            digest = hashlib.sha256(raw).hexdigest()
            http_inspection = http_cleanup.is_inspect(raw)
            enroll = writer_enroll.is_enroll(raw)   # 0.3.19: keyed and limited like a lookup
            probe = writer_probe.is_probe(raw)      # 0.3.21: likewise
            legacy = legacy_migrate.kind(raw)       # 0.3.23 (#985 M4/M5): likewise
            if selected and legacy != legacy_migrate.UNSELECT:
                # A selection waits for the next start; new results would not be in the moved history.
                self.diag.add(diagnose.Reason("LEGACY_SELECTED_WAITING", diagnose.HOLD,
                                              "The moved history is selected and takes effect at the next "
                                              "Deployer restart. Other requests wait until then.",
                                              "Restart the Deployer, or send an Unselect request first."))
                continue   # skipped, not recorded: an Unselect later in the list must still run
            lookup = is_lookup(raw) or http_inspection or enroll or probe or legacy is not None
            key = request_id if lookup else self.key(request_id)  # lookups are read-only in both modes
            seen = ledger.get(key)
            if seen:
                # Idempotent: a processed id is never run again. A changed manifest under a
                # used id is refused (logged once); the AI must use a new request_id. (0.3.8: a
                # TIMED_OUT request can be asked again only through a re-ask under a NEW id.)
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
                if enroll:
                    self._writer_enroll(request_id, raw, digest)
                elif probe:
                    self._writer_probe(request_id, raw, digest)
                elif legacy is not None:
                    self._legacy(request_id, raw, digest)
                elif http_inspection:
                    self._http_inspect(request_id, raw, digest)
                else:
                    self._lookup(request_id, raw, digest)
                return  # at most one request per poll
            if is_reask(raw):
                if self._reask(request_id, raw, digest, head):
                    return  # at most one request per poll
                continue
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
            with self._writer_operation():
                engine = self.ensure_engine()
                result = engine.run(manifest)
                self.finish(request_id, manifest.digest, manifest.tracking_issue, result, writer_result=True)
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


def build_service(options_path: str, data_dir: str, *, startup_provider=None) -> Service:
    opts = load_options(options_path)
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        raise OptionsError("SUPERVISOR_TOKEN missing")
    sup = os.environ.get("HBD_SUPERVISOR_URL", "http://supervisor")
    ws = os.environ.get("HBD_CORE_WS_URL", "ws://supervisor/core/websocket")
    api = os.environ.get("HBD_GITHUB_API", "https://api.github.com")
    packages = os.environ.get("HBD_PACKAGES_ROOT", "/homeassistant/packages")
    ha = HomeAssistant(sup, token, ws)
    timing = Timing(approval_timeout=opts.approval_timeout_minutes * 60.0,
                    restart_approval_timeout=opts.restart_approval_timeout_minutes * 60.0)
    # A selected migration never silently falls back to an empty legacy Journal.
    def settings_for():
        return Settings(opts.notify_service, approval.resolve_owner(ha, opts.owner_username),
                        dry_run=opts.dry_run, require_auth=opts.require_phone_unlock, timing=timing)
    if opts.return_to_previous_history:
        # 0.3.23 (#985, owner pop-up "Build both"): emergency return; works even when a selected start is held.
        withdrawn = legacy_migrate.withdraw_at_startup(data_dir)
        if withdrawn:
            LOG.warning("return_to_previous_history: the moved-history selection was renamed to %s; this start "
                        "uses the previous history. Turn the option off again.", withdrawn)
    owner_context = None
    if startup_provider is not None:
        runtime = startup_hold(data_dir, provider=startup_provider, ha=ha, settings_for=settings_for)
    else:
        # #985 (owner pop-up 2026-10-12): with a v2 selection marker and an enrolled writer lock, one
        # owner tap may recover the selected interrupted install. No marker: nothing new runs. Anything
        # missing: the provider is None and startup_hold keeps today's hold without asking anything.
        from .migration.deployer_owner_issuer import installed_owner_provider

        def ask(*, stage: str, title: str, message: str) -> approval.Decision:
            return approval.ask(ha, opts.notify_service, approval.resolve_owner(ha, opts.owner_username),
                                stage=stage, title=title, message=message, timeout=timing.approval_timeout,
                                require_auth=opts.require_phone_unlock)
        owner_context = installed_owner_provider(data_dir, ask)
        owner_provider = owner_context.__enter__()
        try:
            runtime = startup_hold(data_dir, provider=owner_provider, ha=ha, settings_for=settings_for)
        except BaseException:
            owner_context.__exit__(None, None, None)
            raise
        if runtime is None:
            owner_context.__exit__(None, None, None)
        else:
            # 0.3.23: the moved history runs normally; the runtime keeps the bound folders for its life.
            owner_context.keep_for_runtime()
    if runtime is None:
        return _construct_service(opts, ha, api, packages, data_dir, timing)
    try:
        with runtime.boundary():
            if packages != runtime.plan['packages_root']:
                raise ValueError('FORWARD_PACKAGES_CHANGED')
            if approval.resolve_owner(ha, opts.owner_username) != runtime.scope.owner_id:
                raise ValueError('FORWARD_OWNER_CHANGED')
            service = _construct_service(opts, ha, api, packages, data_dir, timing, runtime=runtime)
            runtime.bind_service(service)
        service.migrated_runtime = runtime
        return service
    except BaseException:
        runtime.close()
        raise
    finally:
        runtime.release_initial()
        if owner_context is not None:
            owner_context.__exit__(None, None, None)   # releases the startup lock; Maintenance may write again


def _construct_service(opts, ha, api, packages, data_dir, timing, *, runtime=None):
    store = ghauth.KeyStore(data_dir)
    provider = ghauth.TokenProvider(store, opts.github_repo, GITHUB_PERMISSIONS, api, f"house-brain-deployer/{VERSION}")
    auth = ghauth.Auth(opts.github_auth, opts.github_token, provider, retire=opts.retire_old_token, api=api,
                       user_agent=f"house-brain-deployer/{VERSION}")
    gh = GitHub(opts.github_repo, opts.github_token, api, auth=auth)
    if os.environ.get("HBD_TEST_MODE") == "1":
        scale = float(os.environ.get("HBD_TEST_TIME_SCALE", "1"))
        timing = Timing(approval_timeout=timing.approval_timeout * scale,
                        restart_approval_timeout=timing.restart_approval_timeout * scale,
                        precondition_wait=900 * scale, running_timeout=900 * scale,
                        poll=max(0.05, 10 * scale), settle_scale=scale)
    open_url = panel_url(ha_mod.INSTANCE)
    if open_url is None:
        LOG.warning("Deployer panel identity unavailable; use native notification actions or Open Web UI")
    if runtime is not None and runtime._bindings is not None:
        from .migration.deployer_bound_forward_io import BoundForwardState, BoundForwardPackages
        journal = Journal(runtime.journal_root, file_io=BoundForwardState(runtime))
        package_client = BoundForwardPackages(runtime)
    else:
        journal = Journal(runtime.journal_root if runtime is not None else data_dir)
        package_client = Packages(packages)
    if runtime is not None:
        ha, gh, journal = runtime.wrap(ha), runtime.wrap(gh), runtime.wrap(journal)
    service = Service(opts, ha, gh, package_client, journal, timing,
                      board=ApprovalBoard(), open_url=open_url,
                      writer_factory=runtime.writer_factory if runtime is not None else None,
                      expected_owner_id=runtime.scope.owner_id if runtime is not None else None)
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


STARTUP_HELD_NOTE = ("The Deployer found a migration start file it cannot use and stayed stopped. Nothing was "
                     "installed or changed. To go back to the previous history, turn on return_to_previous_history "
                     "in the App's Configuration and restart it. Ask the Deployer chat; see 'Startup held' in the "
                     "App documentation.")


def report_startup_held(code: str, options_path: str) -> None:
    """D6(c): bounded report of a held startup. Never constructs a Journal, Service, credentials or migration.

    The status entity also carries an open install's restart marker; it is never overwritten (that would hide a
    Core restart from recovery). The same HELD reason is reported once, not on every restart."""
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        return
    ha = HomeAssistant(os.environ.get("HBD_SUPERVISOR_URL", "http://supervisor"), token,
                       os.environ.get("HBD_CORE_WS_URL", "ws://supervisor/core/websocket"))
    try:
        current = ha.get_state(ha_mod.STATUS_ENTITY) or {}
        readable = True
    except Exception:  # noqa: BLE001 - unknown status: report by push only, never overwrite it
        current, readable = {}, False
    attrs = current.get("attributes") or {}
    if current.get("state") == "HELD" and attrs.get("reason") == code:
        return
    if readable and not attrs.get("txn_marker"):
        ha.publish_status("HELD", {"reason": code, "version": VERSION, "recovery": STARTUP_HELD_NOTE})
    try:
        notify = load_options(options_path).notify_service
    except Exception:  # noqa: BLE001 - no valid notify target: the status and the log carry it
        return
    approval.inform(ha, notify, "House Brain Deployer: startup held", f"{code}. {STARTUP_HELD_NOTE}")


def main() -> int:
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(net.RedactingFilter())
    logging.basicConfig(level=logging.INFO, handlers=[handler],
                        format="%(asctime)s %(levelname)s %(message)s")
    options_path = os.environ.get("HBD_OPTIONS", "/data/options.json")
    data_dir = os.environ.get("HBD_DATA", "/data")
    try:
        service = build_service(options_path, data_dir)
    except WorkingError as err:
        if str(err).startswith(("STARTUP_", "FORWARD_")):   # 0.3.23: a moved-history start that fails holds too
            LOG.error("startup held: %s (no service, no migration; see 'Startup held' in DOCS)", err)
            report_startup_held(str(err), options_path)
            return 2
        LOG.error("configuration refused: %s", err)
        return 2
    except (OptionsError, ValueError, OSError) as err:
        if str(err).startswith("FORWARD_"):
            LOG.error("startup held: %s", err)
            report_startup_held(str(err), options_path)
            return 2
        LOG.error("configuration refused: %s", err)
        return 2
    try:
        start_ingress(service)
    except OSError as err:  # the push buttons still work without the page
        LOG.warning("approval page unavailable: %s", err)
    LOG.info("House Brain Deployer %s started (dry_run=%s)", VERSION, service.o.dry_run)
    selfreport.log_report(LOG)  # 0.3.16: read-only native facts for the shared-writer layout; never raises
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
    pending = service.engine is not None and bool(service.j.load_txn())
    runtime = getattr(service, 'migrated_runtime', None)
    if runtime is not None:
        runtime.close()
    if not pending:
        LOG.info("stopped")
        return 0
    LOG.warning("stopped with a transaction pending; it will be recovered on next start")
    return 0


if __name__ == "__main__":
    sys.exit(main())

