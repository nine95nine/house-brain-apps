"""0.7.4 ``STAGE_LOCAL_APP_SOURCE``: install a new local House Brain App version from a pinned carrier, one tap.

Owner-approved design ``docs/architecture/MAINTENANCE_STAGE_LOCAL_APP_SOURCE_R1.md`` (owner pop-ups 2026-10-10:
"A: Maintenance job (Recommended)", "Approve design, build it", "Build as 0.7.4 on top").

Order (nothing is written before the owner's Approve):
1. Validate again: built-in slug allowlist (``manifest.STAGE_APPS``), carrier path, 40-hex commit, 64-hex SHA-256,
   plain versions; the App is installed at ``from_version`` and nothing else is offered for it; not held; no
   after-update check open; Reject of this version is final.
2. The commit must be on ``main`` of the configured repository (GitHub compare); the carrier bytes are read at that
   commit (read-only GitHub access, at most 1 MiB) and must hash to ``sha256`` (else ``STAGE_CARRIER_HASH_MISMATCH``).
3. The carrier is unpacked into a private temporary folder (only plain files and folders, no absolute paths, no
   ``..``, no links) and its own ``app_source_update.sh check`` runs. It must say ``SOURCE_BEFORE=<from>``,
   ``READY_TO_STAGE=true`` and ``HB91_APP_UPDATE_RESULT=PASS``, and its ``UPDATE_TARGET.json`` must name exactly this
   App and these versions. Anything else stops the job and the owner is never asked.
4. One ask through the existing approval flow (counts toward ``max_approval_requests_per_day``; no answer = no).
5. On Approve: ``stage`` (the carrier's verified backup and atomic swap), ``POST /store/reload`` inside
   ``ha.stage_target`` (the only new Supervisor route), then the existing App update path for exactly that slug
   (``Updater.apply``: backup, update, health watch, restore on failure) without a second ask, then start the App
   only if it was running before (existing ``start_app`` fix route).
6. Any failure after ``stage``: the carrier's ``undo``, store reload, report.

The mapped ``/addons`` and ``/share`` folders are written ONLY by the carrier script that ``_script`` runs (a static
test and AppArmor hold this); this module itself writes only its private temporary folder and its own /data memory.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass

from . import approval, net
from .ha import ForbiddenCall, HAError, HomeAssistant
from .journal import Journal
from .jobs import DONE, DRY_RUN_OK, FAILED, FAILED_MANUAL, REFUSED, REJECTED, Result
from .manifest import (RE_CARRIER_PATH, RE_COMMIT, RE_SHA256, RE_STAGE_VERSION, STAGE_APPS,
                       STAGE_LOCAL_APP_SOURCE as JOB, Manifest)
from .review import BLOCKED
from .updates import RESTORED

ADDONS_DIR = "/addons"                      # config.yaml map: addons (rw); the carrier writes one App folder in it
SHARE_DIR = "/share/hb91-app-update"        # config.yaml map: share (rw); the carrier's own verified backups
MAX_CARRIER_BYTES = 1024 * 1024
MAX_MEMBERS = 256
SCRIPT = "app_source_update.sh"
REQUIRED_MEMBERS = (SCRIPT, "app_update_helper.py", "hb223pb_guard.py", "UPDATE_TARGET.json", "CARRIER_MANIFEST.tsv")
RE_MEMBER = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,99}(?:/[A-Za-z0-9_][A-Za-z0-9._-]{0,99}){0,4}$")
RE_OUT = re.compile(r"^([A-Z][A-Z0-9_]{1,60})=([\x20-\x7e]{0,200})$")
MODES = ("check", "stage", "undo")
MAIN_BRANCH = "main"
PENDING_DOC = "stage_pending"               # survives a restart mid-job (recover)
DECLINED_DOC = "stage_declined"             # "<slug>@<to>": the owner rejected it; final

# Machine reason codes (contracts/house_brain_reason_code_registry.v1.json)
STAGE_SLUG_NOT_ALLOWED = "STAGE_SLUG_NOT_ALLOWED"
STAGE_REQUEST_INVALID = "STAGE_REQUEST_INVALID"
STAGE_REJECTED_BEFORE = "STAGE_REJECTED_BEFORE"
STAGE_UPDATE_HELD = "STAGE_UPDATE_HELD"
STAGE_AFTER_UPDATE_OPEN = "STAGE_AFTER_UPDATE_OPEN"
STAGE_APP_NOT_INSTALLED = "STAGE_APP_NOT_INSTALLED"
STAGE_APP_VERSION_MISMATCH = "STAGE_APP_VERSION_MISMATCH"
STAGE_UPDATE_ALREADY_OFFERED = "STAGE_UPDATE_ALREADY_OFFERED"
STAGE_COMMIT_NOT_ON_MAIN = "STAGE_COMMIT_NOT_ON_MAIN"
STAGE_CARRIER_NOT_FOUND = "STAGE_CARRIER_NOT_FOUND"
STAGE_CARRIER_TOO_LARGE = "STAGE_CARRIER_TOO_LARGE"
STAGE_CARRIER_HASH_MISMATCH = "STAGE_CARRIER_HASH_MISMATCH"
STAGE_CARRIER_UNSAFE = "STAGE_CARRIER_UNSAFE"
STAGE_TARGET_MISMATCH = "STAGE_TARGET_MISMATCH"
STAGE_CHECK_NOT_READY = "STAGE_CHECK_NOT_READY"
STAGE_SCRIPT_FAILED = "STAGE_SCRIPT_FAILED"
STAGE_STAGE_FAILED = "STAGE_STAGE_FAILED"
STAGE_STORE_NOT_REFRESHED = "STAGE_STORE_NOT_REFRESHED"
STAGE_UPDATE_BLOCKED = "STAGE_UPDATE_BLOCKED"
STAGE_UPDATE_FAILED = "STAGE_UPDATE_FAILED"
STAGE_UNDO_FAILED = "STAGE_UNDO_FAILED"
STAGE_INSTALLED = "STAGE_INSTALLED"
STAGE_RESTARTED_MID_JOB = "STAGE_RESTARTED_MID_JOB"
# The same codes as one literal list (read by tools/reason_code_registry.py; test_l1_stage keeps both equal).
REASON_CODES = (
    "STAGE_SLUG_NOT_ALLOWED",
    "STAGE_REQUEST_INVALID",
    "STAGE_REJECTED_BEFORE",
    "STAGE_UPDATE_HELD",
    "STAGE_AFTER_UPDATE_OPEN",
    "STAGE_APP_NOT_INSTALLED",
    "STAGE_APP_VERSION_MISMATCH",
    "STAGE_UPDATE_ALREADY_OFFERED",
    "STAGE_COMMIT_NOT_ON_MAIN",
    "STAGE_CARRIER_NOT_FOUND",
    "STAGE_CARRIER_TOO_LARGE",
    "STAGE_CARRIER_HASH_MISMATCH",
    "STAGE_CARRIER_UNSAFE",
    "STAGE_TARGET_MISMATCH",
    "STAGE_CHECK_NOT_READY",
    "STAGE_SCRIPT_FAILED",
    "STAGE_STAGE_FAILED",
    "STAGE_STORE_NOT_REFRESHED",
    "STAGE_UPDATE_BLOCKED",
    "STAGE_UPDATE_FAILED",
    "STAGE_UNDO_FAILED",
    "STAGE_INSTALLED",
    "STAGE_RESTARTED_MID_JOB",
)


class CarrierError(ValueError):
    pass


@dataclass
class StagePolicy:
    addons: str = ADDONS_DIR
    share: str = SHARE_DIR
    script_seconds: float = 600.0           # one carrier run (check/stage/undo)
    reload_seconds: float = 120.0           # how long the Supervisor may take to show the new local version
    poll: float = 5.0
    work_root: str | None = None            # temporary folder parent (None = the system temp, /tmp)


def target_ok(target: dict, slug: str, frm: str, to: str) -> bool:
    """The carrier's UPDATE_TARGET.json names exactly this allowlisted App and these versions."""
    app_dir = STAGE_APPS[slug][0]
    return (isinstance(target, dict) and target.get("schema") == "hb91_app_source_update_target.v1"
            and target.get("app_dir") == app_dir and target.get("slug") == app_dir
            and f"local_{target.get('slug')}" == slug
            and target.get("old_version") == frm and target.get("new_version") == to)


def extract(raw: bytes, dest: str) -> None:
    """Unpack an uncompressed carrier into the empty private folder ``dest``: plain files and folders only, relative
    names without ``..``, no links, devices or duplicates. Files 0600, folders 0700 (the carrier does not trust modes;
    its own guard verifies every byte against CARRIER_MANIFEST.tsv)."""
    try:
        tf = tarfile.open(fileobj=io.BytesIO(raw), mode="r:")
    except tarfile.TarError:
        raise CarrierError("not an uncompressed tar") from None
    with tf:
        members = tf.getmembers()
        if not members or len(members) > MAX_MEMBERS:
            raise CarrierError("member count")
        seen: set[str] = set()
        for m in members:
            name = m.name[:-1] if m.isdir() and m.name.endswith("/") else m.name
            parts = name.split("/")
            if (name.startswith("/") or not RE_MEMBER.fullmatch(name) or ".." in parts or "." in parts
                    or not (m.isfile() or m.isdir()) or name in seen):
                raise CarrierError(f"unsafe member {name[:60]!r}")
            seen.add(name)
        root = os.path.realpath(dest)
        for m in sorted(members, key=lambda x: (not x.isdir(), x.name)):
            name = m.name.rstrip("/")
            path = os.path.realpath(os.path.join(root, name))
            if not path.startswith(root + os.sep):
                raise CarrierError("member outside the folder")
            if m.isdir():
                os.makedirs(path, mode=0o700, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
            src = tf.extractfile(m)
            data = src.read(MAX_CARRIER_BYTES + 1) if src is not None else b""
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
            finally:
                os.close(fd)
    for need in REQUIRED_MEMBERS:
        if not os.path.isfile(os.path.join(dest, need)) or os.path.islink(os.path.join(dest, need)):
            raise CarrierError(f"missing {need}")


def parse_output(text: str) -> dict[str, str]:
    """``KEY=value`` lines of the carrier script (printable ASCII only; the last value of a key wins)."""
    out: dict[str, str] = {}
    for line in text.splitlines()[:400]:
        m = RE_OUT.fullmatch(line.strip())
        if m:
            out[m.group(1)] = m.group(2)
    return out


class Stager:
    def __init__(self, ha: HomeAssistant, journal: Journal, settings, policy: StagePolicy, gh, updater,
                 after_active=None) -> None:
        self.ha = ha
        self.j = journal
        self.s = settings              # jobs.Settings: notify service, owner, dry_run, approval board ...
        self.p = policy
        self.gh = gh
        self.updater = updater         # updates.Updater: the existing App update path (and the hold list)
        self.after_active = after_active or (lambda: False)

    # -- the carrier script: the ONLY writer of the mapped /addons and /share folders -------------------------
    def _script_env(self) -> dict[str, str]:
        return {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
                "HB91_ADDONS": self.p.addons, "HB91_SHARE": self.p.share}

    def _script(self, work: str, mode: str) -> tuple[int, dict[str, str]]:
        if mode not in MODES:
            raise ValueError("carrier mode")
        try:
            proc = subprocess.run(["bash", os.path.join(work, SCRIPT), mode], cwd=work,  # noqa: S603,S607
                                  env=self._script_env(), stdin=subprocess.DEVNULL, capture_output=True,
                                  timeout=self.p.script_seconds, check=False)
        except (OSError, subprocess.TimeoutExpired) as err:
            return -1, {"STOP": f"SCRIPT_{type(err).__name__}"[:60]}
        return proc.returncode, parse_output(proc.stdout.decode("utf-8", "replace"))

    # -- helpers ----------------------------------------------------------------------------------------------
    @staticmethod
    def _stop(outcome: str, code: str, text: str, facts: dict) -> Result:
        return Result(outcome, [f"{code}: {text}"], {**facts, "reason_code": code})

    @staticmethod
    def _seen(out: dict[str, str]) -> dict:
        keep = ("SOURCE_BEFORE", "SOURCE_AFTER", "READY_TO_STAGE", "ALREADY_STAGED", "HB91_APP_UPDATE_RESULT", "STOP",
                "AUTOMATIC_RESTORE", "LIVE_CONFIG_VERSION", "FOLDERS_WITH_SLUG")
        return {k: out[k] for k in keep if k in out}

    def _invalid(self, m: Manifest) -> str | None:
        if m.stage_slug not in STAGE_APPS:
            return STAGE_SLUG_NOT_ALLOWED
        own = self.ha.self_slug()
        if m.stage_slug == own:
            return STAGE_SLUG_NOT_ALLOWED
        ok = (isinstance(m.stage_carrier, str) and RE_CARRIER_PATH.fullmatch(m.stage_carrier)
              and isinstance(m.stage_sha256, str) and RE_SHA256.fullmatch(m.stage_sha256)
              and isinstance(m.stage_commit, str) and RE_COMMIT.fullmatch(m.stage_commit)
              and isinstance(m.stage_from, str) and RE_STAGE_VERSION.fullmatch(m.stage_from)
              and isinstance(m.stage_to, str) and RE_STAGE_VERSION.fullmatch(m.stage_to)
              and m.stage_from != m.stage_to)
        return None if ok else STAGE_REQUEST_INVALID

    def _fetch(self, m: Manifest, work: str, facts: dict) -> Result | None:
        """Commit on main, carrier bytes at that commit, size, SHA-256, safe unpack. None when all hold."""
        try:
            on_main = self.gh.is_ancestor(str(m.stage_commit), MAIN_BRANCH)
        except net.NetError as err:
            if err.status not in (404, 422):
                raise
            on_main = False
        if not on_main:
            return self._stop(REFUSED, STAGE_COMMIT_NOT_ON_MAIN, f"commit {str(m.stage_commit)[:12]} is not on "
                              f"{MAIN_BRANCH} of {getattr(self.gh, 'repo', 'the repository')}; nothing asked", facts)
        try:
            raw = self.gh.read_file(str(m.stage_carrier), str(m.stage_commit))
        except net.NetError as err:
            if err.status != 404:
                raise
            return self._stop(REFUSED, STAGE_CARRIER_NOT_FOUND, "the carrier is not in that commit; nothing asked",
                              facts)
        raw = bytes(raw or b"")
        if len(raw) > MAX_CARRIER_BYTES:
            return self._stop(REFUSED, STAGE_CARRIER_TOO_LARGE, f"the carrier is {len(raw)} bytes (limit 1 MiB); "
                              "nothing asked", facts)
        got = hashlib.sha256(raw).hexdigest()
        facts["carrier_bytes"] = len(raw)
        if got != m.stage_sha256:
            return self._stop(REFUSED, STAGE_CARRIER_HASH_MISMATCH, f"the carrier hashes to {got[:12]}, the "
                              f"request pins {str(m.stage_sha256)[:12]}; nothing asked", facts)
        try:
            extract(raw, work)
        except (CarrierError, OSError) as err:
            return self._stop(REFUSED, STAGE_CARRIER_UNSAFE, f"the carrier was not unpacked ({str(err)[:80]}); "
                              "nothing asked", facts)
        try:
            with open(os.path.join(work, "UPDATE_TARGET.json"), encoding="utf-8") as fh:
                target = json.loads(fh.read(4096))
        except (OSError, ValueError):
            target = None
        if not target_ok(target, str(m.stage_slug), str(m.stage_from), str(m.stage_to)):
            return self._stop(REFUSED, STAGE_TARGET_MISMATCH, "the carrier's UPDATE_TARGET.json does not name this "
                              "App and these versions; nothing asked", facts)
        return None

    def _check(self, m: Manifest, work: str, facts: dict) -> Result | None:
        rc, out = self._script(work, "check")
        facts["check"] = self._seen(out)
        ready = (rc == 0 and out.get("CARRIER_VERIFIED") == "true" and out.get("SOURCE_BEFORE") == m.stage_from
                 and out.get("READY_TO_STAGE") == "true" and out.get("HB91_APP_UPDATE_RESULT") == "PASS")
        if ready:
            return None
        if rc != 0 and "HB91_APP_UPDATE_RESULT" not in out:
            return self._stop(FAILED, STAGE_SCRIPT_FAILED, f"the carrier check did not run to the end ({rc}, "
                              f"{out.get('STOP', 'no result')}); nothing asked or changed", facts)
        return self._stop(REFUSED, STAGE_CHECK_NOT_READY, f"the carrier check says the live source is "
                          f"{out.get('SOURCE_BEFORE', '?')} (needs {m.stage_from}), ready={out.get('READY_TO_STAGE', '?')}"
                          f", result={out.get('HB91_APP_UPDATE_RESULT', '?')}; nothing asked or changed", facts)

    def _ask(self, m: Manifest, label: str) -> str:
        lines = [
            f"Install {label} {m.stage_from} → {m.stage_to} (source from GitHub {str(m.stage_commit)[:7]}, checked).",
            "Plan: put the new source in place (verified backup of the old one) -> refresh the App store -> back up "
            "this App only -> update -> watch its health -> on any failure restore its backup and put the old "
            "source back.",
            "Home Assistant itself is not updated or restarted. This one tap covers the whole install.",
        ]
        text = "\n".join(lines)
        if m.note:
            text += f"\n\nAI note (advisory, not part of what you approve):\n{m.note}"
        text += f"\n\nRequest: {m.request_id}"
        self.j.audit(m.request_id, "APPROVAL_REQUESTED", job=JOB)
        decision = approval.ask(self.ha, self.s.notify_service, self.s.owner_user_id, stage="STAGE",
                                title=f"Maintenance: install {label} {m.stage_to}?", message=text,
                                timeout=self.s.approval_timeout, require_auth=self.s.require_auth,
                                board=self.s.board, open_url=self.s.open_url,
                                should_stop=getattr(self.s, "should_stop", None))
        self.j.audit(m.request_id, "APPROVAL_" + decision.outcome, channel=decision.channel,
                     ignored=decision.ignored_events)
        return decision.outcome

    def _reload(self, slug: str) -> None:
        with self.ha.stage_target(slug):
            self.ha.reload_store()

    def _wait_offered(self, slug: str, version: str) -> bool:
        deadline = time.monotonic() + self.p.reload_seconds
        while True:
            if self.ha.app_detail(slug).version_latest == version:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(self.p.poll)

    # -- the job ----------------------------------------------------------------------------------------------
    def run(self, m: Manifest) -> Result:
        facts = {"slug": m.stage_slug, "from": m.stage_from, "to": m.stage_to, "carrier": m.stage_carrier,
                 "commit": m.stage_commit, "sha256": m.stage_sha256, "automatic": False}
        bad = self._invalid(m)
        if bad:
            return self._stop(REFUSED, bad, "only the Inspector, Observer or Inventory Scout local source, from a "
                              "pinned carrier on main, can be staged; nothing asked", facts)
        slug, frm, to = str(m.stage_slug), str(m.stage_from), str(m.stage_to)
        label = STAGE_APPS[slug][1]
        if self.j.load_doc(DECLINED_DOC, {}).get(f"{slug}@{to}"):
            return self._stop(REFUSED, STAGE_REJECTED_BEFORE, f"you rejected installing {label} {to}; a Reject is "
                              "final and is never asked again", facts)
        held = self.updater.held(slug, to) if self.updater is not None else None
        if held:
            return self._stop(REFUSED, STAGE_UPDATE_HELD, f"update held: {held}; nothing asked (release it first)",
                              facts)
        if self.after_active():
            return self._stop(REFUSED, STAGE_AFTER_UPDATE_OPEN, "an after-update check is still open (Reload/Undo); "
                              "no new update starts until it closes", facts)
        app = next((a for a in self.ha.installed_apps() if a.slug == slug), None)
        if app is None:
            return self._stop(REFUSED, STAGE_APP_NOT_INSTALLED, f"{slug} is not installed; nothing asked", facts)
        facts["state_before"] = app.state
        if app.version != frm:
            return self._stop(REFUSED, STAGE_APP_VERSION_MISMATCH, f"{label} runs {app.version}, the request "
                              f"expects {frm}; nothing asked", facts)
        if app.update_available or (app.version_latest and app.version_latest != app.version):
            return self._stop(REFUSED, STAGE_UPDATE_ALREADY_OFFERED, f"the App store already offers {label} "
                              f"{app.version_latest}; that update is asked the normal way; nothing asked", facts)
        work = tempfile.mkdtemp(prefix="hbm-stage-", dir=self.p.work_root)
        try:
            stop = self._fetch(m, work, facts) or self._check(m, work, facts)
            if stop is not None:
                return stop
            if self.s.dry_run:
                return Result(DRY_RUN_OK, ["dry run: carrier verified and checked, nothing asked or changed"],
                              {**facts, "would_ask": f"Install {label} {frm} → {to}"})
            decision = self._ask(m, label)
            if decision != approval.APPROVE:
                if decision == approval.REJECT:
                    declined = self.j.load_doc(DECLINED_DOC, {})
                    declined[f"{slug}@{to}"] = time.time()
                    self.j.save_doc(DECLINED_DOC, declined)
                reasons = [f"owner decision: {decision}"]
                if decision != approval.REJECT:
                    reasons.append("nothing changed; file a new request to ask again")
                return Result(REJECTED, reasons, facts)
            self.j.save_doc(PENDING_DOC, {"request_id": m.request_id, "slug": slug, "from": frm, "to": to,
                                          "carrier": m.stage_carrier, "commit": m.stage_commit,
                                          "sha256": m.stage_sha256, "state_before": app.state,
                                          "issue": m.tracking_issue, "digest": m.digest, "started_at": time.time()})
            # 0.8.1 (found by #975): keep the pending record on an uncertain result or an exception; an effect
            # may already have happened although its answer was lost, so Undo evidence must not be thrown away.
            result = self._install(m, work, app, label, facts)
            if result.outcome != FAILED_MANUAL and not self.j.load_txn():
                self.j.save_doc(PENDING_DOC, {})
            return result
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _install(self, m: Manifest, work: str, app, label: str, facts: dict) -> Result:
        slug, frm, to = str(m.stage_slug), str(m.stage_from), str(m.stage_to)
        self.j.audit(m.request_id, "STAGE_STAGING", slug=slug, to=to)
        rc, out = self._script(work, "stage")
        facts["stage"] = self._seen(out)
        if not (rc == 0 and out.get("HB91_APP_UPDATE_RESULT") == "PASS" and out.get("SOURCE_AFTER") == to):
            # The carrier restores its own swap when its verification fails; read what the folder holds now.
            _, now = self._script(work, "check")
            facts["after_failed_stage"] = self._seen(now)
            if now.get("SOURCE_BEFORE") == frm:
                return self._stop(FAILED, STAGE_STAGE_FAILED, f"staging stopped ({out.get('STOP', rc)}); the "
                                  f"live source is still {frm}; nothing else changed", facts)
            if now.get("SOURCE_BEFORE") == to:
                return self._undo(m, work, facts, FAILED, STAGE_STAGE_FAILED,
                                  f"staging reported a problem ({out.get('STOP', rc)})")
            return self._stop(FAILED_MANUAL, STAGE_STAGE_FAILED, f"staging stopped ({out.get('STOP', rc)}) and the "
                              "live source is neither version; look at the App folder (carrier undo)", facts)
        try:
            self._reload(slug)
            offered = self._wait_offered(slug, to)
        except (HAError, net.NetError, ForbiddenCall) as err:
            return self._undo(m, work, facts, FAILED, STAGE_STORE_NOT_REFRESHED,
                              f"store reload failed ({net.redact(str(err))[:120]})")
        if not offered:
            return self._undo(m, work, facts, FAILED, STAGE_STORE_NOT_REFRESHED,
                              f"the App store did not offer {label} {to} after the reload")
        try:
            current = next((a for a in self.ha.installed_apps() if a.slug == slug), app)
            rev = self.updater.review_app(current, self.updater._mem())
        except (HAError, net.NetError, ForbiddenCall) as err:
            return self._undo(m, work, facts, FAILED, STAGE_UPDATE_FAILED,
                              f"the update review failed ({net.redact(str(err))[:120]})")
        facts.update({"verdict": rev.verdict, "review": list(rev.reasons)[:4]})
        if rev.verdict == BLOCKED or rev.to_version != to:
            return self._undo(m, work, facts, REFUSED, STAGE_UPDATE_BLOCKED,
                              "update held by the review: " + "; ".join(rev.reasons)[:200])
        self.j.audit(m.request_id, "STAGE_UPDATING", slug=slug, to=to)
        try:
            out_upd = self.updater.apply(m.request_id, rev, prior_state=app.state, automatic=False)
        except (HAError, net.NetError, ForbiddenCall) as err:
            out_upd = None
            facts["update_error"] = net.redact(str(err))[:200]
        if out_upd is not None:
            facts.update({k: v for k, v in out_upd.facts.items() if k in ("backup", "failure", "quarantined")})
            facts["update_outcome"] = out_upd.result_outcome
        if out_upd is not None and out_upd.result_outcome == DONE:
            reasons = [f"{STAGE_INSTALLED}: installed {label} {frm} -> {to} from GitHub "
                       f"{str(m.stage_commit)[:7]}; healthy after the watch"]
            reasons += self._start_if_was_running(slug, app.state)
            return Result(DONE, reasons, {**facts, "reason_code": STAGE_INSTALLED})
        if out_upd is not None and out_upd.result_outcome == FAILED_MANUAL and self.j.load_txn():
            # The update itself is still being checked (journalled); its own recovery decides. Never undo under it.
            return self._stop(FAILED_MANUAL, STAGE_UPDATE_FAILED, "the update could not be confirmed; it is re-checked "
                              f"on the next poll; the new source stays until then ({'; '.join(out_upd.reasons)[:160]})",
                              facts)
        why = "; ".join(out_upd.reasons)[:200] if out_upd is not None else facts.get("update_error", "error")
        base = RESTORED if out_upd is not None and out_upd.result_outcome == RESTORED else FAILED
        return self._undo(m, work, facts, base, STAGE_UPDATE_FAILED, f"the update did not finish ({why})")

    def _start_if_was_running(self, slug: str, before: str) -> list[str]:
        if before != "started":
            return [f"the App was {before} before; it is not started"]
        if self.ha.app_detail(slug).state == "started":
            return ["the App runs again, as before"]
        try:
            with self.ha.fix_target("start_app", slug):
                self.ha.start_app(slug)
        except (HAError, net.NetError, ForbiddenCall) as err:
            return [f"the App was running before but could not be started ({net.redact(str(err))[:120]})"]
        return ["the App was running before; started it"]

    def _undo(self, m: Manifest, work: str, facts: dict, outcome: str, code: str, why: str) -> Result:
        """Failure after ``stage``: the carrier's own ``undo`` (its verified backup), then a store reload."""
        self.j.audit(m.request_id, "STAGE_UNDOING", reason=code)
        rc, out = self._script(work, "undo")
        facts["undo"] = self._seen(out)
        undone = rc == 0 and out.get("HB91_APP_UPDATE_RESULT") == "PASS" and out.get("SOURCE_AFTER") == m.stage_from
        try:
            self._reload(str(m.stage_slug))
            reloaded = "store reloaded"
        except (HAError, net.NetError, ForbiddenCall) as err:
            reloaded = f"store reload failed ({net.redact(str(err))[:80]})"
        if not undone:
            return Result(FAILED_MANUAL, [f"{code}: {why}", f"{STAGE_UNDO_FAILED}: the old source could not be put back "
                                          f"({out.get('STOP', rc)}); run the carrier's undo by hand", reloaded],
                          {**facts, "reason_code": STAGE_UNDO_FAILED})
        return Result(outcome, [f"{code}: {why}", f"old source {m.stage_from} put back (carrier undo); {reloaded}"],
                      {**facts, "reason_code": code})

    # -- recovery after a restart mid-job ----------------------------------------------------------------------
    def recover(self, doc: dict) -> tuple[str, Result]:
        """The App restarted after Approve. Read what is true now and undo a staged source the App does not run."""
        rid = str(doc.get("request_id", "unknown"))
        facts = {k: doc.get(k) for k in ("slug", "from", "to", "carrier", "commit", "sha256")}
        facts["recovered"] = True
        m = Manifest(request_id=rid, job=JOB, requested_by="owner", tracking_issue=None, note="",
                     expect_scout_version="", expect_key_fingerprint=None, digest=str(doc.get("digest", "")),
                     stage_carrier=doc.get("carrier"), stage_sha256=doc.get("sha256"), stage_commit=doc.get("commit"),
                     stage_slug=doc.get("slug"), stage_from=doc.get("from"), stage_to=doc.get("to"))
        if self._invalid(m):
            return rid, self._stop(FAILED_MANUAL, STAGE_RESTARTED_MID_JOB, "the saved install record is not valid; "
                                   "look at the App folder", facts)
        work = tempfile.mkdtemp(prefix="hbm-stage-", dir=self.p.work_root)
        try:
            stop = self._fetch(m, work, facts)
            if stop is not None:
                return rid, self._stop(FAILED_MANUAL, STAGE_RESTARTED_MID_JOB, "after a restart the carrier could "
                                       f"not be read again ({stop.reasons[0][:120]}); look at the App folder", facts)
            _, out = self._script(work, "check")
            facts["check"] = self._seen(out)
            installed = self.ha.app_detail(str(m.stage_slug)).version
            facts["installed"] = installed
            if out.get("SOURCE_BEFORE") == m.stage_from:
                return rid, self._stop(FAILED, STAGE_RESTARTED_MID_JOB, "the App restarted before the source was "
                                       f"staged; nothing changed ({m.stage_slug} {installed})", facts)
            if out.get("SOURCE_BEFORE") == m.stage_to and installed == m.stage_to:
                return rid, Result(DONE, [f"{STAGE_INSTALLED}: after a restart {m.stage_slug} runs {m.stage_to} "
                                          "from the staged source"], {**facts, "reason_code": STAGE_INSTALLED})
            if out.get("SOURCE_BEFORE") == m.stage_to:
                return rid, self._undo(m, work, facts, FAILED, STAGE_RESTARTED_MID_JOB,
                                       f"the App restarted during the install ({m.stage_slug} runs {installed})")
            return rid, self._stop(FAILED_MANUAL, STAGE_RESTARTED_MID_JOB, "after a restart the live source is neither "
                                   "version; look at the App folder", facts)
        finally:
            shutil.rmtree(work, ignore_errors=True)
