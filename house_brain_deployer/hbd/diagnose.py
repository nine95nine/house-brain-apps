"""Plain-English reasons for everything that stops, holds or slows the Deployer (0.3.1).

Every condition that used to be silent (daily approval limit, freeze, recovery, id reuse,
unreadable manifest) or cryptic (HTTP codes, "UNAVAILABLE") becomes a Reason with what is
wrong and what to do. Reasons are shown on the approval page, in the status entity and the
log (on change), and - for held requests - once in a push and once on the tracking issue.
Nothing here changes a decision; it only explains one. Text is always secret-redacted.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from . import ghauth, net

ERROR, HOLD, INFO = "error", "hold", "info"
ERROR_NOTIFY_STREAK = 3          # consecutive failed polls before an error push (~15 min at 5-min polls)


@dataclass(frozen=True)
class Reason:
    code: str
    severity: str
    text: str
    fix: str = ""
    request_id: str = ""

    @property
    def key(self) -> str:
        return f"{self.code}:{self.request_id}"

    def line(self) -> str:
        who = f"[{self.request_id}] " if self.request_id else ""
        out = f"{who}{self.text}"
        if self.fix:
            out += f" What to do: {self.fix}"
        return net.redact(out)[:600]


def local_time(epoch: float) -> str:
    return time.strftime("%I:%M %p %a %b %d", time.localtime(epoch)).lstrip("0")


# -- errors ----------------------------------------------------------------------------------
def _detail(err: BaseException) -> str:
    return net.redact(f"{type(err).__name__}: {err}")[:200]


def github_error(err: BaseException, *, branch: str = "", repo: str = "") -> Reason:
    status = getattr(err, "status", None)
    code = getattr(err, "code", "")
    explained = ghauth.explain(code, "Deployer") if isinstance(code, str) else None
    if explained:     # Credential Autopilot (0.3.6): GitHub App sign-in faults
        return Reason(code, ERROR, *explained)
    msg = str(err).lower()
    d = _detail(err)
    renew_fix = ("Best: switch to the GitHub App (no token to renew): open the Deployer page -> GitHub connection "
                 "-> Connect to GitHub. Or create a new fine-grained GitHub token (this repository only; Contents: "
                 "Read-only; Issues: Read and write) and paste it into the app's Configuration -> github_token, "
                 "then Save. Never screenshot that page.")
    if status == 401:
        return Reason("GITHUB_TOKEN_REJECTED", ERROR,
                      "GitHub rejected the token: it expired, was revoked, or was pasted wrong.", renew_fix)
    if status == 403 and ("rate limit" in msg or "secondary rate" in msg):
        return Reason("GITHUB_RATE_LIMIT", ERROR,
                      "GitHub's own API rate limit was reached. It resets by itself within an hour.",
                      "Nothing; the Deployer retries automatically.")
    if status == 403:
        return Reason("GITHUB_TOKEN_NO_ACCESS", ERROR,
                      f"The token works but may not read/comment on {repo or 'the repository'} "
                      "(missing repository access or permissions).", renew_fix)
    if status == 404 and "branch not found" in msg:
        return Reason("GITHUB_BRANCH_MISSING", ERROR,
                      f"The requests branch '{branch}' does not exist on GitHub (nothing has been filed yet, "
                      "or requests_branch is misspelled).",
                      "Nothing if no request was filed; otherwise ask the AI to push the request branch.")
    if status == 404:
        return Reason("GITHUB_NOT_FOUND", ERROR,
                      f"GitHub says the repository or file was not found ({repo}). The repository name may be "
                      "wrong, or the token was not given access to this repository.", renew_fix)
    if status == 422 or status == 409:
        return Reason("GITHUB_REFUSED", ERROR, f"GitHub refused the request ({d}).",
                      "Ask the AI to check the request branch and commit.")
    if isinstance(status, int) and status >= 500:
        return Reason("GITHUB_OUTAGE", ERROR, f"GitHub is having a problem (HTTP {status}).",
                      "Nothing; the Deployer retries automatically.")
    if status == 0:
        if any(k in msg for k in ("name or service not known", "temporary failure in name resolution",
                                  "nodename nor servname", "gaierror", "no address associated")):
            return Reason("NO_DNS", ERROR,
                          "Home Assistant cannot look up github.com (DNS or internet is down).",
                          "Check the internet connection / router; the Deployer retries automatically.")
        if "timed out" in msg or "timeout" in msg:
            return Reason("GITHUB_TIMEOUT", ERROR, "GitHub did not answer in time (slow or no internet).",
                          "Nothing if it clears; otherwise check the internet connection.")
        if "certificate" in msg or "ssl" in msg:
            return Reason("TLS_FAILED", ERROR,
                          "The secure connection to GitHub failed (wrong system clock, or something on the "
                          "network is intercepting HTTPS).",
                          "Check Settings -> System -> General time zone/clock, and any network filter.")
        if "refused" in msg or "unreachable" in msg or "no route" in msg:
            return Reason("NO_INTERNET", ERROR, "Home Assistant cannot reach the internet.",
                          "Check the internet connection; the Deployer retries automatically.")
        if "branch head missing" in msg:
            return Reason("GITHUB_BAD_ANSWER", ERROR, "GitHub answered without a branch commit.",
                          "Nothing; retried automatically.")
    return Reason("GITHUB_ERROR", ERROR, f"Talking to GitHub failed: {d}.",
                  "If it repeats, send this message to the AI.")


def ha_error(err: BaseException, *, notify_service: str = "") -> Reason:
    status = getattr(err, "status", None)
    code = getattr(err, "code", "")
    msg = str(err)
    low = msg.lower()
    d = _detail(err)
    if code == "OWNER_NOT_FOUND":
        return Reason("OWNER_NOT_FOUND", ERROR,
                      "owner_username does not match exactly one active Home Assistant login ("
                      + msg.split(": ", 1)[-1][:200] + ").",
                      "Set owner_username in the app's Configuration to your login name, then Save.")
    if getattr(err, "what", "") == "notify" or ("notify" in low and status in (400, 404)):
        return Reason("NOTIFY_SERVICE_MISSING", ERROR,
                      f"The phone notification service 'notify.{notify_service}' does not exist, so no approval "
                      "request can reach the phone.",
                      "Developer Tools -> Actions -> type 'notify.mobile_app' to see the right name; put it in "
                      "notify_service (without 'notify.').")
    if status in (401, 403):
        return Reason("HA_ACCESS_DENIED", ERROR,
                      "Home Assistant refused the app's access (its Supervisor token or role).",
                      "Restart the app; if it repeats, Rebuild it from its page.")
    if code in ("WS_AUTH", "WS_HANDSHAKE"):
        return Reason("HA_ACCESS_DENIED", ERROR,
                      "Home Assistant refused the app's live connection (websocket login failed).",
                      "Restart the app; if it repeats, Rebuild it from its page.")
    if (status == 0 or "connection" in low or code in ("WS_TIMEOUT", "WS_SHAPE")
            or type(err).__module__.startswith("websockets")
            or isinstance(err, (ConnectionError, TimeoutError, OSError))):
        return Reason("HA_UNREACHABLE", ERROR,
                      "Home Assistant Core did not answer (it may be restarting or overloaded).",
                      "Nothing if it clears within a few minutes.")
    return Reason("HA_ERROR", ERROR, f"Talking to Home Assistant failed: {d}.",
                  "If it repeats, send this message to the AI.")


def classify(err: BaseException, *, branch: str, repo: str, notify_service: str) -> Reason:
    if getattr(err, "service", "") == "github":
        return github_error(err, branch=branch, repo=repo)
    if (getattr(err, "service", "") == "home_assistant" or type(err).__name__ == "HAError"
            or type(err).__module__.startswith("websockets")):
        return ha_error(err, notify_service=notify_service)
    return Reason("UNEXPECTED", ERROR, f"Unexpected problem: {_detail(err)}.",
                  "If it repeats, send this message to the AI.")


def approval_problem(detail: str, notify_service: str) -> str:
    """Explain an approval that could not even be asked (Decision UNAVAILABLE)."""
    low = (detail or "").lower()
    if "notify" in low or "400" in low or "404" in low:
        return (f"The approval request could not be sent to the phone: 'notify.{notify_service}' failed "
                f"({net.redact(detail)[:120]}). Check notify_service and that the Companion app is signed in.")
    return ("The approval request could not be delivered (lost connection to Home Assistant: "
            f"{net.redact(detail)[:120]}). Nothing was changed; the request can be filed again.")


# -- holds and information -------------------------------------------------------------------
def rate_limited(request_id: str, used: int, limit: int, frees_at: float | None, lookup: bool = False) -> Reason:
    what = "lookup" if lookup else "install"
    when = f" It will be asked automatically after {local_time(frees_at)}." if frees_at else ""
    fix = "Tap 'Allow more today' on the Deployer page (lifts the limit until midnight), or wait."
    if limit < 12:
        fix += " You can also raise max_approval_requests_per_day (up to 12) in the app's Configuration."
    counted = ("read-only lookups have their own allowance" if lookup
               else "restart approvals and read-only lookups do not count")
    return Reason("DAILY_APPROVAL_LIMIT", HOLD,
                  f"Waiting: the daily {what} limit is reached ({used} of {limit} {what}s asked in the last 24 "
                  f"hours; {counted}).{when}", fix, request_id)


def next_local_midnight(now: float) -> float:
    lt = time.localtime(now)
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, 0, 0, 0, 0, 0, -1))


def frozen(request_id: str) -> Reason:
    return Reason("FROZEN", HOLD,
                  f"Paused for safety: request {request_id} could not be rolled back automatically, so no new "
                  "request is processed until you confirm the files were checked.",
                  f"After the files are checked, set clear_freeze_for to {request_id} in the app's "
                  "Configuration and Save.")


def recovering(request_id: str) -> Reason:
    return Reason("RECOVERING", HOLD,
                  f"Finishing or undoing an interrupted install ({request_id}) before anything else.",
                  "Nothing; this completes by itself.", request_id)


def id_reused(request_id: str) -> Reason:
    return Reason("REQUEST_ID_REUSED", HOLD,
                  "This request id was already processed, and its manifest has since changed. A processed id "
                  "is never run again (from 0.3.8 a request that timed out can be asked again under a NEW id "
                  "with a re-ask request).",
                  "Ask the AI to file the change under a new request id.", request_id)


def reask_refused(code: str, request_id: str, reask_of: str, detail: str = "") -> Reason:
    """0.3.8 (owner pop-up 2026-10-08): why a re-ask request was refused. Final for that re-ask id."""
    new_request = "Nothing. If the change is still wanted, the AI files it as a new request (new id)."
    if code == "REASK_UNKNOWN":
        return Reason(code="REASK_UNKNOWN", severity=HOLD, request_id=request_id,
                      text=f"Re-ask refused: {reask_of} was never processed by this Deployer (or only in practice "
                           "mode), so there is nothing to ask again.",
                      fix="Ask the AI to check the request id, or to file the change as a normal new request.")
    if code == "REASK_OF_REASK":
        return Reason(code="REASK_OF_REASK", severity=HOLD, request_id=request_id,
                      text=f"Re-ask refused: {reask_of} is itself a re-ask. A re-ask must name the original request.",
                      fix="Ask the AI to file a re-ask whose reask_of is the original request id.")
    if code == "REASK_NOT_TIMED_OUT" and detail == "REJECTED":
        return Reason(code="REASK_NOT_TIMED_OUT", severity=HOLD, request_id=request_id,
                      text=f"Re-ask refused: {reask_of} was rejected. Reject is final; a rejected request is never "
                           "asked again.", fix=new_request)
    if code == "REASK_NOT_TIMED_OUT":
        return Reason(code="REASK_NOT_TIMED_OUT", severity=HOLD, request_id=request_id,
                      text="Re-ask refused: only a request whose approval expired unanswered (TIMED_OUT, or "
                           "ROLLED_BACK because the restart approval expired) can be asked again; "
                           f"{reask_of} (or its latest re-ask) ended {detail or 'differently'}.",
                      fix=new_request)
    if code == "REASK_LIMIT":
        return Reason(code="REASK_LIMIT", severity=HOLD, request_id=request_id,
                      text=f"Re-ask refused: {reask_of} was already asked again {detail or '2'} times (the limit is "
                           "2 re-asks per request).",
                      fix="If the change is still wanted, ask the AI to file it as a new request (new id).")
    if code == "REASK_ORIGINAL_CHANGED":
        return Reason(code="REASK_ORIGINAL_CHANGED", severity=HOLD, request_id=request_id,
                      text=f"Re-ask refused: the manifest of {reask_of} on the requests branch is "
                           f"{detail or 'changed'}; a re-ask only re-runs the exact bytes that were asked the first "
                           "time.",
                      fix="Ask the AI to file the change as a new request (new id) instead of a re-ask.")
    if code == "REASK_LOOKUP":
        return Reason(code="REASK_LOOKUP", severity=HOLD, request_id=request_id,
                      text=f"Re-ask refused: {reask_of} is a read-only lookup; lookups are not re-asked.",
                      fix="Ask the AI to file the lookup again under a new request id.")
    return Reason(code, HOLD, f"Re-ask refused ({code}).", "Ask the AI to file a new request.", request_id)


def unreadable(request_id: str, err: BaseException) -> Reason:
    return Reason("MANIFEST_UNREADABLE", HOLD, f"Could not download this request's manifest ({_detail(err)}).",
                  "Nothing if it clears on the next check.", request_id)


def dry_run() -> Reason:
    return Reason("PRACTICE_MODE", INFO,
                  "Practice mode (dry_run) is ON: requests are checked and approved but no file is changed.",
                  "Turn dry_run off in the app's Configuration for real installs.")


def owner_unresolved(err: BaseException, notify_service: str) -> Reason:
    return ha_error(err, notify_service=notify_service)


class Tracker:
    """Current reasons for one poll; logs changes; decides one-shot error pushes."""

    def __init__(self, log: logging.Logger) -> None:
        self.log = log
        self.reasons: list[Reason] = []
        self._keys: set[str] = set()
        self._current: list[Reason] = []
        self.last_check: float | None = None
        self.last_ok: float | None = None
        self.checked_requests = 0
        self.streak: dict[str, int] = {}
        self._error_pushed: set[str] = set()

    def begin(self) -> None:
        self._current = []
        self.checked_requests = 0

    def add(self, reason: Reason) -> None:
        if reason.key not in {r.key for r in self._current}:
            self._current.append(reason)

    def end(self, now: float) -> list[Reason]:
        """Close the poll; return errors that just reached the push threshold."""
        keys = {r.key for r in self._current}
        for r in self._current:
            if r.key not in self._keys:
                level = logging.WARNING if r.severity in (ERROR, HOLD) else logging.INFO
                self.log.log(level, "%s: %s", r.code, r.line())
        for gone in sorted(self._keys - keys):
            self.log.info("resolved: %s", gone.split(":", 1)[0])
        errors = {r.code for r in self._current if r.severity == ERROR}
        self.streak = {c: self.streak.get(c, 0) + 1 for c in errors}
        self._error_pushed &= errors
        due = [r for r in self._current if r.severity == ERROR and self.streak[r.code] >= ERROR_NOTIFY_STREAK
               and r.code not in self._error_pushed]
        self._error_pushed |= {r.code for r in due}
        self.reasons, self._keys, self.last_check = list(self._current), keys, now
        if not errors:
            self.last_ok = now
        return due

    def state(self) -> str:
        sev = {r.severity for r in self.reasons}
        return "ERROR" if ERROR in sev else "HELD" if HOLD in sev else "OK"

    def lines(self) -> list[str]:
        return [r.line() for r in self.reasons]
