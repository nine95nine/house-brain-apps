"""HACS and device-firmware updates (0.7.0, owner decisions 2026-10-08: "Yes, design it", "Approve design",
"Yes, but always ask" for firmware; the owner's goal: "have the maintenance app handle any and all applications").

Every Home Assistant ``update.*`` entity that the Supervisor does not own (Apps, Core and OS stay with their own
engines) is found on the update cadence, reviewed and handled one at a time:

* **HACS card or theme** (display only, no restart): with ``update_mode: auto_low_risk`` a low-risk bug-fix
  update installs by itself in the night window after the waiting period, like an App; everything else asks.
* **HACS integration** (and other HACS code): always asks. One Approve covers the Home Assistant restart an
  integration needs and, if the health check fails, the rollback restart.
* **Device firmware** (Z-Wave, Matter, routers ...): always asks, never automatic, and only after a version has
  been offered for ``firmware_wait_days`` (default 30), so other people find a bad firmware first. Firmware
  cannot be rolled back by Home Assistant: a failure is reported, never "restored".

Transaction (journal ``txn.json``, kind ``entity``), one entity at a time:
  HACS:     BACKING_UP -> BACKED_UP -> INSTALLING -> (RESTARTING) -> DONE | ROLLING_BACK -> RESTORED
  firmware: INSTALLING -> DONE | FAILED | FAILED_MANUAL
After a restart of this App ``recover`` reads the entity's real version and continues from the facts.

0.9.52 (W26-C9, Home Assistant 2026.11 folds HACS into core as the "Marketplace"; fail closed): the platform
``marketplace`` is exactly ``hacs``; an update whose platform is not known (review.FIRMWARE_PLATFORMS) is not handled
and is the visible finding ``UPDATE_PLATFORM_UNKNOWN``; HACS updates waiting while neither repository list command
answers are the visible finding ``HACS_LIST_UNAVAILABLE`` (never "nothing to update"). Both reach the owner through
the Watcher like every other problem (``findings``; morning summary, Messages list, #454).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

from . import approval, issues as I, net, reasks
from .ha import HACS_LIST_UNAVAILABLE, HACS_PLATFORMS, ForbiddenCall, HAError, HomeAssistant, UpdateEntity
from .journal import Journal
from .review import (BLOCKED, HACS_DISPLAY as HACS_DISPLAY_KINDS, LOW, UPDATE_PLATFORM_UNKNOWN, EntityReview, entity_kind,
                     platform_known, review_entity)
from .updates import (AUTO_LOW_RISK, DONE, DRY_RUN_OK, FAILED, FAILED_MANUAL, REFUSED, REJECTED, RESTORED, TIERS,
                      TRACK_RECORD_NEEDED, VERSION_OF, Outcome)
from . import evidence as EV

DOC = "entity_updates"
WATCH_DOC = "firmware_watch"      # 0.9.36: one firmware install whose device has not reported back yet
PAUSE_DOC = "firmware_pause"      # 0.9.37: the device baseline behind a firmware pause (for the self-clear)
JOBS = {"hacs": "UPDATE_HACS", "hacs_integration": "UPDATE_HACS", "firmware": "UPDATE_FIRMWARE"}
NOTES_KEY = "platform_notes"      # 0.9.52: what the last discovery could not handle (kept for the Watcher's findings)


@dataclass
class EntityPolicy:
    mode: str = "ask"                       # update_mode (shared with App updates)
    window: tuple[int, int] = (2, 5)
    wait_days: float = 3.0                  # automatic HACS card/theme installs wait this long
    firmware_wait_days: float = 30.0        # firmware is asked only after it was offered this long
    reask_hours: float = 6.0
    practice_hours: float = 24.0
    health_seconds: float = 180.0
    boot_seconds: float = 900.0             # Core back after a restart within this time
    firmware_seconds: float = 3600.0        # a firmware install may take this long
    firmware_watch_seconds: float = 24 * 3600.0   # 0.9.36: a device that is not back is watched this long
    poll: float = 5.0
    hold: frozenset[str] = frozenset()      # update entities the owner holds (option update_hold)
    enabled: bool = True                    # option entity_updates


def request_id_for(kind: str, entity_id: str, version: str) -> str:
    prefix = "fw" if kind == "firmware" else "hacs"
    rid = re.sub(r"[^a-z0-9-]+", "-", f"{prefix}-{entity_id[len('update.'):][:40]}-{version[:20]}".lower())
    return rid.strip("-")[:64]


class EntityUpdater:
    def __init__(self, ha: HomeAssistant, journal: Journal, settings, policy: EntityPolicy,
                 app_successes=lambda: 0) -> None:
        self.ha = ha
        self.j = journal
        self.s = settings
        self.p = policy
        self.app_successes = app_successes  # the App-update track record counts for automatic installs too
        self.holds = None                   # 0.7.3: the hold list (holds.Holds); its update.* items hold too
        self.ai = None                      # 0.8.9: aireview.AiReviews (no ask/automatic install before the review)
        self.queue: dict[str, dict] = {}    # 0.8.9: HACS/firmware updates offered and why they are (not) asked yet
        self.evidence = None                # 0.9.56: evidence.Evidence (tiers mode)
        self.batch = None                   # 0.9.57: batch.Batch (tier B weekly batch, tiers mode)

    # -- memory ------------------------------------------------------------------------
    def _mem(self) -> dict:
        mem = self.j.load_doc(DOC, {})
        for key in ("quarantine", "declined", "asked", "blocked_noted", "practiced", "first_seen", "timeouts"):
            mem.setdefault(key, {})
        mem.setdefault("successes", 0)
        return mem

    def _save(self, mem: dict) -> None:
        self.j.save_doc(DOC, mem)

    # -- discovery and review (read-only) -------------------------------------------------
    def candidates(self) -> list[tuple[UpdateEntity, str, object]]:
        """(entity, kind, HACS repository or None) for each update waiting that this engine handles."""
        if not self.p.enabled:
            self._note_platforms([], False)
            return []
        held = self.p.hold | (self.holds.entity_ids() if self.holds is not None else frozenset())
        ents = [e for e in self.ha.update_entities()
                if e.state == "on" and e.installed and e.latest and e.installed != e.latest
                and e.skipped != e.latest and not e.auto_update and e.entity_id not in held
                and e.platform not in ("", "hassio")]
        unknown = sorted({e.platform for e in ents if not platform_known(e.platform)})
        repos: dict = {}
        blind = False
        if any(e.platform in HACS_PLATFORMS for e in ents):
            try:
                repos = self.ha.hacs_repos()
            except HAError as err:
                if err.code != HACS_LIST_UNAVAILABLE:
                    raise
                blind = True                    # 0.9.52: every HACS item stays unhandled (category None), and visible
        self._note_platforms(unknown, blind)
        out = []
        for ent in ents:
            repo = repos.get(ent.unique_id or "") if ent.platform in HACS_PLATFORMS else None
            kind = entity_kind(ent.platform, getattr(repo, "category", None))
            if kind is not None:
                out.append((ent, kind, repo))
        return out

    def _note_platforms(self, unknown: list[str], blind: bool) -> None:
        """0.9.52: remember what this discovery could not handle; audit each new reason code once."""
        mem = self._mem()
        old = mem.get(NOTES_KEY) or {}
        notes = {"unknown": list(unknown)[:20], "hacs_list_unavailable": bool(blind)}
        if notes == {"unknown": list(old.get("unknown") or []),
                     "hacs_list_unavailable": bool(old.get("hacs_list_unavailable"))}:
            return
        for plat in notes["unknown"]:
            if plat not in (old.get("unknown") or []):
                self.j.audit("entity-platforms", UPDATE_PLATFORM_UNKNOWN, platform=plat)
        if blind and not old.get("hacs_list_unavailable"):
            self.j.audit("entity-platforms", HACS_LIST_UNAVAILABLE)
        mem[NOTES_KEY] = notes
        self._save(mem)

    def findings(self) -> list[I.Finding]:
        """0.9.52: the updates this engine saw waiting but cannot handle, as owner-visible problems (Watcher)."""
        notes = self._mem().get(NOTES_KEY) or {}
        out = []
        for plat in (notes.get("unknown") or [])[:20]:
            out.append(I.Finding(
                f"updates:platform_unknown:{plat}", "updates", I.WARNING,
                f"An update from {plat} is waiting, and this App does not know that kind of update",
                detail=(f"reason: {UPDATE_PLATFORM_UNKNOWN}", f"platform: {plat}"),
                steps=("Update it yourself in Settings > System > Updates if you want it now.",
                       "Or ask for a Maintenance release that knows this kind of update."),
                done="Nothing: I do not install a kind of update I do not know.",
                if_ignored="The update stays waiting; nothing is installed.",
                facts={"reason": UPDATE_PLATFORM_UNKNOWN, "platform": plat}))
        if notes.get("hacs_list_unavailable"):
            out.append(I.Finding(
                "updates:hacs_list_unavailable", "updates", I.WARNING,
                "HACS (Marketplace) updates are waiting, but their repository list cannot be read",
                detail=(f"reason: {HACS_LIST_UNAVAILABLE}",),
                steps=("Update them yourself in Settings > System > Updates if you want them now.",
                       "Home Assistant may have renamed the list; a Maintenance release has to learn the new name."),
                done="Nothing: without the list I cannot tell a card from an integration, so I install none of them.",
                if_ignored="Those updates stay waiting; nothing is installed.",
                facts={"reason": HACS_LIST_UNAVAILABLE}))
        return out

    def _key(self, ent: UpdateEntity) -> str:
        return f"{ent.entity_id}@{ent.latest}"

    def _due(self, key: str, mem: dict) -> bool:
        if mem["declined"].get(key):
            return False
        if self.s.dry_run:
            practiced = mem["practiced"].get(key)
            if practiced and time.time() - float(practiced) < self.p.practice_hours * 3600:
                return False
        asked = mem["asked"].get(key)
        if not asked:
            return True
        n = int(mem["timeouts"].get(key) or 0)
        if n and approval.owner_decided_since(float(asked)):          # 0.9.39: the owner is back
            mem["timeouts"].pop(key, None)
            self._save(mem)
            n = 0
        if not reasks.phone_allowed(n):
            return False
        return time.time() - float(asked) >= reasks.wait_seconds(self.p.reask_hours, n)

    def review(self, ent: UpdateEntity, kind: str, repo, mem: dict) -> EntityReview:
        unhealthy, _ = self.ha.health_flags()
        return review_entity(entity_id=ent.entity_id, name=ent.title or ent.entity_id, kind=kind,
                             category=getattr(repo, "category", None) or "firmware",
                             from_version=str(ent.installed), to_version=str(ent.latest),
                             notes=self.ha.release_notes(ent.entity_id), ha_min=getattr(repo, "homeassistant", None),
                             core_version=self.ha.core_version(), unhealthy=unhealthy,
                             quarantined=mem["quarantine"].get(ent.entity_id) == ent.latest,
                             in_progress=ent.in_progress)

    def _waited(self, key: str, mem: dict) -> float:
        first = mem["first_seen"].get(key)
        return (time.time() - float(first)) / 86400 if first else 0.0

    def _in_window(self) -> bool:
        hour = time.localtime().tm_hour
        start, end = self.p.window
        return start <= hour < end if start <= end else (hour >= start or hour < end)

    def _track_record(self, mem: dict) -> int:
        return int(mem["successes"]) + int(self.app_successes() or 0)

    def tier(self, rev: EntityReview) -> str:
        return EV.tier_of(rev.kind, bump=rev.bump, verdict=rev.verdict, slug=rev.entity_id, name=rev.name,
                          version=rev.to_version, category=rev.category)

    def card(self, rev: EntityReview, key: str, mem: dict) -> dict | None:
        if self.evidence is None:
            return None
        domain = rev.entity_id.split(".", 1)[-1].rsplit("_update", 1)[0] if rev.kind == "hacs_integration" else ""
        return self.evidence.for_item(item=f"entity:{key}", name=rev.name, kind=rev.kind, bump=rev.bump,
                                      verdict=rev.verdict, from_version=rev.from_version, to_version=rev.to_version,
                                      slug=rev.entity_id, first_seen=mem["first_seen"].get(key), min_days=self.p.wait_days,
                                      ledger_prefix=request_id_for(rev.kind, rev.entity_id, "") + "-",
                                      domains={domain} if domain else None, category=rev.category)

    def auto_allowed(self, rev: EntityReview, key: str, mem: dict) -> tuple[bool, str]:
        if self.p.mode == TIERS:                                  # 0.9.56: tier A (card or theme patch) on a clean card
            tier = self.tier(rev)
            if tier != EV.TIER_A or rev.kind != "hacs" or rev.category not in HACS_DISPLAY_KINDS:
                return False, f"tier {tier}: asks you" if tier == EV.TIER_B else f"tier {tier}: your tap only"
            if self._track_record(mem) < TRACK_RECORD_NEEDED:
                return False, f"needs {TRACK_RECORD_NEEDED} approved updates first"
            c = self.card(rev, key, mem)
            if c is None:
                return False, "no evidence card (fail closed)"
            if c["verdict"] != EV.CLEAN:
                return False, f"evidence card {c['verdict']}: " + "; ".join(c["reasons"])[:200]
            if not self._in_window():
                return False, "outside the night window"
            return True, "tier A: patch update of a card or theme on a clean evidence card, in the night window"
        if self.p.mode != AUTO_LOW_RISK:
            return False, "update_mode is ask"
        if not rev.auto_eligible:
            return False, f"not a low-risk bug-fix update of a card or theme ({rev.category}, {rev.verdict}, {rev.bump})"
        if self._track_record(mem) < TRACK_RECORD_NEEDED:
            return False, f"needs {TRACK_RECORD_NEEDED} approved updates first"
        waited = self._waited(key, mem)
        if waited < self.p.wait_days:
            return False, f"waiting {self.p.wait_days:g} days after release ({waited:.1f} so far)"
        if not self._in_window():
            return False, "outside the night window"
        return True, "low-risk bug-fix update of a card or theme in the night window"

    def waits_for_auto(self, rev: EntityReview, mem: dict, key: str = "") -> bool:
        start, end = self.p.window
        if self.p.mode == TIERS:                                  # 0.9.56
            if (self.tier(rev) != EV.TIER_A or rev.kind != "hacs" or rev.category not in HACS_DISPLAY_KINDS
                    or start == end or self._track_record(mem) < TRACK_RECORD_NEEDED):
                return False
            c = self.card(rev, key, mem)
            return c is not None and c["verdict"] in (EV.CLEAN, EV.WAIT)
        return (self.p.mode == AUTO_LOW_RISK and rev.auto_eligible and start != end
                and self._track_record(mem) >= TRACK_RECORD_NEEDED)

    def _note_first_seen(self, found: list, mem: dict) -> None:
        now = time.time()
        offered = {self._key(ent) for ent, _, _ in found}
        before = dict(mem["first_seen"])
        mem["first_seen"] = {k: v for k, v in before.items() if k in offered}
        for key in offered:
            mem["first_seen"].setdefault(key, now)
        if mem["first_seen"] != before:
            self._save(mem)

    @staticmethod
    def _facts(rev: EntityReview) -> dict:
        return {"entity_id": rev.entity_id, "kind": rev.kind, "category": rev.category, "from": rev.from_version,
                "to": rev.to_version, "bump": rev.bump, "verdict": rev.verdict, "reasons": list(rev.reasons)}

    # -- one cycle -----------------------------------------------------------------------
    def cycle(self) -> Outcome | None:
        """At most one HACS or firmware update per call. None when there is nothing to do now."""
        mem = self._mem()
        found = self.candidates()
        self._note_first_seen(found, mem)
        old = self.queue
        self.queue = {self._key(e): {"kind": k, "item": f"entity:{self._key(e)}", "name": e.title or e.entity_id,
                                     "from": e.installed, "to": e.latest,
                                     "state": (old.get(self._key(e)) or {}).get("state", "")} for e, k, _ in found}

        def note(key: str, state: str) -> None:
            if key in self.queue:
                self.queue[key]["state"] = state
        def decided_of(ent, kind) -> bool:              # 0.9.0: the owner decided it on the Maintenance page
            return approval.decided("UPDATE", request_id_for(kind, ent.entity_id, str(ent.latest)))
        found = sorted(found, key=lambda t: not decided_of(t[0], t[1]))
        for ent, kind, repo in found:
            key = self._key(ent)
            decided = decided_of(ent, kind)
            if not decided and not self._due(key, mem):
                asked = mem["asked"].get(key)
                n = int(mem["timeouts"].get(key) or 0)
                note(key, "you rejected this version (final)" if mem["declined"].get(key) else
                     reasks.PAGE_ONLY_NOTE if not reasks.phone_allowed(n) else
                     reasks.next_ask_note(float(asked or 0), self.p.reask_hours, n))
                continue
            if mem["declined"].get(key):
                continue
            if kind == "firmware" and not decided and self._waited(key, mem) < self.p.firmware_wait_days:
                first = float(mem["first_seen"].get(key) or time.time())
                note(key, "firmware: asked from " + time.strftime(
                    "%Y-%m-%d", time.gmtime(first + self.p.firmware_wait_days * 86400))
                     + f" ({self.p.firmware_wait_days:g} days after it was first offered)")
                continue                      # firmware is asked only after it has been out for a while
            rev = self.review(ent, kind, repo, mem)
            rid = request_id_for(kind, ent.entity_id, rev.to_version)
            facts = self._facts(rev)
            if key in self.queue:
                self.queue[key].update(verdict=rev.verdict, reasons=list(rev.reasons[:4]))
            if rev.verdict == BLOCKED:
                note(key, "held by this App's review: " + "; ".join(rev.reasons)[:160])
                if mem["blocked_noted"].get(key) == "|".join(rev.reasons):
                    continue
                mem["blocked_noted"][key] = "|".join(rev.reasons)
                self._save(mem)
                return Outcome(rid, REFUSED, [f"update held: {r}" for r in rev.reasons], facts)
            gate = self.ai.gate(f"entity:{key}", mem["first_seen"].get(key)) if self.ai is not None else None
            tiers = self.p.mode == TIERS
            if gate is not None and not gate.proceed and not decided and not tiers:
                note(key, "waiting for the AI review (then asked, or installed at night if low risk; asked anyway "
                     f"from {self.ai.ask_anyway_at(mem['first_seen'].get(key))})")
                continue                              # 0.9.56 tiers: a missing AI review never delays or routes
            if tiers and not decided and EV.prerelease(rev.to_version):
                note(key, "pre-release: waits on the Maintenance page (Needs your decision); never asked on the "
                     "phone and never installed by itself")
                continue
            auto, why = self.auto_allowed(rev, key, mem)
            if auto and gate is not None and not gate.auto_ok and not tiers:
                auto, why = False, gate.line
            if tiers and not decided and self.batch is not None and self.tier(rev) == EV.TIER_B:
                item = f"entity:{key}"                 # 0.9.57: HACS integrations and other tier B entity updates
                if self.batch.due(item):
                    c = self.card(rev, key, mem)
                    if c is not None and c["verdict"] == EV.CLEAN:
                        auto, why = True, "tier B: weekly batch on a clean evidence card"
                    else:
                        note(key, "weekly batch: the evidence card is not clean; waits for the next batch: "
                             + "; ".join((c or {}).get("reasons") or ["no evidence card"])[:160])
                        continue
                else:
                    self.batch.enlist(item, kind=rev.kind, name=rev.name, from_version=rev.from_version,
                                      to_version=rev.to_version, engine="entity")
                    note(key, self.batch.note_for(item))
                    continue
            facts["automatic"] = auto
            facts["automatic_reason"] = why
            if tiers:
                facts["tier"] = self.tier(rev)
            if gate is not None and gate.state != "off":
                facts["ai_review"] = gate.verdict or gate.state
            if self.s.dry_run:
                mem["practiced"][key] = time.time()
                self._save(mem)
                return Outcome(rid, DRY_RUN_OK, ["dry run: reviewed, nothing asked or changed"], facts)
            if decided:
                auto, why = False, "decided by you on the Maintenance page"
                facts["automatic"], facts["automatic_reason"] = auto, why
            if not auto and not decided and (gate is None or gate.auto_ok or tiers) and self.waits_for_auto(rev, mem, key):
                note(key, "low risk: installs by itself in the night window, with a configuration backup first")
                continue
            if not auto and not decided:
                ok, why = reasks.budget(self.s)                  # 0.9.39: the daily ask limit
                if not ok:
                    note(key, why)
                    continue
            note(key, "installing by itself now" if auto else "asking on your phone now")
            self._ai_line = gate.line if gate is not None else ""
            return self._ask_then_apply(ent, rev, rid, key, facts, automatic=auto)
        return None

    def _ask_then_apply(self, ent: UpdateEntity, rev: EntityReview, rid: str, key: str, facts: dict, *,
                        automatic: bool) -> Outcome:
        if not automatic:
            mem = self._mem()
            mem["asked"][key] = time.time()
            self._save(mem)
            decision = self._ask(rid, rev)
            mem = self._mem()
            if decision == approval.TIMEOUT:                             # 0.9.39: counted, for the back-off
                mem["timeouts"][key] = int(mem["timeouts"].get(key) or 0) + 1
            else:
                mem["timeouts"].pop(key, None)
            if decision == approval.REJECT:
                mem["declined"][key] = True
            self._save(mem)
            if decision != approval.APPROVE:
                return Outcome(rid, REJECTED, [f"owner decision: {decision}"], facts)
        if automatic and self.p.mode == TIERS:                  # 0.9.56: the card again, right before the install
            c = self.card(rev, key, self._mem())
            if c is None or c["verdict"] != EV.CLEAN:
                if c is not None:
                    self.evidence.post_once(c, VERSION_OF(), "held before the install")
                return Outcome(rid, REFUSED, ["re-check before the install: " + "; ".join((c or {}).get("reasons") or ["no evidence card"])[:200]], facts)
            self.evidence.post_once(c, VERSION_OF(), "installing by itself")
            facts["evidence"] = c["checks"]
        result = self.apply(rid, ent, rev, automatic=automatic)
        result.facts = {**facts, **result.facts}
        if automatic and self.p.mode == TIERS and self.evidence is not None:
            self.evidence.note_auto(rev.name, rev.from_version, rev.to_version, result.result_outcome)
        if self.p.mode == TIERS and self.batch is not None and result.result_outcome in (DONE, RESTORED, FAILED, FAILED_MANUAL, REJECTED):
            self.batch.finished(f"entity:{key}", result.result_outcome) if result.result_outcome != REJECTED else self.batch.skip(f"entity:{key}")
        return result

    def _lines(self, rev: EntityReview) -> list[str]:
        label = {LOW: "LOW RISK", "risky": "RISKY - read the reasons"}.get(rev.verdict, rev.verdict)
        if rev.kind == "firmware":
            plan = ["Plan: install the firmware on this device -> watch the device until it is back. The device can "
                    "be offline for several minutes; anything it controls may stop or switch off until it is back.",
                    "Firmware cannot be rolled back automatically: a failure is reported to you."]
        elif rev.kind == "hacs_integration":
            plan = ["Plan: back up the Home Assistant configuration (no database) -> install -> restart Home "
                    "Assistant -> check the integration loads and its devices are back -> if not, reinstall "
                    f"{rev.from_version} and restart again.",
                    "This Approve covers the restart (and the rollback restart if one is needed)."]
        else:
            plan = ["Plan: back up the Home Assistant configuration (no database) -> install -> check -> reinstall "
                    f"{rev.from_version} automatically if it fails. No restart. Refresh the app afterwards."]
        what = "firmware" if rev.kind == "firmware" else f"HACS {rev.category}"
        return [f"Update {rev.name} {rev.from_version} -> {rev.to_version} ({what}, {rev.bump})?",
                f"Review: {label}", *[f"- {r}" for r in rev.reasons[:4]], *plan]

    def _ask(self, rid: str, rev: EntityReview) -> str:
        self.j.audit(rid, "APPROVAL_REQUESTED", job=JOBS[rev.kind])
        decision = approval.ask(self.ha, self.s.notify_service, self.s.owner_user_id, stage="UPDATE",
                                title=f"Maintenance: update {rev.name[:40]}?",
                                message="\n".join(self._lines(rev) + ([self._ai_line] if getattr(self, "_ai_line", "")
                                                                        else [])) + f"\n\nRequest: {rid}",
                                timeout=self.s.approval_timeout, require_auth=self.s.require_auth,
                                board=self.s.board, open_url=self.s.open_url,
                                should_stop=getattr(self.s, "should_stop", None))
        self.j.audit(rid, "APPROVAL_" + decision.outcome, channel=decision.channel, ignored=decision.ignored_events)
        return decision.outcome

    # -- the transaction -----------------------------------------------------------------
    def _baseline(self, kind: str, ent: UpdateEntity, domain: str | None) -> dict:
        if kind == "firmware":
            return self.ha.entity_health(device_id=ent.device_id) if ent.device_id else {}
        if kind == "hacs_integration" and domain:
            return self.ha.entity_health(domain=domain)
        return {}

    def apply(self, rid: str, ent: UpdateEntity, rev: EntityReview, *, automatic: bool) -> Outcome:
        kind = rev.kind
        if not self.ha.core_alive():
            return Outcome(rid, REFUSED, ["Home Assistant Core is not answering; nothing changed"])
        domain = None
        if kind == "hacs_integration":
            try:
                repo = self.ha.hacs_repos().get(ent.unique_id or "")
            except HAError as err:
                if err.code != HACS_LIST_UNAVAILABLE:
                    raise
                return Outcome(rid, REFUSED, ["the HACS repository list cannot be read now "
                                              f"({HACS_LIST_UNAVAILABLE}); nothing changed"])
            domain = getattr(repo, "domain", None)
        base_unhealthy, _ = self.ha.health_flags()
        config_failed = ""
        txn = {"kind": "entity", "entity_kind": kind, "request_id": rid, "entity_id": ent.entity_id,
               "platform": ent.platform, "device_id": ent.device_id, "unique_id": ent.unique_id, "domain": domain,
               "from": rev.from_version, "to": rev.to_version, "automatic": automatic, "backup": None,
               "baseline_unhealthy": sorted(base_unhealthy), "baseline": self._baseline(kind, ent, domain),
               "stage": "BACKING_UP" if kind != "firmware" else "INSTALLING", "started_at": time.time()}
        self.j.save_txn(txn)
        try:
            with self.ha.entity_target(ent.entity_id, kind, rev.to_version):
                if kind != "firmware":
                    self.j.audit(rid, "ENTITY_BACKING_UP", entity=ent.entity_id, automatic=automatic)
                    bslug = self.ha.backup_ha_config(ent.entity_id, rev.to_version)
                    if not self.ha.backup_has_ha(bslug):
                        self.j.clear_txn()
                        return Outcome(rid, FAILED, ["the configuration backup could not be verified; nothing was "
                                                     "updated"], {"backup": bslug})
                    txn.update(stage="BACKED_UP", backup=bslug)
                    self.j.save_txn(txn)
                now = self._read(txn)
                if now is None or now.installed != rev.from_version or now.latest != rev.to_version:
                    self.j.clear_txn()
                    return Outcome(rid, REFUSED, ["the installed or offered version changed since the review; "
                                                  "nothing was updated"], {"backup": txn.get("backup")})
                if kind == "hacs_integration":
                    # 0.8.4: the configuration check before the install is the baseline for the one after it
                    txn["config_ok_before"] = self.ha.core_config_check()[0]
                txn.update(stage="INSTALLING", started_at=time.time())
                self.j.save_txn(txn)
                self.j.audit(rid, "ENTITY_INSTALLING", entity=ent.entity_id, to=rev.to_version)
                try:
                    self.ha.install_update(ent.entity_id, None if kind == "firmware" else rev.to_version,
                                           timeout=self.p.firmware_seconds if kind == "firmware" else 600.0)
                except (HAError, net.NetError) as err:
                    self.j.audit(rid, "INSTALL_CALL_ERROR", error=net.redact(str(err))[:200])
                if kind == "hacs_integration":
                    if not self._wait_installed(txn, rev.to_version, self.p.health_seconds):
                        return self._not_installed(txn)
                    if txn.get("config_ok_before"):
                        ok, why = self.ha.core_config_check()
                        if not ok:              # 0.8.4: never restart Home Assistant into a failing configuration
                            config_failed = f"{why} after installing {rev.to_version}; Home Assistant was not restarted"
                    if not config_failed:
                        txn["stage"] = "RESTARTING"
                        self.j.save_txn(txn)
                        self.j.audit(rid, "ENTITY_RESTARTING", entity=ent.entity_id)
                        try:
                            self.ha.restart_core()
                        except (HAError, net.NetError) as err:
                            self.j.audit(rid, "RESTART_CALL_ERROR", error=net.redact(str(err))[:200])
        except ForbiddenCall as err:
            self.j.clear_txn()
            return Outcome(rid, REFUSED, [f"not allowed: {err}"])
        except (HAError, net.NetError) as err:
            if txn.get("stage") in ("BACKING_UP", "BACKED_UP"):
                self.j.clear_txn()
                return Outcome(rid, FAILED, [f"stopped before the update ({net.redact(str(err))[:200]}); "
                                             "nothing was updated"])
            return Outcome(rid, FAILED_MANUAL, [f"interrupted ({net.redact(str(err))[:200]}); it is re-checked "
                                                "on the next poll"])
        if config_failed:
            mem = self._mem()
            mem["quarantine"][txn["entity_id"]] = txn["to"]
            self._save(mem)
            txn["no_restart"] = True                    # Core never loaded the new code: put the old files back only
            return self._rollback(txn, config_failed)
        return self._verify(txn)

    def _read(self, txn: dict) -> UpdateEntity | None:
        try:
            return self.ha.update_entity(txn["entity_id"], txn.get("platform") or "", txn.get("device_id"),
                                         txn.get("unique_id"))
        except (HAError, net.NetError):
            return None

    def _wait_installed(self, txn: dict, version: str, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while True:
            now = self._read(txn)
            if now is not None and now.installed == version and not now.in_progress:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(self.p.poll)

    def _not_installed(self, txn: dict) -> Outcome:
        now = self._read(txn)
        self.j.clear_txn()
        if txn["entity_kind"] == "firmware" and not self._device_ok(txn)[0]:
            return self._start_watch(txn, f"the firmware update to {txn['to']} did not finish (the device reports "
                                          f"{getattr(now, 'installed', None)}) and the device is not back")
        return Outcome(txn["request_id"], FAILED, [f"the update did not happen (still on "
                                                   f"{getattr(now, 'installed', None)}); nothing changed"],
                       {"backup": txn.get("backup")})

    # -- health ---------------------------------------------------------------------------
    def _core_ok(self, baseline_unhealthy: list) -> tuple[bool, str]:
        try:
            if not self.ha.core_alive():
                return False, "Home Assistant Core is not answering"
            state = self.ha.core_state()
            if state["safe_mode"] or state["recovery_mode"]:
                return False, "Core started in safe or recovery mode"
            if state["state"] != "RUNNING":
                return False, f"Core state is {state['state']}"
            unhealthy, _ = self.ha.health_flags()
        except (HAError, net.NetError) as err:
            return False, f"health read failed ({net.redact(str(err))[:80]})"
        new = sorted(set(unhealthy) - set(baseline_unhealthy or []))
        if new:
            return False, "Home Assistant became unhealthy: " + ", ".join(new)[:120]
        return True, "healthy"

    def _integration_ok(self, txn: dict) -> tuple[bool, str]:
        base = txn.get("baseline") or {}
        if not txn.get("domain"):
            return True, "healthy"
        try:
            now = self.ha.entity_health(domain=txn["domain"])
        except (HAError, net.NetError) as err:
            return False, f"health read failed ({net.redact(str(err))[:80]})"
        for entry, state in (base.get("entries") or {}).items():
            if state == "loaded" and now["entries"].get(entry) != "loaded":
                return False, f"the {txn['domain']} integration did not load ({now['entries'].get(entry)})"
        if now["unavailable"] > int(base.get("unavailable") or 0):
            return False, (f"{now['unavailable']} of its {now['total']} entities are unavailable "
                           f"(before: {base.get('unavailable') or 0})")
        return True, "healthy"

    def _device_ok(self, txn: dict) -> tuple[bool, str]:
        base = txn.get("baseline") or {}
        if not txn.get("device_id"):
            return True, "no device to check"
        try:
            now = self.ha.entity_health(device_id=txn["device_id"])
        except (HAError, net.NetError) as err:
            return False, f"health read failed ({net.redact(str(err))[:80]})"
        if now["unavailable"] > int(base.get("unavailable") or 0):
            return False, (f"{now['unavailable']} of the device's {now['total']} entities are unavailable "
                           f"(before: {base.get('unavailable') or 0})")
        return True, "healthy"

    def _watch(self, txn: dict, version: str) -> tuple[bool, str]:
        """After the install (and restart): Core healthy, the entity on ``version``, the integration/device back.
        Waits up to ``boot_seconds`` for Core and the version, then watches ``health_seconds``."""
        kind = txn["entity_kind"]
        deadline = time.monotonic() + self.p.boot_seconds
        why = "not checked"
        while True:
            ok, why = self._core_ok(txn.get("baseline_unhealthy") or [])
            now = self._read(txn) if ok else None
            if ok and now is not None and now.installed == version and not now.in_progress:
                break
            if ok:
                why = f"version is {getattr(now, 'installed', None)}, expected {version}"
            if time.monotonic() >= deadline:
                return False, why
            time.sleep(self.p.poll)
        settle = time.monotonic() + self.p.health_seconds
        while True:
            ok, why = self._core_ok(txn.get("baseline_unhealthy") or [])
            if ok and kind == "hacs_integration":
                ok, why = self._integration_ok(txn)
            if ok and kind == "firmware":
                ok, why = self._device_ok(txn)
            if ok and time.monotonic() >= settle:
                return True, "healthy"
            if not ok and time.monotonic() >= settle:
                return False, why
            time.sleep(self.p.poll)

    def _verify(self, txn: dict) -> Outcome:
        rid, kind = txn["request_id"], txn["entity_kind"]
        if kind == "firmware":
            # a firmware install can take long: wait for it to finish before the health watch
            if not self._wait_installed(txn, txn["to"], self.p.firmware_seconds):
                return self._not_installed(txn)
        elif kind == "hacs":
            if not self._wait_installed(txn, txn["to"], self.p.health_seconds):
                return self._not_installed(txn)
        ok, why = self._watch(txn, txn["to"])
        if ok:
            self.j.clear_txn()
            mem = self._mem()
            mem["successes"] = int(mem["successes"]) + 1
            self._save(mem)
            self.j.audit(rid, "ENTITY_HEALTHY", entity=txn["entity_id"], to=txn["to"])
            return Outcome(rid, DONE, [f"updated {txn['entity_id']} {txn['from']} -> {txn['to']}; healthy after the "
                                       "watch"], {"backup": txn.get("backup"), "automatic": txn.get("automatic")})
        mem = self._mem()
        mem["quarantine"][txn["entity_id"]] = txn["to"]
        self._save(mem)
        if kind == "firmware":
            del mem["quarantine"][txn["entity_id"]]            # 0.9.36: judged after the watch, not now
            self._save(mem)
            return self._start_watch(txn, f"firmware {txn['to']} installed, but the device is not healthy ({why})")
        return self._rollback(txn, why)

    # -- 0.9.36 (owner 2026-10-26, Tesla car software 2026.38.5.11 at 16:08Z: the car went offline for its install,
    # the App waited an hour, called it "not back" and paused itself, so nothing else ran): a device that has not
    # reported back after a firmware install is watched for ``firmware_watch_seconds`` instead of pausing the App.
    def _start_watch(self, txn: dict, why: str) -> Outcome:
        rid = txn["request_id"]
        self.j.clear_txn()
        hours = self.p.firmware_watch_seconds / 3600.0
        doc = {k: txn.get(k) for k in ("request_id", "entity_id", "platform", "device_id", "unique_id", "from", "to",
                                       "baseline", "backup", "automatic")}
        doc.update(why=why[:300], started=time.time(), until=time.time() + self.p.firmware_watch_seconds)
        self.j.save_doc(WATCH_DOC, doc)
        self.j.audit(rid, "FIRMWARE_WATCH_STARTED", entity=txn["entity_id"], to=txn["to"], hours=hours)
        return Outcome(rid, FAILED, [why, f"this App keeps checking the device for {hours:g} h and reports again when "
                                          "it is back; nothing is paused meanwhile"],
                       {"backup": txn.get("backup"), "watch_hours": hours})

    def watch_step(self) -> Outcome | None:
        """0.9.36: one look at the watched device. DONE when it is back on the new version, FAILED when it is back on
        another one, FAILED_MANUAL (the pause, as before) when the watch runs out; None while it is still away."""
        doc = self.j.load_doc(WATCH_DOC, {})
        if not isinstance(doc, dict) or not doc.get("request_id"):
            return None
        rid, to = str(doc["request_id"]), str(doc.get("to"))
        facts = {"backup": doc.get("backup"), "entity_id": doc.get("entity_id"), "to": to, "from": doc.get("from"),
                 "kind": "firmware"}
        now = self._read(doc)
        ok, why = self._device_ok(doc)
        minutes = int((time.time() - float(doc.get("started") or time.time())) // 60)
        if ok and now is not None and not now.in_progress and now.installed == to:
            self.j.save_doc(WATCH_DOC, {})
            mem = self._mem()
            mem["successes"] = int(mem["successes"]) + 1
            self._save(mem)
            self.j.audit(rid, "ENTITY_HEALTHY", entity=doc.get("entity_id"), to=to, after_minutes=minutes)
            return Outcome(rid, DONE, [f"the device is back on {to} after {minutes} min; healthy"], facts)
        if ok and now is not None and not now.in_progress:
            self.j.save_doc(WATCH_DOC, {})
            mem = self._mem()
            mem["quarantine"][str(doc.get("entity_id"))] = to
            self._save(mem)
            return Outcome(rid, FAILED, [f"the device is back after {minutes} min but reports {now.installed}, not "
                                         f"{to}; the update did not take", f"{to} will not be offered again"], facts)
        if time.time() >= float(doc.get("until") or 0):
            self.j.save_doc(WATCH_DOC, {})
            # 0.9.37: the pause remembers the device's baseline, so "back" means "no worse than before the install"
            self.j.save_doc(PAUSE_DOC, {"request_id": rid, "entity_id": doc.get("entity_id"),
                                        "baseline": doc.get("baseline")})
            mem = self._mem()
            mem["quarantine"][str(doc.get("entity_id"))] = to
            self._save(mem)
            return Outcome(rid, FAILED_MANUAL, [f"the device has not come back {minutes // 60} h after the firmware "
                                                f"install ({doc.get('why')}; now: {why})", "check the device; "
                                                "automation is paused", f"{to} will not be offered again"], facts)
        return None

    def device_back(self, rid: str) -> tuple[bool, str] | None:
        """0.9.36: for a pause left by a firmware install (``fw-…``): is that device healthy now? None when the
        request id names no update entity this App can see. 0.9.37: the entity is found by its id even when the
        offered version moved on, and "healthy" is measured against the baseline the pause kept (no more entities
        unavailable than before the install); without one, the update entity must report and the device must not be
        entirely unavailable (a car keeps some entities unavailable for good)."""
        try:
            ents = self.ha.update_entities()
        except (HAError, net.NetError):
            return None
        found = None
        for e in ents:
            slug = request_id_for("firmware", e.entity_id, "")
            if any(request_id_for("firmware", e.entity_id, v or "") == rid for v in (e.installed, e.latest)) \
                    or (rid.startswith(slug + "-") and (found is None or len(slug) > len(found[0]))):
                found = (slug, e)
        if found is None:
            return None
        e = found[1]
        if e.state in ("unavailable", "unknown"):
            return False, f"{e.entity_id} is {e.state}"
        if not e.device_id:
            return True, f"{e.entity_id} reports {e.installed} (no device to check)"
        try:
            health = self.ha.entity_health(device_id=e.device_id)
        except (HAError, net.NetError) as err:
            return False, f"health read failed ({net.redact(str(err))[:80]})"
        pause = self.j.load_doc(PAUSE_DOC, {})
        base = (pause.get("baseline") or {}) if isinstance(pause, dict) and pause.get("request_id") == rid else None
        if base is not None:
            before = int(base.get("unavailable") or 0)
            if health["unavailable"] > before:
                return False, (f"{health['unavailable']} of {health['total']} entities unavailable "
                               f"(before the install: {before})")
            return True, (f"{e.entity_id} reports {e.installed}; {health['unavailable']} of {health['total']} "
                          f"entities unavailable, as before the install ({before})")
        if health["total"] and health["unavailable"] >= health["total"]:
            return False, f"all {health['total']} entities of the device still unavailable"
        if health["unavailable"]:
            return True, (f"{e.entity_id} reports {e.installed}; {health['unavailable']} of {health['total']} "
                          "entities unavailable (no baseline kept for this pause; the update entity reports)")
        return True, f"{e.entity_id} reports {e.installed}; all {health['total']} entities available"

    def _rollback(self, txn: dict, why: str) -> Outcome:
        rid = txn["request_id"]
        txn.update(stage="ROLLING_BACK", failure=why)
        self.j.save_txn(txn)
        self.j.audit(rid, "ENTITY_ROLLING_BACK", entity=txn["entity_id"], reason=why)
        try:
            with self.ha.entity_target(txn["entity_id"], txn["entity_kind"], txn["to"], txn["from"]):
                try:
                    self.ha.install_update(txn["entity_id"], txn["from"])
                except (HAError, net.NetError) as err:
                    self.j.audit(rid, "ROLLBACK_CALL_ERROR", error=net.redact(str(err))[:200])
                if txn["entity_kind"] == "hacs_integration":
                    self._wait_installed(txn, txn["from"], self.p.health_seconds)
                    try:
                        if not txn.get("no_restart"):
                            self.ha.restart_core()
                    except (HAError, net.NetError) as err:
                        self.j.audit(rid, "RESTART_CALL_ERROR", error=net.redact(str(err))[:200])
        except ForbiddenCall as err:
            self.j.audit(rid, "ROLLBACK_REFUSED", error=str(err)[:200])
        return self._verify_rollback(txn)

    def _verify_rollback(self, txn: dict) -> Outcome:
        rid = txn["request_id"]
        ok, why = self._watch(txn, txn["from"])
        facts = {"backup": txn.get("backup"), "failure": txn.get("failure"), "quarantined": txn["to"]}
        self.j.clear_txn()
        if ok:
            return Outcome(rid, RESTORED, [f"update to {txn['to']} failed ({txn.get('failure')}); reinstalled "
                                           f"{txn['from']}", f"{txn['to']} will not be offered again"], facts)
        return Outcome(rid, FAILED_MANUAL, [f"update failed ({txn.get('failure')}) and reinstalling {txn['from']} did "
                                            f"not bring it back ({why})", f"the configuration backup "
                                            f"{txn.get('backup')} holds the files from before; automation is paused"],
                       facts)

    # -- recovery after a restart of this App ---------------------------------------------------
    def recover(self, txn: dict) -> Outcome:
        stage = txn.get("stage")
        rid = str(txn.get("request_id", "unknown"))
        try:
            if stage in ("BACKING_UP", "BACKED_UP"):
                self.j.clear_txn()
                return Outcome(rid, FAILED, ["this App restarted before the update; nothing was updated"])
            if stage == "INSTALLING" and txn.get("entity_kind") == "hacs_integration":
                # Only the version tells whether the install happened. Not installed: nothing changed. Installed:
                # finish what the owner approved (the restart), then the same health check and rollback.
                now = self._read(txn)
                if now is None or now.installed != txn["to"]:
                    self.j.clear_txn()
                    return Outcome(rid, FAILED, ["this App restarted before the install finished; nothing changed"],
                                   {"backup": txn.get("backup")})
                txn["stage"] = "RESTARTING"
                self.j.save_txn(txn)
                try:
                    with self.ha.entity_target(txn["entity_id"], "hacs_integration", txn["to"]):
                        self.ha.restart_core()
                except (HAError, net.NetError, ForbiddenCall) as err:
                    self.j.audit(rid, "RESTART_CALL_ERROR", error=net.redact(str(err))[:200])
                return self._verify(txn)
            if stage in ("INSTALLING", "RESTARTING"):
                return self._verify(txn)
            if stage == "ROLLING_BACK":
                return self._verify_rollback(txn)
        except (HAError, net.NetError) as err:
            return Outcome(rid, FAILED_MANUAL, [f"recovery read-back failed: {net.redact(str(err))[:200]}"])
        self.j.clear_txn()
        return Outcome(rid, FAILED_MANUAL, ["unknown journalled HACS/firmware stage"])
