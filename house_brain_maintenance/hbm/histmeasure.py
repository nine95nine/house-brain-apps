"""0.9.27 HISTORY_MEASURE: how big is one day of history, really? (history archive proposal R1, step 1; owner pop-ups
2026-10-24 "Yes, measure first" and "Yes, 0.9.27 next"; request #454 6385485785).

A read-only, no-approval job. It reads one complete UTC day through Core's own WebSocket APIs
(``history/history_during_period`` with attributes, ``recorder/statistics_during_period`` for the 5-minute and hourly
statistics) chunk by chunk, counts rows and bytes, feeds each chunk through an ``xz`` compressor and lets it go.
It also measures the proposal's archive shape (per row: seconds since midnight, state, changed attributes only), so
the owner gets the real archive size, not an estimate.

Nothing is kept: no file, no document, no value in the result. The result carries counts, byte sizes and the ids of
the largest entities. A chunk Core cannot answer (too large) is split down to one entity; an entity that still cannot
be read is listed as unread. A time cap ends the read early with a partial, clearly marked result.
``CONTROL_AUTHORITY=NONE``.
"""
from __future__ import annotations

import calendar
import json
import lzma
import time

from . import issues as I
from .ha import HISTORY_CHUNK, STATISTIC_PERIODS
from .jobs import DONE, FAILED, Result

PRESET = 6                          # xz level the archive would use
TOP_N = 10
MAX_UNREAD_NAMED = 10
WS_MAX_BYTES = 64 * 1024 * 1024     # one chunk answer; bigger chunks are split
_monotonic = time.monotonic         # tests replace it


def day_window(day: str) -> tuple[str, str, float]:
    """``YYYY-MM-DD`` (UTC) -> (start iso, end iso, start epoch)."""
    start = calendar.timegm(time.strptime(day, "%Y-%m-%d"))
    fmt = "%Y-%m-%dT%H:%M:%S+00:00"
    return time.strftime(fmt, time.gmtime(start)), time.strftime(fmt, time.gmtime(start + 86400)), float(start)


class Stream:
    """Counts raw bytes and the xz size of everything fed to it; keeps none of it."""

    def __init__(self) -> None:
        self._z = lzma.LZMACompressor(preset=PRESET)
        self.raw = 0
        self.xz = 0

    def feed(self, data: bytes) -> None:
        self.raw += len(data)
        self.xz += len(self._z.compress(data))

    def finish(self) -> None:
        if self._z is not None:
            self.xz += len(self._z.flush())
            self._z = None


def archive_rows(rows: list, start_epoch: float) -> list:
    """The proposal's archive shape: [seconds since midnight, state, changed attributes only] per row."""
    out, prev = [], None
    for r in rows:
        if not isinstance(r, dict):
            continue
        attrs = r.get("a") if isinstance(r.get("a"), dict) else {}
        if prev is None:
            delta = attrs
        else:
            delta = {k: v for k, v in attrs.items() if prev.get(k) != v}
            delta.update({k: None for k in prev if k not in attrs})
        prev = attrs
        when = r.get("lu", r.get("lc"))
        out.append([round(float(when) - start_epoch, 3) if isinstance(when, (int, float)) else None,
                    r.get("s"), delta])
    return out


def _dumps(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), default=str).encode()


def fmt_bytes(n: int) -> str:
    for unit, size in (("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)):
        if n >= size:
            return f"{n / size:.1f} {unit}"
    return f"{n} B"


def run(ha, m) -> Result:
    """The job: one UTC day, ``m.measure_chunk`` entities per read, at most ``m.measure_minutes`` in all."""
    day, chunk, minutes = str(m.measure_day), int(m.measure_chunk), int(m.measure_minutes)
    start_iso, end_iso, start_epoch = day_window(day)
    deadline = _monotonic() + minutes * 60
    began = _monotonic()
    ids = ha.entity_ids()
    raw, arch = Stream(), Stream()
    per_entity: dict[str, list[int]] = {}
    unread: list[str] = []
    stopped_early = False
    queue = [ids[i:i + chunk] for i in range(0, len(ids), chunk)]
    sock = ha.ws(max_size=WS_MAX_BYTES)
    try:
        while queue:
            if _monotonic() >= deadline:
                stopped_early = True
                break
            part_ids = queue.pop(0)
            try:
                part = ha.history_full(sock, start_iso, end_iso, part_ids)
            except Exception:  # noqa: BLE001 - Core said no or the answer was too big: split, on a fresh socket
                try:
                    sock.close()
                except Exception:  # noqa: BLE001
                    pass
                sock = ha.ws(max_size=WS_MAX_BYTES)
                if len(part_ids) > 1:
                    mid = len(part_ids) // 2
                    queue[0:0] = [part_ids[:mid], part_ids[mid:]]
                else:
                    unread.append(part_ids[0])
                continue
            for eid in part_ids:
                rows = part.get(eid) if isinstance(part, dict) else None
                rows = rows if isinstance(rows, list) else []
                data = _dumps(rows)
                raw.feed(data)
                arch.feed(_dumps(archive_rows(rows, start_epoch)))
                per_entity[eid] = [len(rows), len(data)]
            del part
        raw.finish()
        arch.finish()
        stats: dict = {"ids": 0, "rows": {p: 0 for p in STATISTIC_PERIODS}, "raw_json_bytes": 0, "xz_bytes": 0,
                       "read": False}
        if not stopped_early:
            sids = ha.statistic_ids(sock)
            stats["ids"] = len(sids)
            st = Stream()
            done = True
            for period in STATISTIC_PERIODS:
                for i in range(0, len(sids), HISTORY_CHUNK):
                    if _monotonic() >= deadline:
                        done = False
                        break
                    part = ha.statistics_full(sock, start_iso, end_iso, sids[i:i + HISTORY_CHUNK], period)
                    for sid in sids[i:i + HISTORY_CHUNK]:
                        rows = part.get(sid) if isinstance(part, dict) else None
                        rows = rows if isinstance(rows, list) else []
                        stats["rows"][period] += len(rows)
                        st.feed(_dumps(rows))
                    del part
                if not done:
                    break
            st.finish()
            stats.update(raw_json_bytes=st.raw, xz_bytes=st.xz, read=done)
    finally:
        try:
            sock.close()
        except Exception:  # noqa: BLE001
            pass
    rows_total = sum(v[0] for v in per_entity.values())
    with_rows = sum(1 for v in per_entity.values() if v[0])
    largest = sorted(per_entity.items(), key=lambda kv: (-kv[1][1], kv[0]))[:TOP_N]
    seconds = round(_monotonic() - began, 1)
    facts = {"day_utc": day, "window_utc": [start_iso, end_iso], "entities_known": len(ids),
             "entities_measured": len(per_entity), "entities_with_rows": with_rows, "rows": rows_total,
             "raw_json_bytes": raw.raw, "xz_bytes": raw.xz, "archive_rows_bytes": arch.raw,
             "archive_xz_bytes": arch.xz, "statistics": stats, "chunk": chunk, "seconds": seconds,
             "stopped_at_cap": stopped_early, "unread": len(unread),
             "unread_ids": [I.scrub(e, 200) for e in unread[:MAX_UNREAD_NAMED]],
             "largest": [[I.scrub(e, 200), n, b] for e, (n, b) in largest]}
    if not per_entity:
        return Result(FAILED, ["no entity could be read for that day; nothing was changed"], facts)
    ratio = (raw.raw / raw.xz) if raw.xz else 0.0
    reasons = [f"{day} (UTC): {rows_total:,} history rows from {with_rows:,} of {len(per_entity):,} entities "
               f"({len(ids):,} known); raw JSON {fmt_bytes(raw.raw)}, xz {fmt_bytes(raw.xz)} ({ratio:.0f}x); "
               f"archive rows {fmt_bytes(arch.raw)}, xz {fmt_bytes(arch.xz)}"]
    if stats["read"]:
        reasons.append(f"statistics: {stats['ids']:,} ids, {stats['rows']['5minute']:,} 5-minute and "
                       f"{stats['rows']['hour']:,} hourly rows; raw JSON {fmt_bytes(stats['raw_json_bytes'])}, "
                       f"xz {fmt_bytes(stats['xz_bytes'])}")
    else:
        reasons.append("statistics: not read (the time cap came first)")
    if stopped_early:
        reasons.append(f"stopped at the {minutes}-minute cap: {len(ids) - len(per_entity):,} entities not read; "
                       "file it again with a smaller day or a higher cap")
    if unread:
        reasons.append(f"{len(unread)} entities could not be read even one at a time (named in the facts)")
    reasons.append("nothing was kept, written or changed")
    return Result(DONE, reasons, facts)
