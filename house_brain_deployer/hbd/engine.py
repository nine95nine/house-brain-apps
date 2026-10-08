"""Deployment transaction state machine.

Order (real mode):
  prepare (fetch/build/verify, no side effects)
  -> owner approval #1 (deploy)            no answer = reject
  -> re-verify snapshot, preconditions, strict baseline config check
  -> partial backup
  -> journaled file steps (retire -> replace -> create)
  -> strict config check (Supervisor errors + Core errors/warnings)
                                           fail -> undo files, no restart
  -> owner approval #2 (restart)           no answer = undo files, no restart
  -> preconditions again -> restart -> health (running, post-checks,
     unchanged entities, log scan)         fail -> undo files, check, restart
Every file step is journaled before it runs; recovery after a crash only
ever completes a rollback or finishes the health check, never moves forward
into new changes.
"""
from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import approval, diagnose, recipe, undo
from .fsops import FsError, Packages, file_sha
from .github import GitHub
from .ha import HomeAssistant
from .journal import Journal
from .manifest import Manifest, StateCheck
from .policy import DEFAULT_SENSITIVE_PATTERNS, PolicyError, basename, scan_package
from .web import ApprovalBoard

# Terminal outcomes
SUCCEEDED = "SUCCEEDED"
DRY_RUN_OK = "DRY_RUN_OK"
DRY_RUN_BLOCKED = "DRY_RUN_BLOCKED"
ALREADY_APPLIED = "ALREADY_APPLIED"
REFUSED = "REFUSED"
REJECTED = "REJECTED"
TIMED_OUT = "TIMED_OUT"
ABORTED = "ABORTED"  # nothing changed
ROLLED_BACK = "ROLLED_BACK"  # files restored, no restart was needed
ROLLED_BACK_RESTARTED = "ROLLED_BACK_RESTARTED"  # files restored + restart
FAILED_MANUAL = "FAILED_MANUAL"  # could not prove a safe state; owner must look


class Stopping(Exception):
    """0.3.5 (P3): the App was asked to stop while waiting. The journal is left exactly as it is; the next
    start's recovery decides (it only rolls back or finishes the health check). Never a forward step."""

NO_CHANGE_OUTCOMES = {DRY_RUN_OK, DRY_RUN_BLOCKED, ALREADY_APPLIED, REFUSED, REJECTED, TIMED_OUT, ABORTED}


@dataclass
class Timing:
    approval_timeout: float = 900.0
    restart_approval_timeout: float = 600.0
    precondition_wait: float = 900.0
    running_timeout: float = 900.0
    poll: float = 10.0
    settle_scale: float = 1.0


@dataclass
class Settings:
    notify_service: str
    owner_user_id: str
    dry_run: bool = True
    require_auth: bool = True
    sensitive_patterns: tuple[str, ...] = DEFAULT_SENSITIVE_PATTERNS
    timing: Timing = field(default_factory=Timing)
    board: ApprovalBoard | None = None
    open_url: str | None = None
    # 0.3.5 (P1/P3): who is running (one id per App start, the host's kernel boot id) and the stop flag
    should_stop: Callable[[], bool] | None = None
    run_id: str = ""
    boot_id: str | None = None
    started_at: float = 0.0


@dataclass
class Result:
    outcome: str
    reasons: list[str] = field(default_factory=list)
    facts: dict[str, Any] = field(default_factory=dict)


class Refused(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass
class Prepared:
    manifest: Manifest
    contents: dict[str, bytes]  # target -> bytes
    flags: list[str]
    pre: dict[str, str | None]


def check_state(check: StateCheck, state: dict | None) -> bool:
    if state is None:
        return False
    if check.attribute:
        value = (state.get("attributes") or {}).get(check.attribute)
    else:
        value = state.get("state")
    if value is None:
        return False
    if check.number is not None:
        try:
            return abs(float(value) - check.number) < 1e-6
        except (TypeError, ValueError):
            return False
    text = str(value)
    if check.equals is not None:
        return text == check.equals
    return text in (check.one_of or ())


def describe(check: StateCheck) -> str:
    target = check.entity_id + (f"[{check.attribute}]" if check.attribute else "")
    if check.number is not None:
        return f"{target} == {check.number:g}"
    if check.equals is not None:
        return f"{target} == {check.equals}"
    return f"{target} in {list(check.one_of or ())}"


class Engine:
    def __init__(self, ha: HomeAssistant, gh: GitHub, packages: Packages, journal: Journal,
                 settings: Settings, sleep: Callable[[float], None] = time.sleep) -> None:
        self.ha = ha
        self.gh = gh
        self.pk = packages
        self.j = journal
        self.s = settings
        self.sleep = sleep
        self.notes: list[str] = []   # plain-English explanations added to the next result (0.3.1)

    def _check_stop(self, where: str) -> None:
        if self.s.should_stop is not None and self.s.should_stop():
            raise Stopping(where)

    # ------------------------------------------------------------------ prepare
    def prepare(self, m: Manifest) -> Prepared:
        if not self.gh.is_ancestor(m.source_commit, m.source_ref):
            raise Refused("SOURCE_NOT_ON_REF")
        contents: dict[str, bytes] = {}
        flags: list[str] = []
        for op in m.files:
            if op.source.kind == "blob":
                data = self.gh.read_file(op.source.path, m.source_commit)
                if recipe.sha256(data) != op.sha256:
                    raise Refused("SHA_MISMATCH", basename(op.target))
            else:
                recipe_bytes = self.gh.read_file(op.source.path, m.source_commit)
                baseline = self.gh.read_file(op.source.baseline or "", m.source_commit)
                try:
                    data = recipe.build(recipe_bytes, baseline, op.source.baseline or "", op.sha256)
                except recipe.RecipeError as err:
                    raise Refused(err.code, basename(op.target)) from None
            errors, file_flags = scan_package(data, self.s.sensitive_patterns)
            if errors:
                raise Refused("CONTENT_POLICY", f"{basename(op.target)}: {', '.join(errors[:5])}")
            flags.extend(f"{basename(op.target)} {f}" for f in file_flags)
            contents[op.target] = data
        self.pk.check_root()
        pre = self.pk.snapshot(m.touched_paths())
        return Prepared(m, contents, flags, pre)

    def _applied(self, p: Prepared) -> bool:
        m = p.manifest
        for op in m.files:
            if p.pre[op.target] != op.sha256:
                return False
            if op.backup_as is not None and p.pre[op.backup_as] is None:
                return False
        for rop in m.retire:
            if p.pre[rop.path] is not None or p.pre[rop.rename_to] is None:
                return False
        return True

    def _state_problems(self, p: Prepared) -> list[str]:
        m, pre = p.manifest, p.pre
        problems: list[str] = []
        for op in m.files:
            name = basename(op.target)
            if op.mode == "create" and pre[op.target] is not None:
                problems.append(f"{name} already exists")
            if op.mode == "replace" and op.backup_as is not None:
                current = pre[op.target]
                if current is None:
                    problems.append(f"{name} missing")
                elif op.expect_current_sha256 and current != op.expect_current_sha256:
                    problems.append(f"{name} live hash {current[:12]} != expected "
                                    f"{op.expect_current_sha256[:12]}")
                if pre[op.backup_as] is not None:
                    problems.append(f"{basename(op.backup_as)} already exists")
        for rop in m.retire:
            name = basename(rop.path)
            current = pre[rop.path]
            if current is None:
                problems.append(f"{name} missing")
            elif rop.expect_current_sha256 and current != rop.expect_current_sha256:
                problems.append(f"{name} live hash {current[:12]} != expected "
                                f"{rop.expect_current_sha256[:12]}")
            if pre[rop.rename_to] is not None:
                problems.append(f"{basename(rop.rename_to)} already exists")
        return problems

    def plan_steps(self, p: Prepared) -> list[dict]:
        m = p.manifest
        steps: list[dict] = []
        for rop in m.retire:
            sha = p.pre[rop.path]
            steps.append({"op": "link", "src": rop.path, "dst": rop.rename_to, "sha": sha})
            steps.append({"op": "retire_unlink", "path": rop.path, "keep": rop.rename_to, "sha": sha})
        for index, op in enumerate(m.files):
            stage = self.pk.stage_path(m.request_id, index)
            steps.append({"op": "stage", "stage": stage, "sha": op.sha256, "target": op.target})
            if op.mode == "replace":
                old = p.pre[op.target]
                steps.append({"op": "link", "src": op.target, "dst": op.backup_as, "sha": old})
                steps.append({"op": "swap", "stage": stage, "target": op.target, "backup": op.backup_as,
                              "old": old, "sha": op.sha256})
            else:
                steps.append({"op": "create", "stage": stage, "target": op.target, "sha": op.sha256})
                steps.append({"op": "unstage", "stage": stage})
        return steps

    # --------------------------------------------------------------- file steps
    def _do(self, step: dict, contents: dict[str, bytes]) -> None:
        L = self.pk.local
        kind = step["op"]
        if kind == "link":
            if file_sha(L(step["src"])) != step["sha"]:
                raise FsError("UNEXPECTED_CONTENT", basename(step["src"]))
            self.pk.link_noclobber(L(step["src"]), L(step["dst"]))
        elif kind == "retire_unlink":
            if file_sha(L(step["keep"])) != step["sha"]:
                raise FsError("BACKUP_NOT_VERIFIED", basename(step["keep"]))
            self.pk.unlink_if(L(step["path"]), step["sha"])
        elif kind == "stage":
            self.pk.write_stage(step["stage"], contents[step["target"]], step["sha"])
        elif kind == "swap":
            if file_sha(L(step["backup"])) != step["old"]:
                raise FsError("BACKUP_NOT_VERIFIED", basename(step["backup"]))
            if file_sha(step["stage"]) != step["sha"]:
                raise FsError("STAGE_HASH")
            self.pk.replace_with(step["stage"], L(step["target"]))
        elif kind == "create":
            if file_sha(step["stage"]) != step["sha"]:
                raise FsError("STAGE_HASH")
            self.pk.link_noclobber(step["stage"], L(step["target"]))
        elif kind == "unstage":
            self.pk.unlink_if(step["stage"], None)
        else:  # pragma: no cover - plan is internal
            raise FsError("UNKNOWN_STEP")

    def _undo(self, step: dict) -> None:
        """Idempotent inverse. Never removes the last copy of original bytes."""
        L = self.pk.local
        kind = step["op"]
        if kind == "link":
            src, dst = L(step["src"]), L(step["dst"])
            if file_sha(dst) == step["sha"] and file_sha(src) == step["sha"]:
                self.pk.unlink_if(dst, step["sha"])
        elif kind == "retire_unlink":
            path, keep = L(step["path"]), L(step["keep"])
            if file_sha(path) is None:
                if file_sha(keep) != step["sha"]:
                    raise FsError("CANNOT_RESTORE", basename(step["path"]))
                self.pk.link_noclobber(keep, path)
        elif kind == "stage":
            self.pk.unlink_if(step["stage"], None)
        elif kind == "swap":
            target, backup = L(step["target"]), L(step["backup"])
            current = file_sha(target)
            if current == step["old"]:
                return
            if file_sha(backup) != step["old"]:
                raise FsError("CANNOT_RESTORE", basename(step["target"]))
            tmp = step["stage"] + ".restore"
            self.pk.unlink_if(tmp, None)
            self.pk.link_noclobber(backup, tmp)
            self.pk.replace_with(tmp, target)
        elif kind == "create":
            self.pk.unlink_if(L(step["target"]), step["sha"])
        elif kind == "unstage":
            return

    def _apply(self, txn: dict, contents: dict[str, bytes]) -> None:
        for index, step in enumerate(txn["steps"]):
            if index < txn["done"]:
                continue
            txn["intent"] = index
            txn["applied_any"] = True
            self.j.save_txn(txn)
            self._do(step, contents)
            txn["done"] = index + 1
            self.j.save_txn(txn)

    def _undo_all(self, txn: dict) -> list[str]:
        last = max(txn.get("intent", -1), txn.get("done", 0) - 1)
        for index in range(last, -1, -1):
            self._undo(txn["steps"][index])
        for step in txn["steps"]:
            if step["op"] == "stage":
                self.pk.unlink_if(step["stage"], None)
                self.pk.unlink_if(step["stage"] + ".restore", None)
        txn["done"] = 0
        txn["intent"] = -1
        self.j.save_txn(txn)
        now = self.pk.snapshot(list(txn["pre"]))
        return [basename(p) for p, sha in txn["pre"].items() if now.get(p) != sha]

    # ----------------------------------------------------------------- checks
    def strict_config_check(self) -> list[str]:
        problems: list[str] = []
        ok, detail = self.ha.supervisor_check()
        if not ok:
            problems.append(f"Supervisor check failed: {detail[:300]}")
        try:
            result, errors, warnings = self.ha.core_check()
            if result != "valid" or errors:
                problems.append(f"Core check errors: {str(errors)[:300]}")
            if warnings:
                problems.append(f"Core check warnings: {str(warnings)[:300]}")
        except Exception as err:  # noqa: BLE001 - unknown check result is a failure
            problems.append(f"Core check unavailable: {type(err).__name__}")
        return problems

    def safe_state(self, entity_id: str) -> dict | None:
        try:
            return self.ha.get_state(entity_id)
        except Exception:  # noqa: BLE001 - unreadable (e.g. 502 while Core starts) == not satisfied
            return None

    def unmet(self, checks: tuple[StateCheck, ...]) -> list[str]:
        out = []
        for check in checks:
            state = self.safe_state(check.entity_id)
            if not check_state(check, state):
                out.append(describe(check))
        return out

    def wait_preconditions(self, m: Manifest) -> list[str]:
        deadline = time.monotonic() + self.s.timing.precondition_wait
        while True:
            self._check_stop("PRECONDITIONS")
            unmet = self.unmet(m.preconditions)
            if not unmet or time.monotonic() >= deadline:
                return unmet
            self.sleep(self.s.timing.poll)

    def capture_unchanged(self, m: Manifest) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        for entity in m.expect_unchanged:
            state = self.safe_state(entity)
            out[entity] = state.get("state") if state else None
        return out

    def health(self, txn: dict, m: Manifest) -> list[str]:
        t = self.s.timing
        running = self.ha.wait_running(t.running_timeout, t.poll, should_stop=self.s.should_stop)
        self._check_stop("HEALTH")
        if not running:
            return ["Home Assistant did not reach RUNNING"]
        deadline = time.monotonic() + m.settle_seconds * t.settle_scale
        while True:
            self._check_stop("HEALTH")
            problems = self.unmet(m.post_checks)
            for entity, before in txn["unchanged"].items():
                state = self.safe_state(entity)
                now = state.get("state") if state else None
                if now != before:
                    problems.append(f"{entity} changed {before} -> {now}")
            if not problems or time.monotonic() >= deadline:
                break
            self.sleep(t.poll)
        if problems:
            return problems
        return self.log_scan(m)

    def log_scan(self, m: Manifest) -> list[str]:
        if not m.log_tokens:
            return []
        try:
            sock = self.ha.ws()
            try:
                entries = self.ha_ws_system_log(sock)
            finally:
                sock.close()
        except Exception as err:  # noqa: BLE001 - an unreadable log is not a pass
            return [f"log scan unavailable: {type(err).__name__}"]
        hits = []
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("level") not in m.log_fail_levels:
                continue
            message = entry.get("message")
            if isinstance(message, list):
                message = " ".join(str(x) for x in message)
            text = " ".join(str(x) for x in (entry.get("name"), message, entry.get("source"))).lower()
            if any(token in text for token in m.log_tokens):
                hits.append(f"log {entry.get('level')}: {str(entry.get('name'))[:60]}")
        return hits[:10]

    @staticmethod
    def ha_ws_system_log(sock) -> list:
        result = sock.command({"type": "system_log/list"})
        return result if isinstance(result, list) else []

    # --------------------------------------------------------------- messages
    def _summary(self, p: Prepared) -> str:
        m = p.manifest
        lines = [f"{m.request_id} from {m.requested_by} @ {m.source_commit[:8]}"]
        if m.reask_of:
            lines.append(f"ASKED AGAIN: {m.reask_of} expired unanswered; same files, same checks")
        for op in m.files:
            size = len(p.contents[op.target]) // 1024
            verb = "NEW" if op.mode == "create" else "REPLACE"
            lines.append(f"{verb} {basename(op.target)} ({size} KB, {op.sha256[:8]})")
            if op.backup_as:
                lines.append(f"  old -> {basename(op.backup_as)}")
        for rop in m.retire:
            lines.append(f"RETIRE {basename(rop.path)} -> {basename(rop.rename_to)}")
        if p.flags:
            lines.append("REVIEW: " + "; ".join(p.flags[:4]))
        lines.append(f'AI title: "{m.title}"')
        if m.summary:
            lines.append(f'AI summary: "{m.summary}"')
        return "\n".join(lines)

    # ------------------------------------------------------------------ owner undo (0.3.3)
    def run_undo(self, record: dict) -> Result:
        """Reverse one recorded install with bytes from its on-disk .bak copies (owner tap = approval)."""
        if self.s.dry_run:
            return Result(REFUSED, ["Undo is not available in practice mode (dry_run)"])
        try:
            m, sources = undo.build(record)
        except (undo.UndoError, PolicyError, KeyError, TypeError, ValueError) as err:
            return Result(REFUSED, [f"cannot build the undo: {err}"])
        self.j.audit(m.request_id, "RECEIVED", digest=m.digest, requested_by="owner", commit="local")
        contents: dict[str, bytes] = {}
        try:
            self.pk.check_root()
            for target, (src, sha) in sources.items():
                path = self.pk.local(src)
                if file_sha(path) != sha:
                    return Result(REFUSED, [f"UNDO_SOURCE_CHANGED: {basename(src)} is no longer the saved copy"])
                with open(path, "rb") as fh:
                    data = fh.read()
                errors, _ = scan_package(data, self.s.sensitive_patterns)
                if errors:
                    return Result(REFUSED, [f"CONTENT_POLICY: {basename(src)}: {', '.join(errors[:5])}"])
                contents[target] = data
            pre = self.pk.snapshot(m.touched_paths())
        except FsError as err:
            return Result(REFUSED, [str(err)])
        p = Prepared(m, contents, [], pre)
        facts: dict[str, Any] = {"undo_of": record.get("request_id"),
                                 "live_sha256": {basename(k): v for k, v in pre.items()}}
        problems = self._state_problems(p)
        if problems:
            return Result(REFUSED, ["STATE_MISMATCH (files changed since that install): " + "; ".join(problems)],
                          facts)
        return self._real(p, facts, approved_by_tap=True)

    # ------------------------------------------------------------------ run
    def run(self, m: Manifest) -> Result:
        self.j.audit(m.request_id, "RECEIVED", digest=m.digest, requested_by=m.requested_by,
                     commit=m.source_commit)
        try:
            p = self.prepare(m)
        except Refused as err:
            return Result(REFUSED, [str(err)])
        except FsError as err:
            return Result(REFUSED, [str(err)])
        facts: dict[str, Any] = {
            "live_sha256": {basename(k): v for k, v in p.pre.items()},
            "flags": p.flags,
        }
        spaced = self.pk.space_named_files()
        if spaced:
            facts["space_named_files_in_packages"] = spaced[:10]
        if self._applied(p):
            return Result(ALREADY_APPLIED, [], facts)
        problems = self._state_problems(p)
        if problems:
            return Result(REFUSED, ["STATE_MISMATCH: " + "; ".join(problems)], facts)
        if self.s.dry_run:
            return self._dry_run(p, facts)
        return self._real(p, facts)

    def _ask(self, m: Manifest | str, stage: str, title: str, message: str, timeout: float) -> str:
        rid = m if isinstance(m, str) else m.request_id
        self.j.audit(rid, "APPROVAL_REQUESTED", stage=stage)
        decision = approval.ask(self.ha, self.s.notify_service, self.s.owner_user_id, stage=stage,
                                title=title, message=message, timeout=timeout,
                                require_auth=self.s.require_auth, board=self.s.board,
                                open_url=self.s.open_url, should_stop=self.s.should_stop)
        if decision.outcome == approval.STOPPED:
            self.j.audit(rid, "APPROVAL_STOPPED", stage=stage)
            raise Stopping(stage)
        self.j.audit(rid, "APPROVAL_DECISION", stage=stage, outcome=decision.outcome,
                     ignored_events=decision.ignored_events, detail=decision.detail,
                     channel=decision.channel)
        if decision.outcome == approval.UNAVAILABLE:
            self.notes.append(diagnose.approval_problem(decision.detail, self.s.notify_service))
        elif decision.outcome == approval.TIMEOUT:
            after = {"RESTART": "the new files were taken out again and the previous ones put back",
                     "ROLLBACK": "see the result below for what was done",
                     "LOOKUP": "nothing was read or changed"}.get(stage, "nothing was changed")
            self.notes.append(f"No answer to the {stage.lower()} approval before it expired. That counts as "
                              f"Reject: {after}.")
        return decision.outcome

    def _dry_run(self, p: Prepared, facts: dict) -> Result:
        m = p.manifest
        outcome = self._ask(m, "DRYRUN", "House Brain Deployer: DRY RUN (nothing will change)",
                            self._summary(p), self.s.timing.approval_timeout)
        if outcome != approval.APPROVE:
            return Result(REJECTED if outcome == approval.REJECT else TIMED_OUT, [outcome], facts)
        reasons = self.strict_config_check()
        unmet = self.unmet(m.preconditions)
        if unmet:
            reasons.append("preconditions not met now: " + "; ".join(unmet))
        try:
            info = self.ha.supervisor_info()
            facts["supervisor"] = {"version": info.get("supervisor"), "channel": info.get("channel")}
            facts["core_version"] = self.ha.core_info().get("version")
        except Exception as err:  # noqa: BLE001 - informational facts only
            facts["platform_info_error"] = type(err).__name__
        facts["would_do"] = [s["op"] + " " + basename(str(s.get("target") or s.get("dst") or s.get("path")
                                                          or s.get("stage") or "")) for s in self.plan_steps(p)]
        return Result(DRY_RUN_BLOCKED if reasons else DRY_RUN_OK, reasons, facts)

    def _real(self, p: Prepared, facts: dict, approved_by_tap: bool = False) -> Result:
        m, t = p.manifest, self.s.timing
        if approved_by_tap:
            # 0.3.3 Undo: the owner's authenticated single-use tap on the page is the deploy approval.
            outcome = approval.APPROVE
            self.j.audit(m.request_id, "DEPLOY_APPROVED_BY_OWNER_TAP")
        else:
            outcome = self._ask(m, "DEPLOY", "House Brain Deployer: approve deploy?",
                                self._summary(p) + "\nRestart needs a 2nd tap. Auto-rollback on failure.",
                                t.approval_timeout)
        if outcome != approval.APPROVE:
            return Result(REJECTED if outcome == approval.REJECT else TIMED_OUT, [outcome], facts)

        if self.pk.snapshot(list(p.pre)) != p.pre:
            return Result(ABORTED, ["files changed while waiting for approval"], facts)
        unmet = self.wait_preconditions(m)
        if unmet:
            return Result(ABORTED, ["preconditions not met: " + "; ".join(unmet)], facts)
        baseline = self.strict_config_check()
        if baseline:
            return Result(ABORTED, ["baseline config not clean (no changes made)"] + baseline, facts)
        if self.ha.core_state() != "RUNNING":
            return Result(ABORTED, ["Home Assistant not RUNNING"], facts)
        unchanged = self.capture_unchanged(m)
        unreadable = [e for e, v in unchanged.items() if v in (None, "unknown", "unavailable")]
        if unreadable:
            return Result(ABORTED, ["cannot read entities that must stay unchanged: " + ", ".join(unreadable)],
                          facts)

        backup_name = f"HBD {m.request_id} pre-deploy"
        try:
            before = {b.get("slug") for b in self.ha.backups()}
            self.ha.backup_partial(backup_name)
            new = [b for b in self.ha.backups()
                   if b.get("slug") not in before and b.get("name") == backup_name]
        except Exception as err:  # noqa: BLE001
            return Result(ABORTED, [f"backup failed: {type(err).__name__}: {str(err)[:120]}"], facts)
        if len(new) != 1:
            return Result(ABORTED, ["backup not found after creation"], facts)
        facts["backup_slug"] = new[0].get("slug")
        self.j.audit(m.request_id, "BACKUP_OK", slug=facts["backup_slug"])

        marker = secrets.token_hex(8)
        try:
            self.ha.set_marker(marker, m.request_id)
        except Exception as err:  # noqa: BLE001 - without the marker a Core restart could go unnoticed
            return Result(ABORTED, [f"cannot write status marker: {type(err).__name__}"], facts)
        txn = {
            "request_id": m.request_id, "digest": m.digest, "phase": "APPLYING",
            "steps": self.plan_steps(p), "done": 0, "intent": -1, "pre": p.pre,
            "unchanged": unchanged, "restarted": False, "marker": marker,
            "backup_slug": facts["backup_slug"], "facts": facts,
            # 0.3.5 (P1): which App run and which host boot opened this transaction
            "app_run": self.s.run_id, "boot_id": self.s.boot_id, "opened_at": time.time(),
        }
        self.j.save_txn(txn)
        try:
            self._apply(txn, p.contents)
        except Exception as err:  # noqa: BLE001
            return self._rollback_files(txn, [f"apply failed: {err}"])
        txn["post"] = self.pk.snapshot(list(p.pre))
        self.j.audit(m.request_id, "FILES_APPLIED")

        txn["phase"] = "CHECKING"
        self.j.save_txn(txn)
        problems = self.strict_config_check()
        if problems:
            return self._rollback_files(txn, ["config check failed"] + problems)

        txn["phase"] = "AWAITING_RESTART"
        self.j.save_txn(txn)
        outcome = self._ask(m, "RESTART", "House Brain Deployer: approve restart?",
                            f"{m.request_id}: files installed, config check clean.\n"
                            "Approve restarts Home Assistant now. Reject/no answer restores the old files.",
                            t.restart_approval_timeout)
        if outcome != approval.APPROVE:
            return self._rollback_files(txn, [f"restart not approved ({outcome})"])
        if self.pk.snapshot(list(p.pre)) != txn["post"]:
            return self._rollback_files(txn, ["files changed while waiting for restart approval"])
        unmet = self.wait_preconditions(m)
        if unmet:
            return self._rollback_files(txn, ["preconditions not met before restart: " + "; ".join(unmet)])
        return self._restart_and_verify(txn, m)

    def _restart_and_verify(self, txn: dict, m: Manifest) -> Result:
        txn["phase"] = "RESTARTING"
        txn["restarted"] = True
        self.j.save_txn(txn)
        self.j.audit(m.request_id, "RESTART_REQUESTED")
        try:
            self.ha.restart()
        except Exception as err:  # noqa: BLE001 - health decides what actually happened
            self.j.audit(m.request_id, "RESTART_CALL_ERROR", error=type(err).__name__)
        txn["phase"] = "HEALTH"
        self.j.save_txn(txn)
        problems = self.health(txn, m)
        if problems:
            return self._rollback_restart(txn, m, ["health check failed"] + problems)
        txn["facts"]["post_sha256"] = {basename(k): v for k, v in self.pk.snapshot(list(txn["pre"])).items()}
        self._record_install(m, txn)
        self.j.clear_txn()
        return Result(SUCCEEDED, [], txn["facts"])

    def _record_install(self, m: Manifest, txn: dict) -> None:
        """0.3.3: remember what a successful install did, so the owner can undo it from the page."""
        try:
            undo.record_success(self.j, m, txn["pre"], self.pk.snapshot(list(txn["pre"])))
        except Exception as err:  # noqa: BLE001 - never turn a success into a failure; undo just won't be offered
            self.j.audit(m.request_id, "INSTALL_RECORD_FAILED", error=type(err).__name__)

    def _rollback_files(self, txn: dict, reasons: list[str]) -> Result:
        txn["phase"] = "ROLLING_BACK"
        self.j.save_txn(txn)
        self.j.audit(txn["request_id"], "ROLLBACK_FILES", reasons=reasons[:3])
        try:
            diff = self._undo_all(txn)
        except Exception as err:  # noqa: BLE001
            return Result(FAILED_MANUAL, reasons + [f"rollback error: {err}"], txn["facts"])
        if diff:
            return Result(FAILED_MANUAL, reasons + ["not restored: " + ", ".join(diff)], txn["facts"])
        if txn.get("marker") and txn.get("applied_any") and self._core_restarted(txn):
            return self._finish_rollback_restart(txn, reasons)
        self.j.clear_txn()
        return Result(ROLLED_BACK, reasons, txn["facts"])

    def _core_restarted(self, txn: dict) -> bool:
        self.ha.wait_running(self.s.timing.running_timeout, self.s.timing.poll)
        return not self.ha.marker_present(txn["marker"])

    def _finish_rollback_restart(self, txn: dict, reasons: list[str]) -> Result:
        """Core restarted outside the Deployer while new files were in place.

        The old files are back on disk, but Home Assistant may be running the new
        ones. Restarting was never approved for this case, so ask the owner.
        """
        reasons = reasons + ["Home Assistant restarted during the deployment; old files restored on disk"]
        txn["phase"] = "AWAITING_ROLLBACK_RESTART"
        self.j.save_txn(txn)
        outcome = self._ask(txn["request_id"], "ROLLBACK", "House Brain Deployer: restart to finish rollback?",
                            f"{txn['request_id']}: Home Assistant restarted while new files were installed. "
                            "Old files are restored. Approve restarts Home Assistant on the old files.",
                            self.s.timing.restart_approval_timeout)
        if outcome != approval.APPROVE:
            return Result(FAILED_MANUAL, reasons + [f"rollback restart not approved ({outcome}); "
                                                    "Home Assistant may be running the new files until restarted"],
                          txn["facts"])
        ok, detail = self.ha.supervisor_check()
        if not ok:
            return Result(FAILED_MANUAL, reasons + ["restored files fail the config check; not restarting",
                                                    detail[:200]], txn["facts"])
        txn["phase"] = "ROLLBACK_RESTARTING"
        txn["restarted"] = True
        self.j.save_txn(txn)
        try:
            self.ha.restart()
        except Exception as err:  # noqa: BLE001
            self.j.audit(txn["request_id"], "RESTART_CALL_ERROR", error=type(err).__name__)
        if not self.ha.wait_running(self.s.timing.running_timeout, self.s.timing.poll):
            return Result(FAILED_MANUAL, reasons + ["Home Assistant not RUNNING after rollback restart"],
                          txn["facts"])
        self.j.clear_txn()
        return Result(ROLLED_BACK_RESTARTED, reasons, txn["facts"])

    def _rollback_restart(self, txn: dict, m: Manifest | None, reasons: list[str]) -> Result:
        txn["phase"] = "ROLLING_BACK"
        self.j.save_txn(txn)
        self.j.audit(txn["request_id"], "ROLLBACK_RESTART", reasons=reasons[:3])
        try:
            diff = self._undo_all(txn)
        except Exception as err:  # noqa: BLE001
            return Result(FAILED_MANUAL, reasons + [f"rollback error: {err}"], txn["facts"])
        if diff:
            return Result(FAILED_MANUAL, reasons + ["not restored: " + ", ".join(diff)], txn["facts"])
        ok, detail = self.ha.supervisor_check()
        if not ok:
            return Result(FAILED_MANUAL, reasons + ["restored files fail the config check; not restarting",
                                                    detail[:200]], txn["facts"])
        txn["phase"] = "ROLLBACK_RESTARTING"
        self.j.save_txn(txn)
        try:
            self.ha.restart()
        except Exception as err:  # noqa: BLE001
            self.j.audit(txn["request_id"], "RESTART_CALL_ERROR", error=type(err).__name__)
        if not self.ha.wait_running(self.s.timing.running_timeout, self.s.timing.poll):
            return Result(FAILED_MANUAL, reasons + ["Home Assistant not RUNNING after rollback restart"],
                          txn["facts"])
        self.j.clear_txn()
        return Result(ROLLED_BACK_RESTARTED, reasons, txn["facts"])

    # ---------------------------------------------------------------- recovery
    def recover(self, manifest_for: Callable[[str], Manifest | None]) -> tuple[str, Result] | None:
        txn = self.j.load_txn()
        if not txn:
            return None
        rid = txn["request_id"]
        phase = txn.get("phase")
        cause, details = self.interruption(txn)
        self.j.audit(rid, "RECOVERY", phase=phase, cause=cause)
        txn.setdefault("facts", {})["recovery"] = details
        note = f"interruption: {cause} during {phase}"
        if phase in ("RESTARTING", "HEALTH"):
            m = manifest_for(rid)
            if m is None or m.digest != txn["digest"]:
                return rid, self._rollback_restart(txn, None, ["recovered after crash; manifest unavailable", note])
            txn["phase"] = "HEALTH"
            self.j.save_txn(txn)
            problems = self.health(txn, m)
            if problems:
                return rid, self._rollback_restart(txn, m, ["recovered after crash; health failed", note] + problems)
            self._record_install(m, txn)
            self.j.clear_txn()
            return rid, Result(SUCCEEDED, ["completed after recovery", note], txn.get("facts", {}))
        if phase in ("ROLLING_BACK", "ROLLBACK_RESTARTING") and txn.get("restarted"):
            return rid, self._rollback_restart(txn, None, ["recovered after crash during rollback", note])
        return rid, self._rollback_files(txn, [f"recovered after crash in {phase}", note])

    def interruption(self, txn: dict) -> tuple[str, dict]:
        """0.3.5 (P1/P5): why a transaction was left open, from the journal and the audit trail only."""
        rid = txn["request_id"]
        run = txn.get("app_run")
        events = [e for e in self.j.audit_events() if e.get("run") == run] if run else []
        stop = next((e for e in reversed(events) if e.get("event") == "STOP_REQUESTED"), None)
        error = next((e for e in reversed(events) if e.get("event") == "TXN_EXCEPTION"
                      and e.get("request_id") == rid), None)
        if not run:
            cause = "unknown (opened by an older Deployer version)"
        elif run == self.s.run_id:
            cause = f"internal error {error.get('type') if error else 'unknown'} (the App kept running)"
        elif txn.get("boot_id") and self.s.boot_id and txn["boot_id"] != self.s.boot_id:
            cause = "the host rebooted"
        elif stop is not None:
            cause = "the App was stopped (App update, options save or restart)"
        else:
            cause = "the App ended without a stop signal (killed or crashed)"
        saved = txn.get("saved_at")
        marker = txn.get("marker")
        details = {
            "phase": txn.get("phase"),
            "cause": cause,
            "journal_age_seconds": int(time.time() - saved) if isinstance(saved, (int, float)) else None,
            "core_restarted": (not self.ha.marker_present(marker)) if marker else None,
            "stop_requested_at": stop.get("ts") if stop else None,
            "app_started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.s.started_at))
            if self.s.started_at else None,
        }
        return cause, details
