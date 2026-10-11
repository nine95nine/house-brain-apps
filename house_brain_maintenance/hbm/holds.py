"""0.7.3 hold list (owner pop-ups 2026-10-08: "Hold list", "Auto-hold .0 + my list", "AI files a request").

Why: on 2026-10-08 the owner had decided to wait for Core 2026.10.1, but the phone prompt for 2026.10.0 came back
after the re-ask time and was approved by mistake; GoveeLife failed to start right after. A held update is never
sent to the phone, so a tap cannot approve it.

* Items (exact text, lower case):
  ``core`` / ``os``                  every Core / OS update until released;
  ``core@2026.10.0`` / ``os@18.4``   that one version;
  ``app:<slug>`` / ``app:<slug>@<v>`` an App (all versions / one version);
  ``update.<entity>``                a HACS or firmware update item (same meaning as the ``update_hold`` option).
* Auto-hold (option ``hold_core_dot_zero``, default on): every new Core ``YYYY.M.0`` is held until a later patch is
  offered (the owner's standing rule "wait for .1"). ``RELEASE_UPDATE core@YYYY.M.0`` lets that one through.
* Held items are skipped by every update path (Apps, Core/OS, HACS/firmware, ``ASK_UPDATE_NOW``). They change only
  this App's own memory (``/data``); nothing in Home Assistant is touched, and nothing is asked.
* The list changes only through ``HOLD_UPDATE`` / ``RELEASE_UPDATE`` requests (no approval: they ask nothing and
  only make the App ask less), and by an owner-approved Undo of an update (that version is held).
"""
from __future__ import annotations

import re
import time

from .journal import Journal

DOC = "holds"
MAX_ITEMS = 50
RE_VER = r"[0-9A-Za-z][0-9A-Za-z._+-]{0,39}"
RE_ITEM = re.compile(rf"^(?:(?:core|os)(?:@{RE_VER})?|app:[a-z0-9][a-z0-9_]{{0,99}}(?:@{RE_VER})?"
                     r"|update\.[a-z0-9_]{1,200})$")
RE_DOT_ZERO = re.compile(r"^[0-9]{4}\.[0-9]{1,2}\.0$")


def valid_item(item: object) -> bool:
    return isinstance(item, str) and bool(RE_ITEM.fullmatch(item))


class Holds:
    def __init__(self, journal: Journal, *, dot_zero: bool = True, clock=time.time) -> None:
        self.j = journal
        self.dot_zero = dot_zero
        self.clock = clock

    def _doc(self) -> dict:
        doc = self.j.load_doc(DOC, {})
        for key in ("items", "released"):
            if not isinstance(doc.get(key), dict):
                doc[key] = {}
        return doc

    def items(self) -> dict[str, dict]:
        return dict(self._doc()["items"])

    def released(self) -> dict[str, float]:
        return dict(self._doc()["released"])

    def hold(self, item: str, by: str, note: str = "") -> tuple[bool, str]:
        if not valid_item(item):
            return False, "not a hold item"
        doc = self._doc()
        if item in doc["items"]:
            return True, f"{item} was already held"
        if len(doc["items"]) >= MAX_ITEMS:
            return False, f"the hold list is full ({MAX_ITEMS} items); release one first"
        doc["items"][item] = {"at": self.clock(), "by": str(by)[:64], "note": str(note)[:120]}
        doc["released"].pop(item, None)
        self.j.save_doc(DOC, doc)
        return True, f"{item} is held"

    def release(self, item: str) -> tuple[bool, str]:
        if not valid_item(item):
            return False, "not a hold item"
        doc = self._doc()
        was = doc["items"].pop(item, None) is not None
        auto = self._auto_core(item)
        if not was and not auto:
            return False, f"{item} is not held"
        if auto:
            doc["released"][item] = self.clock()   # lets this .0 version through the automatic hold
        self.j.save_doc(DOC, doc)
        return True, f"{item} is released"

    def _auto_core(self, item: str) -> bool:
        return self.dot_zero and item.startswith("core@") and bool(RE_DOT_ZERO.fullmatch(item[5:]))

    def why_held(self, kind: str, version: str | None, slug: str | None = None) -> str | None:
        """Reason text when this update is held, else None. ``kind``: core | os | app."""
        doc = self._doc()
        held = doc["items"]
        v = str(version or "")
        if kind in ("core", "os"):
            if f"{kind}@{v}" in held:
                return f"{kind} {v} is on your hold list"
            if kind in held:
                return f"all {'Core' if kind == 'core' else 'OS'} updates are on your hold list"
            if kind == "core" and self._auto_core(f"core@{v}") and f"core@{v}" not in doc["released"]:
                return f"Core {v} is a .0 release: held until a later patch (your standing rule)"
            return None
        if kind == "app" and slug:
            if f"app:{slug}@{v}" in held:
                return f"{slug} {v} is on your hold list"
            if f"app:{slug}" in held:
                return f"all updates of {slug} are on your hold list"
        return None

    def entity_ids(self) -> frozenset[str]:
        return frozenset(i for i in self._doc()["items"] if i.startswith("update."))

    def summary(self) -> dict:
        doc = self._doc()
        return {"held": sorted(doc["items"]), "auto_hold_core_dot_zero": self.dot_zero,
                "released_dot_zero": sorted(doc["released"])}
