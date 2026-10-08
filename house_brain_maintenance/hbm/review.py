"""Deterministic update review: is one pending App update low risk, risky, or blocked?

Pure functions over projected Supervisor facts (``ha.AppDetail``) and the App's changelog.
No I/O, no clock. The verdict drives two things only: the wording of the owner's approval
request, and (when the owner has enabled ``update_mode: auto_low_risk``) whether a
bug-fix-level update may run in the night window without a tap.

Verdicts:
* ``blocked`` - do not offer: Supervisor says the new version is unavailable, it needs a newer
  Home Assistant than is running, the system is unhealthy, or the version already failed here;
* ``risky``   - offer, always ask: major version jump, breaking/migration wording in the
  changelog, a permission change, a pre-release stage, or other Apps depend on it;
* ``low``     - offer; eligible for automatic install only if the bump is ``patch``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .ha import AppDetail

BLOCKED = "blocked"
RISKY = "risky"
LOW = "low"

RISK_WORDS = (
    "breaking", "migration", "migrate", "deprecat", "removed", "no longer", "incompatible",
    "manual action", "action required", "requires home assistant", "backup before", "downgrade",
    "reconfigure", "re-pair", "repair your", "database upgrade",
)
_NUM = re.compile(r"^[0-9]+$")


@dataclass(frozen=True)
class Review:
    slug: str
    name: str
    from_version: str
    to_version: str
    bump: str                      # patch | minor | major | unknown
    verdict: str                   # low | risky | blocked
    reasons: tuple[str, ...]
    dependents: tuple[str, ...] = field(default=())

    @property
    def auto_eligible(self) -> bool:
        return self.verdict == LOW and self.bump == "patch"


def _parts(version: str, tags: bool = False) -> list[str]:
    if tags and len(version) > 1 and version[0] in "vV" and version[1].isdigit():
        version = version[1:]          # 0.7.0 (HACS/firmware only): release tags "v5.2.3", firmware "V7.2.8.5"
    core = re.split(r"[-+]", version, maxsplit=1)[0]
    return core.split(".")


def version_tuple(version: str, tags: bool = False) -> tuple[int, ...] | None:
    parts = _parts(version, tags)
    if not parts or not all(_NUM.fullmatch(p) for p in parts):
        return None
    return tuple(int(p) for p in parts)


def bump_kind(old: str, new: str, tags: bool = False) -> str:
    """``tags`` (0.7.0, HACS and firmware only): a leading v/V is part of the tag, not the version. App versions
    keep 0.6.x behaviour (a v-prefixed App version is not comparable)."""
    a, b = version_tuple(old, tags), version_tuple(new, tags)
    if a is None or b is None or len(a) < 2 or len(b) < 2:
        return "unknown"
    if b <= a:
        return "unknown"            # downgrade or re-tag: never treated as routine
    calver = a[0] >= 2000 and b[0] >= 2000
    if not calver and b[0] != a[0]:
        return "major"
    if calver and b[0] - a[0] > 1:
        return "major"
    if b[:2] != a[:2]:
        return "minor"
    return "patch"


def changelog_window(changelog: str, current: str, limit: int = 6000) -> str:
    """The changelog text newer than the installed version (top of the file down to its heading)."""
    text = changelog[:65536]
    pattern = re.compile(r"^\s{0,3}#{1,6}[^\n]*\b" + re.escape(current) + r"\b", re.MULTILINE)
    m = pattern.search(text)
    return (text[:m.start()] if m else text)[:limit]


def risk_words(window: str) -> tuple[str, ...]:
    low = window.lower()
    return tuple(w for w in RISK_WORDS if w in low)


def _newer(required: str, running: str) -> bool:
    a, b = version_tuple(required), version_tuple(running)
    if a is None or b is None:
        return False
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def dependents_of(target: AppDetail, others: list[AppDetail]) -> tuple[str, ...]:
    provided = {s.split(":", 1)[0] for s in target.services if s.endswith(":provide")}
    out = []
    for other in others:
        if other.slug == target.slug:
            continue
        needs = {s.split(":", 1)[0] for s in other.services if s.endswith((":need", ":want"))}
        if provided & needs:
            out.append(other.slug)
    return tuple(sorted(out))


def review(*, name: str, installed: AppDetail, store: AppDetail, changelog: str, core_version: str,
           unhealthy: frozenset[str], quarantined: bool, dependents: tuple[str, ...]) -> Review:
    from_v = installed.version or "?"
    to_v = store.version_latest or installed.version_latest or "?"
    bump = bump_kind(from_v, to_v)
    blocked: list[str] = []
    risky: list[str] = []
    if quarantined:
        blocked.append(f"{to_v} failed here before and was rolled back")
    if store.available is False:
        blocked.append("Supervisor says this version cannot be installed here")
    if store.homeassistant and _newer(store.homeassistant, core_version):
        blocked.append(f"needs Home Assistant {store.homeassistant}; you run {core_version}")
    if unhealthy:
        blocked.append("Home Assistant reports it is unhealthy: " + ", ".join(sorted(unhealthy))[:120])
    if bump == "major":
        risky.append("major version jump")
    if bump == "unknown":
        risky.append("version numbers are not comparable")
    words = risk_words(changelog_window(changelog, from_v))
    if words:
        risky.append("release notes mention: " + ", ".join(words[:5]))
    if not changelog.strip():
        risky.append("no release notes available")
    before, after = dict(installed.privileges), dict(store.privileges)
    changed = sorted(k for k in set(before) | set(after)
                     if k in before and k in after and before[k] != after[k])
    if changed:
        risky.append("permissions change: " + ", ".join(f"{k} {before[k]}->{after[k]}" for k in changed))
    if store.stage in ("experimental", "deprecated"):
        risky.append(f"stage is {store.stage}")
    if dependents:
        risky.append("other Apps rely on it: " + ", ".join(dependents[:5]))
    verdict = BLOCKED if blocked else RISKY if risky else LOW
    reasons = tuple(blocked or risky or [f"{bump} update, clean release notes, no permission change"])
    return Review(slug=installed.slug, name=name, from_version=from_v, to_version=to_v, bump=bump,
                  verdict=verdict, reasons=reasons, dependents=dependents)


# -- 0.7.0 HACS and device-firmware updates (owner decisions 2026-10-08) ------------------------------------
HACS_DISPLAY = ("plugin", "theme")       # dashboard cards and themes: display only, no restart


@dataclass(frozen=True)
class EntityReview:
    entity_id: str
    name: str
    kind: str                      # hacs | hacs_integration | firmware
    category: str                  # HACS category, or "firmware"
    from_version: str
    to_version: str
    bump: str
    verdict: str
    reasons: tuple[str, ...]

    @property
    def slug(self) -> str:         # the key the shared automatic-install rules use
        return self.entity_id

    @property
    def auto_eligible(self) -> bool:
        """Only a display-only HACS card or theme may ever install without a tap (owner design 2026-10-08)."""
        return self.kind == "hacs" and self.category in HACS_DISPLAY and self.verdict == LOW and self.bump == "patch"


def entity_kind(platform: str, category: str | None) -> str | None:
    """Which engine path an update entity takes; None: not this engine's (Supervisor-owned or unknown)."""
    if platform in ("", "hassio"):
        return None
    if platform == "hacs":
        if category is None:
            return None
        return "hacs_integration" if category == "integration" else "hacs"
    return "firmware"


def review_entity(*, entity_id: str, name: str, kind: str, category: str, from_version: str, to_version: str,
                  notes: str, ha_min: str | None, core_version: str, unhealthy: frozenset[str],
                  quarantined: bool, in_progress: bool) -> EntityReview:
    bump = bump_kind(from_version, to_version, tags=True)
    blocked: list[str] = []
    risky: list[str] = []
    if quarantined:
        blocked.append(f"{to_version} failed here before and was rolled back")
    if ha_min and _newer(ha_min, core_version):
        blocked.append(f"needs Home Assistant {ha_min}; you run {core_version}")
    if unhealthy:
        blocked.append("Home Assistant reports it is unhealthy: " + ", ".join(sorted(unhealthy))[:120])
    if in_progress:
        blocked.append("an install is already in progress")
    if bump == "major":
        risky.append("major version jump")
    if bump == "unknown":
        risky.append("version numbers are not comparable")
    words = risk_words(changelog_window(notes, from_version))
    if words:
        risky.append("release notes mention: " + ", ".join(words[:5]))
    if not notes.strip():
        risky.append("no release notes available")
    verdict = BLOCKED if blocked else RISKY if risky else LOW
    reasons = tuple(blocked or risky or [f"{bump} update, clean release notes"])
    return EntityReview(entity_id=entity_id, name=name, kind=kind, category=category, from_version=from_version,
                        to_version=to_version, bump=bump, verdict=verdict, reasons=reasons)
