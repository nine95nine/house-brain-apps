"""0.9.57 Free public feeds for the evidence card (owner decision 2026-11-08 "Tiers A + B", part 2).

* **Held boards:** ``https://version.home-assistant.io/stable.json`` lists the Core and OS versions Home Assistant
  currently hands out per board. A version the Supervisor offers but the feed holds back for this board is a hold.
* **Crowd regressions:** GitHub's public issue search on the component's repository for new issues (last
  ``CROWD_DAYS`` days) that name the exact version; at or over ``CROWD_THRESHOLD`` the card holds. Unauthenticated
  (GitHub allows 10 searches a minute); at most ``CROWD_PER_DAY`` searches a day, each result cached a day.
* **Security urgency:** OSV (``api.osv.dev``) for advisories that affect the *installed* version of the Core Python
  package; the CVE ids are looked up in CISA's KEV list and FIRST's EPSS (both cached a day). An advisory makes the
  fix urgent: the release-age wait drops to 0 days. Security is an urgency signal, never a hold.

Every read is cached, bounded and fails closed to ``None`` ("unknown"): the card treats an unknown hold-type check as
*wait* and an unknown urgency as *no urgency*. $0: public endpoints, no token, daily caps.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse

from . import net

DOC = "feeds"
STABLE_URL = "https://version.home-assistant.io/stable.json"
SEARCH_URL = "https://api.github.com/search/issues"
OSV_URL = "https://api.osv.dev/v1/query"
KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
EPSS_URL = "https://api.first.org/data/v1/epss"
STABLE_TTL = 6 * 3600.0
DAY_TTL = 24 * 3600.0
CROWD_DAYS = 7
CROWD_THRESHOLD = 5
CROWD_PER_DAY = 25
CROWD_REPOS = {"core": "home-assistant/core", "os": "home-assistant/operating-system"}
RE_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,60}/[A-Za-z0-9_.-]{1,80}$")
RE_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,39}$")
RE_BOARD = re.compile(r"^[a-z0-9_-]{1,40}$")
RE_CVE = re.compile(r"^CVE-\d{4}-\d{4,9}$")
MAX_CVES = 20


def day_of(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now))


def _vtuple(v: str) -> tuple | None:
    parts = re.split(r"[-+]", str(v or ""), maxsplit=1)[0].split(".")
    try:
        return tuple(int(p) for p in parts) if parts and all(p.isdigit() for p in parts) else None
    except ValueError:
        return None


def board_version(stable, kind: str, board: str) -> str | None:
    """The version the stable feed hands out for ``board`` (Core: ``homeassistant``; OS: ``hassos``), or None."""
    if not isinstance(stable, dict) or not RE_BOARD.fullmatch(board or ""):
        return None
    table = stable.get("homeassistant" if kind == "core" else "hassos")
    v = table.get(board) if isinstance(table, dict) else None
    return v if isinstance(v, str) and RE_VERSION.fullmatch(v) else None


def held_back(stable, kind: str, board: str, target: str) -> bool | None:
    """True when the feed hands this board a *lower* version than the one offered (held back); None when unknown."""
    listed = board_version(stable, kind, board)
    a, b = _vtuple(listed or ""), _vtuple(target)
    if a is None or b is None:
        return None
    return a < b


class Feeds:
    def __init__(self, journal, clock=time.time, request=None) -> None:
        self.j, self.clock = journal, clock
        self.request = request or net.request

    def _doc(self) -> dict:
        doc = self.j.load_doc(DOC, {})
        doc = doc if isinstance(doc, dict) else {}
        doc.setdefault("stable", None)
        doc.setdefault("stable_at", 0.0)
        doc.setdefault("crowd", {})           # "repo@version" -> {"count", "at"}
        doc.setdefault("crowd_day", "")
        doc.setdefault("crowd_today", 0)
        doc.setdefault("osv", {})             # "pkg@version" -> {"cves", "at"}
        doc.setdefault("kev", None)
        doc.setdefault("kev_at", 0.0)
        doc.setdefault("epss", {})            # cve -> {"score", "at"}
        return doc

    def _get(self, url: str, accept: str = "application/json", timeout: float = 20.0):
        try:
            _, data = self.request("GET", url, {"Accept": accept}, timeout=timeout)
        except net.NetError:
            return None
        return data

    # -- held boards ---------------------------------------------------------------------------------------------
    def stable(self):
        doc = self._doc()
        now = self.clock()
        if doc["stable"] is not None and now - float(doc["stable_at"] or 0) < STABLE_TTL:
            return doc["stable"]
        data = self._get(STABLE_URL)
        if not isinstance(data, dict):
            return doc["stable"] if doc["stable"] is not None and now - float(doc["stable_at"] or 0) < DAY_TTL else None
        keep = {k: {b: v for b, v in data[k].items() if isinstance(b, str) and isinstance(v, str)}
                for k in ("homeassistant", "hassos") if isinstance(data.get(k), dict)}
        doc.update(stable=keep, stable_at=now)
        self.j.save_doc(DOC, doc)
        return keep

    # -- crowd regressions ---------------------------------------------------------------------------------------
    def crowd_count(self, repo: str, version: str) -> int | None:
        """New issues in ``repo`` from the last CROWD_DAYS naming the exact version; None when unknown or capped."""
        if not RE_REPO.fullmatch(repo or "") or not RE_VERSION.fullmatch(version or ""):
            return None
        doc = self._doc()
        now = self.clock()
        key = f"{repo}@{version}"
        hit = doc["crowd"].get(key)
        if isinstance(hit, dict) and now - float(hit.get("at") or 0) < DAY_TTL:
            return hit.get("count")
        if doc["crowd_day"] != day_of(now):
            doc.update(crowd_day=day_of(now), crowd_today=0)
        if int(doc["crowd_today"]) >= CROWD_PER_DAY:
            self.j.save_doc(DOC, doc)
            return None
        doc["crowd_today"] = int(doc["crowd_today"]) + 1
        since = time.strftime("%Y-%m-%d", time.gmtime(now - CROWD_DAYS * 86400))
        q = urllib.parse.quote(f'repo:{repo} is:issue created:>={since} "{version}"')
        data = self._get(f"{SEARCH_URL}?q={q}&per_page=1", accept="application/vnd.github+json")
        count = data.get("total_count") if isinstance(data, dict) else None
        count = int(count) if isinstance(count, int) and 0 <= count <= 100000 else None
        if count is not None:
            doc["crowd"][key] = {"count": count, "at": now}
            doc["crowd"] = dict(list(doc["crowd"].items())[-40:])
        self.j.save_doc(DOC, doc)
        return count

    # -- security urgency ----------------------------------------------------------------------------------------
    def osv_cves(self, ecosystem: str, package: str, version: str) -> list[str] | None:
        """CVE ids (or OSV ids) of advisories that affect this installed version; None when unknown."""
        if not RE_VERSION.fullmatch(version or "") or not re.fullmatch(r"[A-Za-z0-9_.-]{1,60}", package or ""):
            return None
        doc = self._doc()
        now = self.clock()
        key = f"{package}@{version}"
        hit = doc["osv"].get(key)
        if isinstance(hit, dict) and now - float(hit.get("at") or 0) < DAY_TTL:
            return hit.get("cves")
        try:
            _, data = self.request("POST", OSV_URL, {"Accept": "application/json", "Content-Type": "application/json"},
                                   {"package": {"name": package, "ecosystem": ecosystem}, "version": version}, timeout=20)
        except net.NetError:
            return None
        if not isinstance(data, dict):
            return None
        ids: list[str] = []
        for v in data.get("vulns") or []:
            if not isinstance(v, dict):
                continue
            names = [str(v.get("id") or "")] + [str(a) for a in (v.get("aliases") or []) if isinstance(a, str)]
            cve = next((n for n in names if RE_CVE.fullmatch(n)), None)
            name = cve or (names[0] if re.fullmatch(r"[A-Za-z0-9-]{1,40}", names[0]) else "")
            if name and name not in ids:
                ids.append(name)
        ids = ids[:MAX_CVES]
        doc["osv"][key] = {"cves": ids, "at": now}
        doc["osv"] = dict(list(doc["osv"].items())[-20:])
        self.j.save_doc(DOC, doc)
        return ids

    def kev(self, cves: list[str]) -> set[str] | None:
        """The given CVEs that CISA lists as known exploited; None when the feed is unknown."""
        want = [c for c in cves if RE_CVE.fullmatch(c)]
        if not want:
            return set()
        doc = self._doc()
        now = self.clock()
        if doc["kev"] is None or now - float(doc["kev_at"] or 0) >= DAY_TTL:
            data = self._get(KEV_URL, timeout=40)
            rows = data.get("vulnerabilities") if isinstance(data, dict) else None
            if isinstance(rows, list):
                doc["kev"] = sorted({str(r.get("cveID")) for r in rows if isinstance(r, dict) and RE_CVE.fullmatch(str(r.get("cveID") or ""))})
                doc["kev_at"] = now
                self.j.save_doc(DOC, doc)
            elif doc["kev"] is None:
                return None
        return {c for c in want if c in set(doc["kev"] or [])}

    def epss_max(self, cves: list[str]) -> float | None:
        """The highest EPSS probability among the CVEs; None when unknown."""
        want = [c for c in cves if RE_CVE.fullmatch(c)][:MAX_CVES]
        if not want:
            return 0.0
        doc = self._doc()
        now = self.clock()
        missing = [c for c in want if not (isinstance(doc["epss"].get(c), dict) and now - float(doc["epss"][c].get("at") or 0) < DAY_TTL)]
        if missing:
            data = self._get(f"{EPSS_URL}?cve={','.join(missing)}")
            rows = data.get("data") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                return None
            for r in rows:
                if isinstance(r, dict) and RE_CVE.fullmatch(str(r.get("cve") or "")):
                    try:
                        doc["epss"][str(r["cve"])] = {"score": max(0.0, min(1.0, float(r.get("epss") or 0))), "at": now}
                    except (TypeError, ValueError):
                        continue
            for c in missing:
                doc["epss"].setdefault(c, {"score": 0.0, "at": now})
            doc["epss"] = dict(list(doc["epss"].items())[-60:])
            self.j.save_doc(DOC, doc)
        return max(float(doc["epss"][c]["score"]) for c in want if c in doc["epss"])

    def security(self, ecosystem: str, package: str, installed: str) -> dict | None:
        """{cves, kev, epss} for the installed version; None when OSV is unknown (no urgency, never a hold)."""
        cves = self.osv_cves(ecosystem, package, installed)
        if cves is None:
            return None
        kev = self.kev(cves)
        epss = self.epss_max(cves)
        return {"cves": cves, "kev": sorted(kev) if kev is not None else None, "epss": epss}
