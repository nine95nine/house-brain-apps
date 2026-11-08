"""0.9.54 HISTORY_ARCHIVE: keep every day of history, compressed, for the House Brain AIs (history archive proposal
R1, ``docs/architecture/HOUSE_BRAIN_HISTORY_ARCHIVE_PROPOSAL_R1.md``; owner pop-ups 2026-10-24 "Design the compressed
archive" and 2026-11-08 "Build only": "save it somewhere, compress it, so that the AIs can learn from it").

Home Assistant deletes raw history after 40 days (``purge_keep_days``). This job reads whole UTC days through Core's
own read-only WebSocket APIs (the 0.9.27 ``HISTORY_MEASURE`` reader: ``history/history_during_period`` with attributes,
``recorder/statistics_during_period`` for the 5-minute statistics) and writes each day to

    /share/house_brain_history_archive/YYYY/MM/YYYY-MM-DD.jsonl.xz      (xz, Python standard library)
    /share/house_brain_history_archive/YYYY/MM/YYYY-MM-DD.meta.json     (counts, sha256, what was left out)
    /share/house_brain_history_archive/index.json                       (every archived day, newest last)

Day file format (JSON Lines, one object per line):
  line 1: {"schema": "house_brain_history_archive.v1", "day_utc", "window_utc", "start_epoch", "row": [...]}
  then:   {"e": entity_id, "r": [[seconds since window start, state, changed attributes only], ...]}
  then:   {"st": statistic_id, "p": "5minute", "r": [statistics rows as Core returns them]}

Rules:
* Read-only toward Home Assistant; writes only under the archive root.
* A day is written to a temporary name, read back and checked (line count and SHA-256), then renamed. A finished day
  is never overwritten. A day that hits the time cap is discarded and tried again later: never a partial day.
* Privacy: restricted domains (``contracts/house_brain_privacy_data_classes.v1.json`` ``restricted_ha_domains``),
  free-text helpers (``input_text``) and the owner's deny prefixes (read policy) are never read. The archive stays on
  the Pi; nothing is posted anywhere except counts.
* Disk guard: nothing is written while free space under the root is below ``MIN_FREE_BYTES``.
* Only days Core still holds are attempted (the last 39 complete UTC days); older days are reported as gone.
``CONTROL_AUTHORITY=NONE``.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import lzma
import os
import shutil
import time

from . import issues as I
from .histmeasure import WS_MAX_BYTES, archive_rows, day_window
from .jobs import DONE, FAILED, REFUSED, Result

ARCHIVE_ROOT = "/share/house_brain_history_archive"
SCHEMA = "house_brain_history_archive.v1"
PRESET = 6
MIN_FREE_BYTES = 20 * 1024 ** 3          # stop writing below 20 GB free
KEEP_DAYS = 39                           # Core keeps 40 days; the oldest is already being purged
RESTRICTED_DOMAINS = frozenset({"alarm_control_panel", "camera", "device_tracker", "geo_location", "image", "lock",
                                "person", "zone"})
FREE_TEXT_DOMAINS = frozenset({"input_text"})
DENY_PREFIXES = ("sensor.hb_health_", "binary_sensor.hb_health_")
STAT_PERIOD = "5minute"                  # hourly statistics are kept forever by Core itself
MAX_RANGE_DAYS = 45
_monotonic = time.monotonic              # tests replace it
_now = time.time


def allowed(entity_id: str) -> bool:
    domain = entity_id.split(".", 1)[0]
    return (domain not in RESTRICTED_DOMAINS and domain not in FREE_TEXT_DOMAINS
            and not entity_id.startswith(DENY_PREFIXES))


def day_paths(root: str, day: str) -> tuple[str, str]:
    folder = os.path.join(root, day[:4], day[5:7])
    return os.path.join(folder, f"{day}.jsonl.xz"), os.path.join(folder, f"{day}.meta.json")


def oldest_available(now: float | None = None) -> str:
    """The oldest UTC day Core can still return in full (today - KEEP_DAYS)."""
    t = (now if now is not None else _now()) - KEEP_DAYS * 86400
    return time.strftime("%Y-%m-%d", time.gmtime(t))


def yesterday(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime((now if now is not None else _now()) - 86400))


def days_between(first: str, last: str) -> list[str]:
    a = calendar.timegm(time.strptime(first, "%Y-%m-%d"))
    b = calendar.timegm(time.strptime(last, "%Y-%m-%d"))
    return [time.strftime("%Y-%m-%d", time.gmtime(t)) for t in range(a, b + 1, 86400)]


def _line(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), default=str).encode() + b"\n"


class CapReached(Exception):
    """The time cap came before the day was complete; the temporary file is discarded."""


def _read_entities(ha, sock_holder: list, start_iso: str, end_iso: str, ids: list[str], chunk: int, deadline: float,
                   on_rows) -> list[str]:
    """Read ``ids`` in chunks (split on failure, like HISTORY_MEASURE); call ``on_rows(eid, rows)``; return unread."""
    unread: list[str] = []
    queue = [ids[i:i + chunk] for i in range(0, len(ids), chunk)]
    while queue:
        if _monotonic() >= deadline:
            raise CapReached()
        part_ids = queue.pop(0)
        try:
            part = ha.history_full(sock_holder[0], start_iso, end_iso, part_ids)
        except Exception:  # noqa: BLE001 - Core said no or the answer was too big: split, on a fresh socket
            try:
                sock_holder[0].close()
            except Exception:  # noqa: BLE001
                pass
            sock_holder[0] = ha.ws(max_size=WS_MAX_BYTES)
            if len(part_ids) > 1:
                mid = len(part_ids) // 2
                queue[0:0] = [part_ids[:mid], part_ids[mid:]]
            else:
                unread.append(part_ids[0])
            continue
        for eid in part_ids:
            rows = part.get(eid) if isinstance(part, dict) else None
            on_rows(eid, rows if isinstance(rows, list) else [])
        del part
    return unread


def archive_day(ha, day: str, root: str, chunk: int, deadline: float, ids: list[str] | None = None) -> dict:
    """Archive one UTC day. Returns a status dict: status in {WRITTEN, EXISTS, CAP, EMPTY}."""
    final, meta_path = day_paths(root, day)
    if os.path.exists(final) and os.path.exists(meta_path):
        return {"day": day, "status": "EXISTS"}
    start_iso, end_iso, start_epoch = day_window(day)
    all_ids = ids if ids is not None else ha.entity_ids()
    keep = [e for e in all_ids if allowed(e)]
    left_out = len(all_ids) - len(keep)
    os.makedirs(os.path.dirname(final), exist_ok=True)
    tmp = final + ".tmp"
    counts = {"entities": 0, "entities_with_rows": 0, "rows": 0, "statistics_ids": 0, "statistics_rows": 0}
    sock_holder = [ha.ws(max_size=WS_MAX_BYTES)]
    try:
        with lzma.open(tmp, "wb", preset=PRESET) as out:
            out.write(_line({"schema": SCHEMA, "day_utc": day, "window_utc": [start_iso, end_iso],
                             "start_epoch": start_epoch, "row": ["t_since_window_start_s", "state",
                                                                 "changed_attributes"]}))
            lines = 1

            def on_rows(eid, rows):
                nonlocal lines
                counts["entities"] += 1
                if rows:
                    counts["entities_with_rows"] += 1
                    counts["rows"] += len(rows)
                    out.write(_line({"e": eid, "r": archive_rows(rows, start_epoch)}))
                    lines += 1

            unread = _read_entities(ha, sock_holder, start_iso, end_iso, keep, chunk, deadline, on_rows)
            sids = [s for s in ha.statistic_ids(sock_holder[0]) if allowed(s)]
            counts["statistics_ids"] = len(sids)
            for i in range(0, len(sids), 50):
                if _monotonic() >= deadline:
                    raise CapReached()
                part = ha.statistics_full(sock_holder[0], start_iso, end_iso, sids[i:i + 50], STAT_PERIOD)
                for sid in sids[i:i + 50]:
                    rows = part.get(sid) if isinstance(part, dict) else None
                    if isinstance(rows, list) and rows:
                        counts["statistics_rows"] += len(rows)
                        out.write(_line({"st": sid, "p": STAT_PERIOD, "r": rows}))
                        lines += 1
                del part
    except CapReached:
        _discard(tmp)
        return {"day": day, "status": "CAP"}
    except BaseException:
        _discard(tmp)
        raise
    finally:
        try:
            sock_holder[0].close()
        except Exception:  # noqa: BLE001
            pass
    if counts["entities_with_rows"] == 0:
        _discard(tmp)
        return {"day": day, "status": "EMPTY"}
    digest, size, read_back = _verify(tmp)
    if read_back != lines:
        _discard(tmp)
        raise OSError(f"ARCHIVE_READBACK_MISMATCH:{day}")
    meta = {"schema": SCHEMA + ".meta", "day_utc": day, "window_utc": [start_iso, end_iso], "file": os.path.basename(final),
            "sha256": digest, "bytes": size, "lines": lines, **counts, "unread": len(unread),
            "unread_ids": [I.scrub(e, 200) for e in unread[:20]], "left_out_private": left_out,
            "left_out_rules": ["restricted_ha_domains", "input_text", "owner_deny_prefixes"],
            "written_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_now())), "control_authority": "NONE"}
    os.replace(tmp, final)
    with open(meta_path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=1, sort_keys=True)
    os.replace(meta_path + ".tmp", meta_path)
    _update_index(root, meta)
    return {"day": day, "status": "WRITTEN", "bytes": size, "rows": counts["rows"], "unread": len(unread)}


def _discard(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _verify(path: str) -> tuple[str, int, int]:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    lines = 0
    with lzma.open(path, "rb") as fh:
        for raw in fh:
            json.loads(raw)                        # every line must be valid JSON
            lines += 1
    return h.hexdigest(), os.path.getsize(path), lines


def _update_index(root: str, meta: dict) -> None:
    path = os.path.join(root, "index.json")
    try:
        with open(path, encoding="utf-8") as fh:
            idx = json.load(fh)
        if not isinstance(idx, dict) or not isinstance(idx.get("days"), dict):
            raise ValueError
    except (OSError, ValueError):
        idx = {"schema": SCHEMA + ".index", "days": {}}
    idx["days"][meta["day_utc"]] = {k: meta[k] for k in ("file", "sha256", "bytes", "rows", "entities_with_rows",
                                                         "statistics_rows", "unread")}
    idx["days"] = dict(sorted(idx["days"].items()))
    idx["format"] = "see the day file's first line; rows are [seconds since window start, state, changed attributes]"
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(idx, fh, indent=1, sort_keys=True)
    os.replace(path + ".tmp", path)


def free_bytes(root: str) -> int:
    probe = root
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    return shutil.disk_usage(probe).free


def run_range(ha, root: str, first: str, last: str, chunk: int, minutes: int) -> Result:
    """Archive every missing day in [first, last] that Core still holds, oldest first, inside ``minutes``."""
    began = _monotonic()
    deadline = began + minutes * 60
    floor = oldest_available()
    days = days_between(first, last)
    gone = [d for d in days if d < floor]
    todo = [d for d in days if d >= floor]
    if free_bytes(root) < MIN_FREE_BYTES:
        return Result(REFUSED, [f"less than {MIN_FREE_BYTES // 1024 ** 3} GB free under the archive folder; "
                                "nothing was written"], {"free_bytes": free_bytes(root), "days": len(days)})
    ids = ha.entity_ids() if todo else []
    written, existing, capped, empty = [], [], [], []
    total_bytes = 0
    for day in todo:
        if _monotonic() >= deadline:
            capped.append(day)
            continue
        if free_bytes(root) < MIN_FREE_BYTES:
            capped.append(day)
            continue
        st = archive_day(ha, day, root, chunk, deadline, ids)
        if st["status"] == "WRITTEN":
            written.append(day)
            total_bytes += st["bytes"]
        elif st["status"] == "EXISTS":
            existing.append(day)
        elif st["status"] == "CAP":
            capped.append(day)
        else:
            empty.append(day)
    facts = {"range_utc": [first, last], "written": written, "already_archived": len(existing),
             "not_done_yet": capped, "empty": empty, "gone_before_archive": gone, "bytes_written": total_bytes,
             "seconds": round(_monotonic() - began, 1), "root": root, "control_authority": "NONE"}
    reasons = [f"archived {len(written)} day(s) ({total_bytes / 1024 ** 2:.1f} MB compressed); "
               f"{len(existing)} already archived; {len(capped)} left for the next run"]
    if gone:
        reasons.append(f"{len(gone)} day(s) older than Home Assistant keeps ({gone[0]} .. {gone[-1]}) could not be read")
    reasons.append("read-only toward Home Assistant; files only under the archive folder")
    if not written and not existing and todo and not capped:
        return Result(FAILED, reasons, facts)
    return Result(DONE, reasons, facts)


def run(ha, m, root: str | None = None) -> Result:
    """The job (``maint/requests``): ``from``..``to`` UTC days."""
    return run_range(ha, root or ARCHIVE_ROOT, str(m.archive_from), str(m.archive_to), int(m.archive_chunk),
                     int(m.archive_minutes))


def nightly(ha, root: str | None = None, minutes: int = 45, chunk: int = 25) -> Result:
    """Automatic catch-up: every missing day Core still holds, up to yesterday, oldest first."""
    return run_range(ha, root or ARCHIVE_ROOT, oldest_available(), yesterday(), chunk, minutes)
