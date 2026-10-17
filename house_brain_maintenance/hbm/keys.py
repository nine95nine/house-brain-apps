"""0.8.0 automatic Broker keys (owner pop-ups K1-K4 2026-10-11; docs/architecture/HOUSE_BRAIN_AUTOMATIC_KEY_ROTATION_R1.md).

Goal (owner): "once it's set up, I want to be involved as little as possible, especially with things like swapping
out keys ... done automatically where I don't have to worry about it at all."

* This App holds a **rotation key** that no other App ever sees. The Broker (0.6.0) learns only its fingerprint
  (first from the plain var ``ROTATION_KEY_SHA256``; later through its own rotation). With it this App asks the
  Broker to accept a new key for one role, switches the App that uses that role, checks it works, then commits.
* Roles and the Apps that hold them: ``observer_write`` (Observer), ``inspector_entity`` (Inspector), ``ai_read``
  (AI Access), ``scout_inventory`` (Scout), and ``rotation`` (this App).
* One role per night inside the automatic-update window; each role every ``key_rotate_days``. The first automatic
  change asks the owner once; after that nothing is asked. Only a failure is reported (#454 + one push).
* Standing permission (K3/K4): set the key option of, and restart, only the Observer, AI Access and Inspector,
  only right after their key changed (and to roll that back). A stopped App is never started.
* Any failure before the commit rolls back: the old option value is written back, the App restarted only if it
  was running, the Broker's pending key dropped. The old key is still the Broker's ``current`` until the commit,
  so a failed change can never lock an App out.
* Keys live in ``/data/keys.json`` (mode 0600). They are registered for redaction and never logged or reported:
  only 12-hex fingerprint prefixes leave this App.
"""
from __future__ import annotations

import calendar
import os
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable

from . import approval, issues as I, net
from .ha import KEY_NO_RESTART, KEY_OPTIONS, ForbiddenCall, HAError, HomeAssistant, fingerprint

ROLES = ("observer_write", "inspector_entity", "ai_read", "scout_inventory", "rotation")
APP_NAMES = {"observer_write": "Engineering Observer", "inspector_entity": "Engineering Inspector",
             "ai_read": "AI Access", "scout_inventory": "Inventory Scout", "rotation": "this App's rotation key"}
DOC = "keys"
JOB = "ROTATE_KEY"
SETUP_JOB = "KEYS_SETUP"
DONE, SKIPPED, ROLLED_BACK, FAILED, FAILED_MANUAL, REFUSED = (
    "DONE", "SKIPPED", "ROLLED_BACK", "FAILED", "FAILED_MANUAL", "REFUSED")
RETRY_AFTER_FAILURE = 20 * 3600          # a failed or skipped role is tried again the next night, not every poll
STATUS_EVERY = 3600                      # Broker status read at most hourly outside a change
UNKNOWN_HOLDER = "unknown_holder"


@dataclass
class KeyPolicy:
    rotate_days: float = 90.0
    window: tuple[int, int] = (2, 5)
    enabled: bool = True
    probe_wait: float = 90.0             # the Broker caches its registry for 30 s
    verify_seconds: float = 600.0        # the Observer publishes every 90 s, the Inspector every 5 min
    restart_seconds: float = 120.0
    poll: float = 5.0
    reask_hours: float = 24.0
    ask_hours: tuple[int, int] = (9, 21)  # the one first-change question is asked in the daytime only


@dataclass
class Outcome:
    request_id: str
    result_outcome: str
    reasons: list[str]
    facts: dict = field(default_factory=dict)
    notify: bool = False


class BrokerError(RuntimeError):
    def __init__(self, status: int, reason: str = "") -> None:
        super().__init__(f"Broker HTTP {status} {reason}".strip()[:200])
        self.status = status
        self.reason = reason


class Broker:
    """The Broker's key routes (0.6.0). ``base`` is ``https://<host>``."""

    def __init__(self, base: str, timeout: float = 20.0) -> None:
        self.base = base.rstrip("/")
        self.timeout = timeout

    def _call(self, method: str, path: str, key: str, body: dict | None = None):
        try:
            return net.request(method, self.base + path, {"Authorization": f"Bearer {key}"}, body=body,
                               timeout=self.timeout)
        except net.NetError as err:
            reason = ""
            text = str(err)
            for code in ("fingerprint_reused", "rotation_in_progress", "no_pending", "pending_mismatch",
                         "commit_needs_new_key", "key_registry_unavailable", "rotation_not_commissioned"):
                if code in text:
                    reason = code
            raise BrokerError(err.status, reason) from None

    def status(self, rot: str) -> dict:
        _, data = self._call("GET", "/v1/keys/status", rot)
        if not isinstance(data, dict) or not isinstance(data.get("roles"), dict):
            raise BrokerError(0, "status_shape")
        return data

    def rotate(self, rot: str, role: str, fp: str) -> None:
        self._call("POST", "/v1/keys/rotate", rot, {"role": role, "next_sha256": fp})

    def commit(self, key: str, role: str, fp: str) -> None:
        self._call("POST", "/v1/keys/commit", key, {"role": role, "sha256": fp})

    def abort(self, rot: str, role: str) -> None:
        self._call("POST", "/v1/keys/abort", rot, {"role": role})

    def probe(self, key: str, role: str) -> bool:
        try:
            status, _ = net.request("GET", f"{self.base}/v1/keys/probe?{urllib.parse.urlencode({'role': role})}",
                                    {"Authorization": f"Bearer {key}"}, timeout=self.timeout)
        except net.NetError as err:
            if err.status == 401:
                return False
            raise BrokerError(err.status) from None
        return status == 204


def _write_private(path: str, obj: dict) -> None:
    import json
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class KeyRotator:
    def __init__(self, ha: HomeAssistant, journal, settings, policy: KeyPolicy, broker_base: Callable[[], str],
                 clock: Callable[[], float] = time.time, sleep: Callable[[float], None] = time.sleep) -> None:
        self.ha = ha
        self.j = journal
        self.s = settings
        self.p = policy
        self.broker_base = broker_base
        self.clock = clock
        self.sleep = sleep
        self.broker_factory = Broker

    # -- state ----------------------------------------------------------------------------------------
    def _doc(self) -> dict:
        doc = self.j.load_doc(DOC, {})
        doc = doc if isinstance(doc, dict) else {}
        doc.setdefault("roles", {})
        doc.setdefault("queue", [])
        for name in ("rotation_key",):
            if isinstance(doc.get(name), str):
                net.register_secret(doc[name])
        infl = doc.get("inflight")
        if isinstance(infl, dict):
            for name in ("new_key", "old_value"):
                if isinstance(infl.get(name), str):
                    net.register_secret(infl[name])
        return doc

    def _save(self, doc: dict) -> None:
        _write_private(os.path.join(self.j.dir, f"{DOC}.json"), doc)

    def _broker(self) -> Broker:
        return self.broker_factory(self.broker_base())

    def ensure_rotation_key(self) -> dict:
        doc = self._doc()
        if not isinstance(doc.get("rotation_key"), str):
            key = secrets.token_urlsafe(48)                 # 64 chars, 384 random bits
            net.register_secret(key)
            doc.update(rotation_key=key, rotation_fp=fingerprint(key), created_at=self.clock())
            self._save(doc)
        return doc

    # -- visibility -----------------------------------------------------------------------------------
    def summary(self) -> dict:
        """Status-entity attributes: per role age (days), last result; no key, no full fingerprint."""
        doc = self._doc()
        now = self.clock()
        roles = {}
        for role in ROLES:
            rec = doc["roles"].get(role) or {}
            at = rec.get("rotated_at")
            roles[role] = {"age_days": round((now - at) / 86400, 1) if isinstance(at, (int, float)) else None,
                           "last": rec.get("last"),
                           "next_due_days": (round(max(0.0, at + self.p.rotate_days * 86400 - now) / 86400, 1)
                                             if isinstance(at, (int, float)) else None)}
        return {"keys_enabled": self.p.enabled, "keys_enrolled": bool(doc.get("enrolled")),
                "keys_first_approved": bool(doc.get("first_approved")), "keys": roles}

    def findings(self) -> list[I.Finding]:
        """Problems for the morning summary / one push (owner: tell me only if something fails)."""
        doc = self._doc()
        now = self.clock()
        out: list[I.Finding] = []
        if not self.p.enabled:
            return out
        for role in ROLES:
            rec = doc["roles"].get(role) or {}
            name = APP_NAMES[role]
            if rec.get("last") in (FAILED, FAILED_MANUAL, ROLLED_BACK) and int(rec.get("failures") or 0) >= 2:
                out.append(I.Finding(f"keys:{role}:failed", "credential_keys", I.WARNING,
                                     f"Automatic key change for {name} did not finish; its old key still works",
                                     steps=(str(rec.get("note") or "")[:200] or "Details are on GitHub issue #454.",
                                            "Nothing to do: it is tried again the next night."),
                                     facts={"role": role, "last": rec.get("last")}))
            holder = rec.get(UNKNOWN_HOLDER)
            if isinstance(holder, (int, float)) and now - holder < 7 * 86400:
                out.append(I.Finding(f"keys:{role}:holder", "credential_keys", I.WARNING,
                                     f"Something besides {name} still uses its old Broker key",
                                     steps=("The old key stops working when its grace time ends; whatever still uses "
                                            "it will stop reading or sending then.",
                                            "Details are on GitHub issue #454."),
                                     facts={"role": role}, announce_clear=False))
            at = rec.get("rotated_at")
            if doc.get("first_approved") and isinstance(at, (int, float)) \
                    and now - at > 2 * self.p.rotate_days * 86400:
                out.append(I.Finding(f"keys:{role}:overdue", "credential_keys", I.WARNING,
                                     f"{name}'s Broker key was not changed for {int((now - at) / 86400)} days",
                                     steps=("Automatic key changes keep failing or are paused; details on #454.",),
                                     facts={"role": role}))
        created = doc.get("created_at")
        if not doc.get("enrolled") and isinstance(created, (int, float)) and now - created > 3 * 86400:
            out.append(I.Finding("keys:not_enrolled", "credential_keys", I.WARNING,
                                 "Automatic key changes are not set up yet",
                                 steps=("Waiting for the Broker update that knows this App's rotation key "
                                        "(an AI files it; you tap Promote once).",),
                                 facts={"rotation_fingerprint_prefix": str(doc.get("rotation_fp", ""))[:12]}))
        return out

    # -- requests ---------------------------------------------------------------------------------------
    def queue(self, role: str) -> list[str]:
        """``ROTATE_KEYS_NOW``: rotate ``role`` (or ``all``) as soon as possible, outside the night window."""
        doc = self._doc()
        wanted = list(ROLES) if role == "all" else [role]
        for r in wanted:
            if r not in doc["queue"]:
                doc["queue"].append(r)
        self._save(doc)
        return list(doc["queue"])

    # -- the loop step ----------------------------------------------------------------------------------
    def cycle(self) -> Outcome | None:
        """At most one action per call. None: nothing done (or only a cheap status read)."""
        if not self.p.enabled or self.s.dry_run:
            return None
        doc = self.ensure_rotation_key()
        if isinstance(doc.get("inflight"), dict):
            return self._recover(doc)
        if not doc.get("setup_reported"):
            doc["setup_reported"] = True
            self._save(doc)
            return Outcome("keys-setup", DONE, [
                "this App made its rotation key; only the fingerprint below leaves this Home Assistant",
                "next: an AI commits ROTATION_KEY_SHA256 to the Broker config; the owner taps Promote once"],
                {"rotation_fingerprint": doc["rotation_fp"], "broker_var": "ROTATION_KEY_SHA256"})
        now = self.clock()
        status = None
        if now - float(doc.get("status_at") or 0) >= STATUS_EVERY or doc["queue"]:
            status = self._status_read(doc)
            doc = self._doc()
        if not doc.get("enrolled"):
            return None
        due = self._due(doc, now)
        if not due:
            return None
        if not doc.get("first_approved"):
            return self._first_ask(doc, now, due)
        if doc["queue"]:
            role = doc["queue"][0]
        else:
            if not self._in_window(now) or doc.get("last_change_day") == self._day(now):
                return None
            role = due[0]
        return self.rotate(role, status)

    def _status_read(self, doc: dict) -> dict | None:
        now = self.clock()
        try:
            status = self._broker().status(doc["rotation_key"])
        except (BrokerError, net.NetError):
            doc["status_at"] = now
            self._save(doc)
            return None
        if not doc.get("enrolled"):
            doc["enrolled"] = True
            self.j.audit("keys", "KEYS_ENROLLED", rotation_fingerprint_prefix=doc["rotation_fp"][:12])
        doc["status_at"] = now
        self._note_holders(doc, status)
        self._save(doc)
        return status

    def _note_holders(self, doc: dict, status: dict) -> None:
        """An old key used after its App was switched means something else still holds it."""
        for role in ROLES:
            rec = doc["roles"].get(role) or {}
            at = rec.get("rotated_at")
            row = (status.get("roles") or {}).get(role) or {}
            if not isinstance(at, (int, float)):
                continue
            for field_name in ("previous_seen_at", "legacy_seen_at"):
                seen = _parse_ts(row.get(field_name))
                # 120 s margin: the App itself may still use the old key right up to its restart.
                if seen is not None and seen > at + 120 and seen > float(rec.get(UNKNOWN_HOLDER) or 0):
                    rec[UNKNOWN_HOLDER] = seen
                    doc["roles"][role] = rec
                    self.j.audit("keys", "KEY_OLD_STILL_USED", role=role, slot=field_name)

    def _due(self, doc: dict, now: float) -> list[str]:
        out = []
        for role in ROLES:
            rec = doc["roles"].get(role) or {}
            at = rec.get("rotated_at")
            if role in doc["queue"]:
                out.append(role)
                continue
            if now - float(rec.get("last_at") or 0) < RETRY_AFTER_FAILURE:
                continue
            if rec.get("last") == SKIPPED and now - float(rec.get("last_at") or 0) < self.p.rotate_days * 86400:
                continue                        # 0.9.14: an App without a key is looked at once per key cycle
            if not isinstance(at, (int, float)) or now - at >= self.p.rotate_days * 86400:
                out.append(role)
        return out

    def _in_window(self, now: float) -> bool:
        start, end = self.p.window
        hour = time.localtime(now).tm_hour
        return start <= hour < end if start < end else (hour >= start or hour < end)

    @staticmethod
    def _day(now: float) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(now))

    def _first_ask(self, doc: dict, now: float, due: list[str]) -> Outcome | None:
        """The owner's one question before the first automatic change (K3: "approve the first rotation once")."""
        if doc.get("first_declined"):
            return None
        hour = time.localtime(now).tm_hour
        lo, hi = self.p.ask_hours
        decided = approval.decided("KEYS", "keys-first")      # 0.9.0: decided on the Maintenance page
        if not doc["queue"] and not lo <= hour < hi and not decided:
            return None
        if now - float(doc.get("first_asked_at") or 0) < self.p.reask_hours * 3600 and not decided:
            return None
        doc["first_asked_at"] = now
        self._save(doc)
        lines = [
            "Start automatic key changes?",
            "From now on this App changes the Broker keys of the Engineering Observer, Inspector, AI Access and the "
            f"Inventory Scout by itself, one at night, every {int(self.p.rotate_days)} days.",
            "It restarts only that one App right after its key changed. If anything fails, the old key keeps "
            "working and you get one message.",
            "You will not be asked again.",
            "",
            "Request: keys-first",
        ]
        self.j.audit("keys-first", "APPROVAL_REQUESTED", job=JOB)
        decision = approval.ask(self.ha, self.s.notify_service, self.s.owner_user_id, stage="KEYS",
                                title="Maintenance: automatic key changes", message="\n".join(lines),
                                timeout=self.s.approval_timeout, require_auth=self.s.require_auth,
                                board=self.s.board, open_url=self.s.open_url,
                                should_stop=getattr(self.s, "should_stop", None))
        self.j.audit("keys-first", "APPROVAL_" + decision.outcome, channel=decision.channel)
        doc = self._doc()
        if decision.outcome == approval.APPROVE:
            doc["first_approved"] = True
            self._save(doc)
            return Outcome("keys-first", DONE, ["owner approved automatic key changes; the first one runs tonight"],
                           {"roles": due})
        if decision.outcome == approval.REJECT:
            doc["first_declined"] = True
            self._save(doc)
            return Outcome("keys-first", REFUSED, ["owner declined automatic key changes; nothing is changed and "
                                                   "this App does not ask again"], {"roles": due})
        return None                                         # no answer: asked again tomorrow

    # -- one change ------------------------------------------------------------------------------------
    def rotate(self, role: str, status: dict | None = None) -> Outcome | None:
        doc = self._doc()
        rid = f"key-{role.replace('_', '-')}-{time.strftime('%Y%m%d%H%M', time.gmtime(self.clock()))}"
        rot = doc["rotation_key"]
        broker = self._broker()
        facts: dict = {"role": role, "app": APP_NAMES[role]}
        try:
            status = broker.status(rot)
        except (BrokerError, net.NetError) as err:
            return self._record(doc, role, rid, FAILED, f"the Broker could not be read ({err}); nothing changed", facts)
        row = (status.get("roles") or {}).get(role) or {}
        if row.get("pending_prefix"):
            try:
                broker.abort(rot, role)                     # a leftover from a lost change: nothing uses it
            except (BrokerError, net.NetError):
                pass
        if role == "rotation":
            return self._rotate_self(doc, rid, broker, facts)
        slug = self.ha.key_slug(role)
        if slug is None or slug not in {a.slug for a in self.ha.installed_apps()}:
            return self._record(doc, role, rid, SKIPPED, f"{APP_NAMES[role]} is not installed; nothing changed", facts)
        option = KEY_OPTIONS[role]
        try:
            with self.ha.key_target(role, slug):
                state, options = self.ha.key_consumer_read()
        except (HAError, net.NetError, ForbiddenCall) as err:
            return self._record(doc, role, rid, FAILED, f"its settings could not be read ({err}); nothing changed",
                                facts)
        old = options.get(option)
        if not isinstance(old, str) or not old:
            return self._record(doc, role, rid, SKIPPED, f"{APP_NAMES[role]} has no Broker key set; nothing changed",
                                facts)
        facts["old_prefix"] = fingerprint(old)[:12]
        try:
            accepted = broker.probe(old, role)
        except (BrokerError, net.NetError) as err:
            return self._record(doc, role, rid, FAILED, f"the Broker check failed ({err}); nothing changed", facts)
        if not accepted:
            return self._record(doc, role, rid, FAILED,
                                "the Broker does not accept the key this App uses today; nothing changed", facts)
        new = secrets.token_urlsafe(48)
        net.register_secret(new)
        infl = {"rid": rid, "role": role, "slug": slug, "option": option, "new_key": new, "new_fp": fingerprint(new),
                "old_value": old, "was_started": state == "started", "stage": "ROTATING", "started_at": self.clock()}
        doc["inflight"] = infl
        self._save(doc)
        facts["new_prefix"] = infl["new_fp"][:12]
        self.j.audit(rid, "KEY_ROTATING", role=role, old=facts["old_prefix"], new=facts["new_prefix"])
        try:
            broker.rotate(rot, role, infl["new_fp"])
        except (BrokerError, net.NetError) as err:
            doc = self._doc()
            doc.pop("inflight", None)
            return self._record(doc, role, rid, FAILED, f"the Broker refused the new key ({err}); nothing changed",
                                facts)
        if not self._await_probe(broker, new, role):
            return self._rollback(rid, "the Broker did not accept the new key in time", facts)
        self._stage("SWITCHING")
        try:
            with self.ha.key_target(role, slug):
                _, current = self.ha.key_consumer_read()
                self.ha.key_consumer_write({**current, option: new})
                if infl["was_started"] and role not in KEY_NO_RESTART:
                    self.ha.key_consumer_restart()
                _, back = self.ha.key_consumer_read()
        except (HAError, net.NetError, ForbiddenCall) as err:
            return self._rollback(rid, f"switching {APP_NAMES[role]} failed ({err})", facts)
        if back.get(option) != new:
            return self._rollback(rid, "the App's settings did not keep the new key", facts)
        self._stage("VERIFYING")
        ok, why = self._verify(broker, rot, role, slug, new, infl["was_started"])
        if not ok:
            return self._rollback(rid, why, facts)
        try:
            broker.commit(rot, role, infl["new_fp"])
        except (BrokerError, net.NetError) as err:
            return self._rollback(rid, f"the Broker commit failed ({err})", facts)
        doc = self._doc()
        doc.pop("inflight", None)
        facts["verified"] = why
        return self._record(doc, role, rid, DONE, f"{APP_NAMES[role]} now uses its new Broker key ({why}); the old "
                            "key stays accepted for its grace time", facts, rotated=True)

    def _rotate_self(self, doc: dict, rid: str, broker: Broker, facts: dict) -> Outcome | None:
        old = doc["rotation_key"]
        new = secrets.token_urlsafe(48)
        net.register_secret(new)
        doc["inflight"] = {"rid": rid, "role": "rotation", "new_key": new, "new_fp": fingerprint(new),
                           "old_value": old, "stage": "ROTATING", "started_at": self.clock()}
        self._save(doc)
        facts.update(old_prefix=fingerprint(old)[:12], new_prefix=fingerprint(new)[:12])
        try:
            broker.rotate(old, "rotation", fingerprint(new))
            self._commit_self(broker, new)                          # proof of possession: signed with the new key
        except (BrokerError, net.NetError) as err:
            return self._recover(self._doc(), reason=f"rotation-key change failed ({err})")
        doc = self._doc()
        doc["rotation_key"], doc["rotation_fp"] = new, fingerprint(new)
        doc.pop("inflight", None)
        return self._record(doc, "rotation", rid, DONE, "this App's rotation key was changed", facts, rotated=True)

    def _commit_self(self, broker: Broker, new: str) -> None:
        """0.9.14 (live 2026-10-17, #454 6232092258: HTTP 401): another Broker instance may still hold the registry
        from before the rotate for up to 30 s (its per-isolate cache), so it does not know the new key yet. A 401 is
        retried until ``probe_wait``; any other answer, or a 401 after it, ends the change (then rolled back)."""
        deadline = self.clock() + self.p.probe_wait
        while True:
            try:
                broker.commit(new, "rotation", fingerprint(new))
                return
            except BrokerError as err:
                if err.status != 401 or self.clock() >= deadline:
                    raise
            self.sleep(self.p.poll)

    def _await_probe(self, broker: Broker, key: str, role: str) -> bool:
        deadline = self.clock() + self.p.probe_wait
        while True:
            try:
                if broker.probe(key, role):
                    return True
            except (BrokerError, net.NetError):
                pass
            if self.clock() >= deadline:
                return False
            self.sleep(self.p.poll)

    def _verify(self, broker: Broker, rot: str, role: str, slug: str, new: str, was_started: bool) -> tuple[bool, str]:
        if was_started and role not in KEY_NO_RESTART:
            deadline = self.clock() + self.p.restart_seconds
            while self.ha.app_state(slug) != "started":
                if self.clock() >= deadline:
                    return False, f"{APP_NAMES[role]} did not start again after its key change"
                self.sleep(self.p.poll)
        if was_started and role in ("observer_write", "inspector_entity"):
            deadline = self.clock() + self.p.verify_seconds
            while True:
                try:
                    row = (broker.status(rot).get("roles") or {}).get(role) or {}
                    if row.get("pending_seen_at"):
                        return True, "the App already used the new key"
                except (BrokerError, net.NetError):
                    pass
                if self.clock() >= deadline:
                    return False, f"{APP_NAMES[role]} did not use the new key within {int(self.p.verify_seconds)} s"
                self.sleep(self.p.poll)
        try:
            if broker.probe(new, role):
                return True, "the Broker accepts the new key" + ("" if was_started else "; the App was stopped and "
                                                                    "uses it at its next start")
        except (BrokerError, net.NetError):
            pass
        return False, "the Broker does not accept the new key"

    def _stage(self, stage: str) -> None:
        doc = self._doc()
        if isinstance(doc.get("inflight"), dict):
            doc["inflight"]["stage"] = stage
            self._save(doc)

    def _rollback(self, rid: str, why: str, facts: dict) -> Outcome | None:
        doc = self._doc()
        infl = doc.get("inflight") or {}
        role = infl.get("role")
        problems = []
        if infl.get("stage") in ("SWITCHING", "VERIFYING") and role in KEY_OPTIONS:
            try:
                with self.ha.key_target(role, infl["slug"]):
                    _, current = self.ha.key_consumer_read()
                    if current.get(infl["option"]) != infl["old_value"]:
                        self.ha.key_consumer_write({**current, infl["option"]: infl["old_value"]})
                        if infl.get("was_started") and role not in KEY_NO_RESTART:
                            self.ha.key_consumer_restart()
                    _, back = self.ha.key_consumer_read()
                if back.get(infl["option"]) != infl["old_value"]:
                    problems.append("its settings do not hold the old key")
            except (HAError, net.NetError, ForbiddenCall) as err:
                problems.append(f"putting the old key back failed ({err})")
        try:
            self._broker().abort(doc["rotation_key"], role)
        except (BrokerError, net.NetError) as err:
            problems.append(f"dropping the new key at the Broker failed ({err})")
        doc = self._doc()
        if problems:
            # The Broker still accepts the old key as `current`; the App may hold the new one. Leave the
            # in-flight record so the next poll tries the rollback again; tell the owner once.
            infl["attempts"] = int(infl.get("attempts") or 0) + 1
            doc["inflight"] = infl
            self._save(doc)
            if infl["attempts"] > 1:
                return None                                  # retried quietly each poll; reported once already
            return Outcome(rid, FAILED_MANUAL, [why, *problems, "the change is undone again on the next poll"], facts,
                           notify=True)
        doc.pop("inflight", None)
        return self._record(doc, role, rid, ROLLED_BACK, f"{why}; the old key was put back and still works", facts)

    def _recover(self, doc: dict, reason: str = "this App restarted during a key change") -> Outcome | None:
        infl = doc["inflight"]
        rid = str(infl.get("rid") or "key-recover")
        role = infl.get("role")
        facts = {"role": role, "app": APP_NAMES.get(role, role), "recovered": True}
        if role == "rotation":
            broker = self._broker()
            for key in (infl.get("new_key"), infl.get("old_value")):
                try:
                    st = broker.status(key)
                except (BrokerError, net.NetError):
                    continue
                current = ((st.get("roles") or {}).get("rotation") or {}).get("current_prefix")
                if key == infl.get("new_key") and current == infl.get("new_fp", "")[:12]:
                    doc["rotation_key"], doc["rotation_fp"] = key, infl["new_fp"]
                    doc.pop("inflight", None)
                    return self._record(doc, "rotation", rid, DONE, "the rotation-key change had finished", facts,
                                        rotated=True)
                if key == infl.get("old_value"):
                    try:
                        broker.abort(key, "rotation")
                    except (BrokerError, net.NetError):
                        pass
                    doc.pop("inflight", None)
                    return self._record(doc, "rotation", rid, ROLLED_BACK, f"{reason}; the old rotation key stays",
                                        facts)
            return Outcome(rid, FAILED_MANUAL, [reason, "the Broker could not be read; tried again next poll"], facts)
        return self._rollback(rid, reason, facts)

    def _record(self, doc: dict, role: str, rid: str, outcome: str, note: str, facts: dict,
                rotated: bool = False) -> Outcome:
        now = self.clock()
        rec = doc["roles"].get(role) or {}
        rec.update(last=outcome, last_at=now, note=note[:200])
        failed = outcome in (FAILED, FAILED_MANUAL, ROLLED_BACK)
        rec["failures"] = int(rec.get("failures") or 0) + 1 if failed else (0 if outcome == DONE else
                                                                             int(rec.get("failures") or 0))
        if rotated:
            rec["rotated_at"] = now
            rec.pop(UNKNOWN_HOLDER, None)
        doc["roles"][role] = rec
        if role in doc["queue"]:
            doc["queue"].remove(role)
        if outcome != SKIPPED:
            doc["last_change_day"] = self._day(now)
        self._save(doc)
        self.j.audit(rid, "KEY_" + outcome, role=role)
        # One failed night (e.g. no internet at 3 am) is retried quietly; the owner hears once, on the second.
        return Outcome(rid, outcome, [note], facts, notify=failed and rec["failures"] == 2)


def _parse_ts(value) -> float | None:
    """The Broker's ISO-8601 UTC time (``2026-10-12T08:00:00.000Z``) as epoch seconds."""
    if not isinstance(value, str):
        return None
    try:
        return float(calendar.timegm(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")))
    except ValueError:
        return None
