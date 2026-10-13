"""0.9.5 Legacy Deployer history staging (#985 Deployer migration Pi test plan R1, steps M2 and M3).

Owner pop-up 2026-10-12, "Backup + shared folder"; Deployer lane handoff #942 comment 6133340527, item 3.

Two owner-approved phases of one request type, ``LEGACY_STAGE``:

* ``phase: backup`` (M2, backup custody). The legacy Deployer (``local_house_brain_deployer``, kept stopped and
  installed) must be stopped and the store Deployer idle. A Supervisor partial backup of that one App only is made
  (never Home Assistant, folders or another App), and its custody facts are recorded: slug, name, date, size,
  exact contents. ``custody_sha256`` is the SHA-256 of that canonical record.
* ``phase: stage`` (M3). The request names ``custody_sha256``, so only that pinned backup can be used. Under the
  shared writer lock (``writer.py``; a missing lock refuses), the custody is read again and must match exactly,
  the backup is read through the Supervisor's download (memory only, bounded), its one App component is parsed by
  the restrictive reader ``legacy_snapshot.read_component`` (USTAR only, ``addon.json`` must say 0.3.6 and stopped),
  and only the five history files are written as an ``hbm.legacy_snapshot.preparation.v1`` stage by
  ``legacy_stage.stage_private`` into ``/share/house_brain_legacy_stage_v1/stage-<32 hex>/`` (0700/0600, sealed by
  ``COMMITTED.json``, read back). The result reports the component, snapshot and per-file SHA-256.

The two parser/stage modules are the inactive #975 preparation (``legacy_stage.py`` source SHA-256 01af64c7…,
``legacy_snapshot.py`` byte-identical to the Deployer's test fixture). Nothing else in the backup is kept: the App
options inside ``addon.json`` (the old GitHub token) are parsed for the version/state check only and dropped.
The legacy Deployer is never started, stopped, changed or uninstalled here. ``CONTROL_AUTHORITY=NONE``.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import tarfile
import time

from . import approval, net
from .ha import DEPLOYER_STATUS_ENTITY, MIGRATE_OLD, ForbiddenCall, HAError
from .legacy_snapshot import FILES, MAX_INPUT, Refusal, read_component
from .legacy_stage import stage_private
from .writer import WriterError

JOB = "LEGACY_STAGE"
DOC = "legacy_stage"
STAGE_ROOT = "/share/house_brain_legacy_stage_v1"
LEGACY_VERSION = "0.3.6"
MAX_BACKUP = 64 * 1024 * 1024
COMPONENTS = (f"{MIGRATE_OLD}.tar.gz", f"{MIGRATE_OLD}.tar")
OUTER_ALLOWED = frozenset({"backup.json", "./backup.json", *COMPONENTS, *(f"./{c}" for c in COMPONENTS)})
RE_SHA = re.compile(r"[0-9a-f]{64}")


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def custody_sha256(record: dict) -> str:
    return hashlib.sha256(_canonical(record)).hexdigest()


def custody_problem(info: dict) -> str | None:
    """The pinned backup must hold exactly the legacy Deployer 0.3.6, nothing else, unencrypted."""
    if info.get("addons") != [{"slug": MIGRATE_OLD, "version": LEGACY_VERSION}]:
        return "the backup must hold exactly the old Deployer 0.3.6"
    if info.get("folders") or info.get("homeassistant"):
        return "the backup holds Home Assistant or folders as well"
    if info.get("protected"):
        return "the backup is password-protected; this App cannot read it"
    return None


def outer_component(raw: bytes) -> tuple[bytes, dict]:
    """The legacy App's component from a Supervisor backup (an uncompressed outer tar holding ``backup.json`` and
    one ``<slug>.tar.gz``), memory only. Anything else in the outer tar refuses.

    0.9.9 (#985 M3 live 2026-10-13, #454 6140987570; owner "Ask them to include it"): the component is capped at
    MAX_BACKUP (64 MiB), not MAX_INPUT. A locally built App's backup also holds its Docker image (the pinned legacy
    Deployer backup is about 41 MB), so the 16 MiB cap refused it before ``normalize_component`` could drop the image.
    The kept bytes (``addon.json`` + history) stay capped at MAX_INPUT there, and the strict reader keeps its own
    limits."""
    facts: dict = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as outer:
            members = outer.getmembers()[:64]
            facts["outer_members"] = len(members)
            if any(m.name not in OUTER_ALLOWED for m in members):
                raise Refusal("BACKUP_CONTENT")
            comps = [m for m in members if m.name.lstrip("./") in COMPONENTS]
            if len(comps) != 1 or not comps[0].isreg() or comps[0].size > MAX_BACKUP:
                raise Refusal("BACKUP_COMPONENT")
            fh = outer.extractfile(comps[0])
            data = fh.read(MAX_BACKUP + 1) if fh is not None else b""
    except Refusal:
        raise
    except (tarfile.TarError, OSError, EOFError):
        raise Refusal("BACKUP_FORMAT") from None
    if not data or len(data) > MAX_BACKUP:
        raise Refusal("BACKUP_COMPONENT")
    return data, facts


PAX_KEYS = frozenset({"path", "mtime", "atime", "ctime", "size", "uid", "gid", "uname", "gname"})


def normalize_component(component: bytes) -> tuple[bytes, dict]:
    """The Supervisor writes App backups with Python's tarfile in PAX format, which adds an extended header for
    fractional timestamps, so the strict USTAR-only reader would refuse every real backup. This keeps only
    ``addon.json`` and ``data/<history file>`` as plain USTAR (fixed metadata), for the unchanged strict reader.
    Refused here: any member other than a regular file or folder, unsafe names, and PAX keys other than the plain
    metadata ones (no xattrs, sparse files or vendor extensions)."""
    keep: dict[str, bytes] = {}
    pax: set[str] = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(component), mode="r:*") as inner:
            fmt = {tarfile.USTAR_FORMAT: "ustar", tarfile.GNU_FORMAT: "gnu",
                   tarfile.PAX_FORMAT: "pax"}.get(inner.format, "unknown")
            members = inner.getmembers()
            if len(members) > 4096:
                raise Refusal("MEMBER_LIMIT")
            total = 0
            for m in members:
                if not (m.isreg() or m.isdir()):
                    raise Refusal("COMPONENT_MEMBER_TYPE")
                if set(m.pax_headers) - PAX_KEYS:
                    raise Refusal("COMPONENT_PAX")
                pax |= set(m.pax_headers)
                name = m.name[2:] if m.name.startswith("./") else m.name
                parts = name.rstrip("/").split("/")
                if name.startswith("/") or "\\" in name or any(p in ("..",) for p in parts):
                    raise Refusal("MEMBER_PATH")
                if m.isreg() and (name == "addon.json" or (name.startswith("data/") and name[5:] in FILES)):
                    if name in keep:
                        raise Refusal("MEMBER_DUPLICATE")
                    total += m.size
                    if m.size > MAX_INPUT or total > MAX_INPUT:
                        raise Refusal("HISTORY_LIMIT")
                    fh = inner.extractfile(m)
                    keep[name] = fh.read() if fh is not None else b""
    except Refusal:
        raise
    except (tarfile.TarError, OSError, EOFError):
        raise Refusal("COMPONENT_FORMAT") from None
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        def add(name, raw=None):
            info = tarfile.TarInfo(name)
            info.mtime, info.uid, info.gid, info.uname, info.gname = 0, 0, 0, "", ""
            if raw is None:
                info.type, info.mode = tarfile.DIRTYPE, 0o755
                tar.addfile(info)
            else:
                info.mode, info.size = 0o644, len(raw)
                tar.addfile(info, io.BytesIO(raw))
        if "addon.json" in keep:
            add("addon.json", keep["addon.json"])
        if any(n.startswith("data/") for n in keep):
            add("data")
            for name in sorted(n for n in keep if n.startswith("data/")):
                add(name, keep[name])
    return out.getvalue(), {"inner_format": fmt, "pax_keys": ",".join(sorted(pax))[:80]}


def component_inventory(component: bytes) -> tuple[dict, dict]:
    """Which of the five history files the component holds, with their SHA-256 (an input to the strict reader,
    which checks everything again), plus format facts for a refusal report (member type letters only)."""
    expected = {name: None for name in FILES}
    kinds: set[str] = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(component), mode="r:*") as inner:
            for m in inner.getmembers()[:4096]:
                kinds.add(m.type.decode("ascii", "replace") if isinstance(m.type, bytes) else str(m.type))
                name = m.name[2:] if m.name.startswith("./") else m.name
                if name.startswith("data/") and name[5:] in FILES and m.isreg() and m.size <= MAX_INPUT:
                    fh = inner.extractfile(m)
                    expected[name[5:]] = hashlib.sha256(fh.read() if fh is not None else b"").hexdigest()
            fmt = {"inner_format": {tarfile.USTAR_FORMAT: "ustar", tarfile.GNU_FORMAT: "gnu",
                                    tarfile.PAX_FORMAT: "pax"}.get(inner.format, "unknown")}
    except (tarfile.TarError, OSError, EOFError):
        raise Refusal("COMPONENT_FORMAT") from None
    return expected, {**fmt, "member_types": "".join(sorted(kinds))[:16]}


def _stage_root(root: str) -> int:
    """Open (creating once, 0700) the shared stage folder; an existing one must be a 0700 folder of this user."""
    parent, name = os.path.split(root)
    pfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            os.mkdir(name, 0o700, dir_fd=pfd)
            os.fsync(pfd)
        except FileExistsError:
            pass
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=pfd)
    finally:
        os.close(pfd)
    st = os.fstat(fd)
    if stat.S_IMODE(st.st_mode) != 0o700 or st.st_uid != os.geteuid():
        os.close(fd)
        raise Refusal("STAGE_ROOT")
    return fd


class LegacyStager:
    def __init__(self, engine, stage_root: str = STAGE_ROOT) -> None:
        self.e, self.ha, self.j, self.s = engine, engine.ha, engine.j, engine.s
        self.stage_root = stage_root

    def _deployers_quiet(self) -> str | None:
        detail = self.ha.app_detail(MIGRATE_OLD)
        if detail.state != "stopped":
            return f"the old Deployer is {detail.state}; it must be stopped (and stay installed)"
        st = self.ha.entity_state(DEPLOYER_STATUS_ENTITY) or {}
        if str(st.get("state") or "").upper() == "DEPLOYING":
            return "the store Deployer is installing something right now"
        return None

    def run(self, m):
        from .jobs import DONE, DRY_RUN_OK, FAILED, REFUSED, REJECTED, Result
        self._r = (DONE, DRY_RUN_OK, FAILED, REFUSED, REJECTED, Result)
        try:
            return self._backup(m) if m.legacy_phase == "backup" else self._stage(m)
        except ForbiddenCall as err:
            return Result(REFUSED, [f"not allowed: {str(err)[:120]}"])

    def _backup(self, m):
        DONE, DRY_RUN_OK, FAILED, REFUSED, REJECTED, Result = self._r
        problem = self._deployers_quiet()
        if problem:
            return Result(REFUSED, [problem + "; nothing asked or changed"])
        if self.s.dry_run:
            return Result(DRY_RUN_OK, ["dry run: the backup would be asked; nothing changed"])
        outcome = self.e._ask(m, "Maintenance: back up the old Deployer?", [
            "Make a backup of the old, stopped House Brain Deployer (0.3.6) only, for its history move (step M2).",
            "Nothing else is backed up; the old Deployer is not started, changed or removed."])
        if outcome != approval.APPROVE:
            return Result(REJECTED, [f"owner decision: {outcome}"])
        name = f"hbm legacy deployer custody {time.strftime('%Y-%m-%d %H%M', time.gmtime())}"
        try:
            with self.ha.legacy_target():
                bslug = self.ha.legacy_backup(name)
            with self.ha.legacy_target(bslug):
                info = self.ha.legacy_backup_info()
        except (HAError, net.NetError) as err:
            return Result(FAILED, [f"the backup did not finish ({net.redact(str(err))[:160]}); nothing else changed"])
        problem = custody_problem(info)
        if problem:
            return Result(FAILED, [problem, "the backup is kept for you to look at; it is not pinned"],
                          {"backup": info["slug"]})
        digest = custody_sha256(info)
        self.j.save_doc(DOC, {"custody": info, "custody_sha256": digest, "at": time.time()})
        self.j.audit(m.request_id, "LEGACY_CUSTODY", backup=info["slug"], custody_sha256=digest)
        return Result(DONE, [f"pinned backup {info['slug']} holds exactly the old Deployer 0.3.6",
                             "next: a LEGACY_STAGE request with phase stage and this custody_sha256"],
                      {"custody": info, "custody_sha256": digest})

    def _stage(self, m):
        DONE, DRY_RUN_OK, FAILED, REFUSED, REJECTED, Result = self._r
        doc = self.j.load_doc(DOC, {})
        doc = doc if isinstance(doc, dict) else {}
        custody = doc.get("custody")
        if not isinstance(custody, dict) or doc.get("custody_sha256") != m.custody_sha256:
            return Result(REFUSED, ["no pinned backup with that custody_sha256 (run phase backup first); nothing "
                                    "asked"])
        facts = {"custody_sha256": m.custody_sha256, "backup": custody.get("slug")}
        writer = getattr(self.e, "writer", None)
        ident = writer.identity() if writer is not None else {"state": "none"}
        if ident.get("state") != "enrolled":
            return Result(REFUSED, [f"the shared writer lock is not usable ({ident.get('state')}); nothing asked"],
                          facts)
        problem = self._deployers_quiet()
        if problem:
            return Result(REFUSED, [problem + "; nothing asked or changed"], facts)
        if self.s.dry_run:
            return Result(DRY_RUN_OK, ["dry run: the staging would be asked; nothing read or written"], facts)
        outcome = self.e._ask(m, "Maintenance: stage the old Deployer's history?", [
            f"Copy the old Deployer's history from its pinned backup {custody.get('slug')} into the stage folder "
            "for the Deployer's history move (step M3).",
            "Only its 5 history files are kept; its settings in the backup are not. Held under the shared writer "
            "lock. The old Deployer is not started, changed or removed."])
        if outcome != approval.APPROVE:
            return Result(REJECTED, [f"owner decision: {outcome}"], facts)
        try:
            with writer.hold() as lease:
                if lease is None:
                    return Result(REFUSED, ["the shared writer lock is no longer enrolled; nothing read"], facts)
                with self.ha.legacy_target(custody["slug"]):
                    info = self.ha.legacy_backup_info()
                    if custody_sha256(info) != m.custody_sha256:
                        return Result(REFUSED, ["the pinned backup changed since its custody was recorded; "
                                                "nothing staged"], facts)
                    raw = self.ha.legacy_backup_download(MAX_BACKUP)
                lease.check()
                component, outer = outer_component(raw)
                del raw
                facts.update(outer)
                facts["original_component_sha256"] = hashlib.sha256(component).hexdigest()
                normalized, fmt = normalize_component(component)
                del component
                component_sha = hashlib.sha256(normalized).hexdigest()
                expected, _ = component_inventory(normalized)
                facts.update(fmt, component_sha256=component_sha)
                snapshot = read_component(normalized, component_sha256=component_sha, expected_files=expected)
                del normalized
                lease.check()
                root = _stage_root(self.stage_root)
                try:
                    name = stage_private(root, snapshot)
                finally:
                    os.close(root)
        except WriterError as err:
            return Result(REFUSED if str(err) == "WRITER_BUSY" else FAILED,
                          [f"{err}: the shared writer lock could not be held; nothing staged"], facts)
        except Refusal as err:
            self.j.audit(m.request_id, "LEGACY_STAGE_REFUSED", code=str(err))
            return Result(FAILED, [f"{err}: the backup's content was refused by the strict reader; nothing staged "
                                   "(a partial stage folder, if any, is kept for inspection)"], facts)
        except (HAError, net.NetError, OSError) as err:
            return Result(FAILED, [f"the staging did not finish ({net.redact(str(err))[:160]}); nothing used"], facts)
        receipt = snapshot.receipt()
        doc.update(stage={"name": name, "component_sha256": receipt["component_sha256"],
                          "snapshot_sha256": receipt["snapshot_sha256"], "files": receipt["files"],
                          "at": time.time()})
        self.j.save_doc(DOC, doc)
        self.j.audit(m.request_id, "LEGACY_STAGED", stage=name, snapshot_sha256=receipt["snapshot_sha256"])
        facts.update(stage=name, stage_root=self.stage_root, snapshot_sha256=receipt["snapshot_sha256"],
                     files=receipt["files"], history_bytes=receipt["history_bytes"], schema=receipt["schema"])
        return Result(DONE, [f"staged {sum(1 for v in receipt['files'].values() if v)} history files as {name}; "
                             "sealed and read back", "next: the Deployer's prepare step (M4) reads this stage"],
                      facts)
