"""Independent additive No-Loss Dashboard Evolution Contract (NDEC) subset for D5.

This checker proves preservation against the *entire* baseline storage
dashboard, including opaque/custom/strategy cards: it does not try to parse
or rewrite the homeowner's existing Lovelace YAML/JSON. It accepts only
append-only cards/sections/views and explicitly allowed title/icon edits.
It never regards a new action, entity or removal as owner-authorized.

This narrow check is NOT a replacement for Dashboard Steward's full NDEC,
registry inventory, or independently authenticated owner-removal policy.
All changes beyond safe presentation append operations remain blocked.
"""
from __future__ import annotations


class NoLossViolation(ValueError):
    """One closed reason code; no user dashboard contents in exception text."""


def _fail(reason: str):
    raise NoLossViolation(reason)


def _cards_preserved(before, after):
    if type(before) is not list or type(after) is not list:
        _fail("NDEC_CARDS_TYPE")
    if len(after) < len(before):
        _fail("NDEC_CARD_REMOVED")
    if after[:len(before)] != before:
        _fail("NDEC_CARD_CHANGED_OR_MOVED")


def _sections_preserved(before, after):
    if type(before) is not list or type(after) is not list:
        _fail("NDEC_SECTIONS_TYPE")
    if len(after) < len(before):
        _fail("NDEC_SECTION_REMOVED")
    for index, section in enumerate(before):
        new = after[index]
        if not isinstance(section, dict) or not isinstance(new, dict):
            if new != section:
                _fail("NDEC_SECTION_CHANGED")
            continue
        orig = {k: v for k, v in section.items() if k != "cards"}
        fresh = {k: v for k, v in new.items() if k != "cards"}
        if orig != fresh:
            _fail("NDEC_SECTION_CHANGED")
        if "cards" in section:
            _cards_preserved(section["cards"], new.get("cards"))
        elif "cards" in new:
            # Original strategy/automatically-generated section must not be
            # mutated into static cards without a distinct future qualification.
            _fail("NDEC_SECTION_CARD_SURFACE_ADDED")


def prove_additive(before: dict, after: dict) -> None:
    """Fail closed if an original dashboard component, action or pin disappears.

    No operation is allowed to change the dashboard's root settings, reorder
    baseline views, alter any existing card's action/target/confirmation, or
    change navigation visibility. The before/after structural comparison
    applies to untouched preexisting components *even if custom or opaque*.
    """
    if not isinstance(before, dict) or not isinstance(after, dict):
        _fail("NDEC_DASHBOARD_SHAPE")
    if {k: v for k, v in before.items() if k != "views"} != {
        k: v for k, v in after.items() if k != "views"
    }:
        _fail("NDEC_ROOT_CHANGED")
    old_views = before.get("views")
    new_views = after.get("views")
    if type(old_views) is not list or type(new_views) is not list:
        _fail("NDEC_VIEWS_TYPE")
    if len(new_views) < len(old_views):
        _fail("NDEC_VIEW_REMOVED")
    for index, old in enumerate(old_views):
        new = new_views[index]
        if not isinstance(old, dict) or not isinstance(new, dict):
            if new != old:
                _fail("NDEC_VIEW_CHANGED")
            continue
        preserved_keys = {"cards", "sections", "title", "icon"}
        if {k: v for k, v in old.items() if k not in preserved_keys} != {
            k: v for k, v in new.items() if k not in preserved_keys
        }:
            _fail("NDEC_VIEW_CONTROL_OR_NAVIGATION_CHANGED")
        if "title" in new and not isinstance(new["title"], str):
            _fail("NDEC_VIEW_TITLE_SHAPE")
        if "icon" in new and not isinstance(new["icon"], str):
            _fail("NDEC_VIEW_ICON_SHAPE")
        if "cards" in old:
            _cards_preserved(old["cards"], new.get("cards"))
        elif "cards" in new:
            _fail("NDEC_VIEW_CARDS_ADDED_TO_OPAQUE")
        if "sections" in old:
            _sections_preserved(old["sections"], new.get("sections"))
        elif "sections" in new:
            _fail("NDEC_VIEW_SECTIONS_ADDED_TO_OPAQUE")
