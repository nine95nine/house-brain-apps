"""Owner "Undo" of a recent install (0.3.3, owner-approved 2026-09-29).

Every successful install is recorded (files, hashes before/after, backup names, safety checks).
Undo turns that record into the exact reverse install, whose bytes come from the ``.bak`` copies
already on disk (hash-verified), and runs it through the normal engine: partial backup, journaled
no-clobber steps, strict config check, restart approval, health checks, automatic restore.

Rules (all enforced here or by the engine's state checks):
* only the newest install of each file is offered, and only while every file is still exactly as
  that install left it (no out-of-order or stale undo);
* the owner's authenticated single-use tap on the page is the deploy approval; the restart still
  needs its own approval;
* nothing an AI files can start an undo; undo never counts against the daily limit;
* the undone version is kept as ``<file>.undone_<tag>.bak``; a retired file is re-created from its
  ``.bak``; a created file is retired (renamed), never deleted.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict

from .manifest import RE_REQUEST_ID, FileOp, Manifest, RetireOp, Source, StateCheck
from .policy import check_backup_path, check_package_target

SUFFIX = "#undo"
MAX_OFFERS = 5


class UndoError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def record_success(journal, m: Manifest, pre: dict, post: dict) -> None:
    if m.request_id.endswith(SUFFIX):
        journal.mark_undone(m.request_id[: -len(SUFFIX)], m.request_id)
        return
    journal.record_install({
        "request_id": m.request_id, "title": m.title, "summary": m.summary,
        "tracking_issue": m.tracking_issue, "at": time.time(),
        "files": [{"target": op.target, "mode": op.mode, "backup_as": op.backup_as,
                   "pre": pre.get(op.target), "post": post.get(op.target)} for op in m.files],
        "retire": [{"path": r.path, "rename_to": r.rename_to, "sha": pre.get(r.path)} for r in m.retire],
        "preconditions": [asdict(c) for c in m.preconditions],
        "expect_unchanged": list(m.expect_unchanged),
        "log_tokens": list(m.log_tokens), "log_fail_levels": list(m.log_fail_levels),
        "settle_seconds": m.settle_seconds,
    })


def _tag(rec: dict) -> str:
    return "undone_" + hashlib.sha256(f"{rec['request_id']}|{rec['at']}".encode()).hexdigest()[:8]


def build(rec: dict) -> tuple[Manifest, dict[str, tuple[str, str]]]:
    """Reverse manifest + {target: (local source path, expected sha)}. Raises UndoError."""
    rid = rec.get("request_id", "")
    if not isinstance(rid, str) or not RE_REQUEST_ID.fullmatch(rid):
        raise UndoError("RECORD")
    tag = _tag(rec)
    files: list[FileOp] = []
    retire: list[RetireOp] = []
    sources: dict[str, tuple[str, str]] = {}
    for f in rec.get("files", []):
        target = check_package_target(f["target"])
        if not f.get("post"):
            raise UndoError("RECORD", "missing post hash")
        if f["mode"] == "replace":
            if not f.get("pre") or not f.get("backup_as"):
                raise UndoError("RECORD", "missing backup")
            backup = check_backup_path(f["backup_as"])
            keep = check_backup_path(f"{target}.{tag}.bak")
            files.append(FileOp(target, "replace", f["pre"], Source("local", backup), keep, f["post"]))
            sources[target] = (backup, f["pre"])
        else:   # created by the install -> retire it (rename, never delete)
            retire.append(RetireOp(target, check_backup_path(f"{target}.{tag}.bak"), f["post"]))
    for r in rec.get("retire", []):
        path, kept = check_package_target(r["path"]), check_backup_path(r["rename_to"])
        if not r.get("sha"):
            raise UndoError("RECORD", "missing retired hash")
        files.append(FileOp(path, "create", r["sha"], Source("local", kept)))
        sources[path] = (kept, r["sha"])
    if not files and not retire:
        raise UndoError("RECORD", "nothing to undo")
    checks = tuple(StateCheck(c["entity_id"], c.get("attribute"), c.get("equals"),
                              tuple(c["one_of"]) if c.get("one_of") is not None else None, c.get("number"))
                   for c in rec.get("preconditions", []))
    digest = hashlib.sha256(("undo|" + json.dumps(rec, sort_keys=True)).encode()).hexdigest()
    m = Manifest(rid + SUFFIX, f"Undo: {rec.get('title', '')}"[:80], "owner", "local", "0" * 40,
                 int(rec.get("tracking_issue") or 0), "owner undo", tuple(files), tuple(retire), checks,
                 tuple(rec.get("expect_unchanged", [])), (), tuple(rec.get("log_tokens", [])),
                 tuple(rec.get("log_fail_levels", ["CRITICAL", "ERROR"])), int(rec.get("settle_seconds", 300)),
                 digest=digest, summary=f"Put back what {rid} replaced")
    return m, sources


def _claims(rec: dict) -> set[str]:
    return {f["target"] for f in rec.get("files", [])} | {r["path"] for r in rec.get("retire", [])}


def offers(journal, packages) -> list[dict]:
    """Newest-first installs the owner may undo right now (at most MAX_OFFERS)."""
    out: list[dict] = []
    claimed: set[str] = set()
    for rec in sorted(journal.installs(), key=lambda r: r.get("at", 0), reverse=True):
        mine = _claims(rec)
        fresh = not (mine & claimed) and not rec.get("undone_by")
        claimed |= mine
        if not fresh or len(out) >= MAX_OFFERS:
            continue
        try:
            paths = [f["target"] for f in rec["files"]] + [f["backup_as"] for f in rec["files"] if f.get("backup_as")]
            paths += [p for r in rec["retire"] for p in (r["path"], r["rename_to"])]
            live = packages.snapshot(paths)
        except Exception:  # noqa: BLE001, S112 - unreadable record or files -> simply not offered
            continue
        ok = all(live[f["target"]] == f["post"] for f in rec["files"])
        ok = ok and all(live[f["backup_as"]] == f["pre"] for f in rec["files"] if f.get("backup_as"))
        ok = ok and all(live[r["path"]] is None and live[r["rename_to"]] == r["sha"] for r in rec["retire"])
        if ok:
            out.append({"request_id": rec["request_id"], "title": rec.get("title", ""), "at": rec.get("at", 0)})
    return out
