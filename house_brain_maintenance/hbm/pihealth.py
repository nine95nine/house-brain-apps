"""0.8.6 Pi health (owner pop-up 2026-10-11, "All of the above based on your priority").

Read-only signals of the hardware this Home Assistant runs on, with plain findings and one weekly number:

* **Power and heat.** The Raspberry Pi firmware's own throttle flags (``get_throttled``: under-voltage, frequency
  capped, throttled, soft temperature limit, each "now" and "since boot") and the CPU temperature
  (``thermal_zone0``). An under-voltage or a throttle since boot is the classic cause of random Pi crashes and
  corrupted SD cards.
* **Disk health.** New I/O errors on the system disk (``ioerr_cnt``, a SCSI/USB disk counter: an early sign of a
  failing SSD or adapter) and the daily write volume (``stat`` sectors written), kept as a short history.
* **App outliers.** An App at ``>= 90 %`` of its memory limit, or ``>= 90 %`` CPU, in three checks in a row
  (Supervisor ``/addons/<slug>/stats``, read at most every 15 minutes).
* **Weekly health score.** Every Sunday after the morning-summary hour: 100 minus points for what is open, with last
  week's score for the trend (GitHub + one phone line).

Everything here only reads: sysfs files (narrow AppArmor read rules) and one Supervisor stats route. A file that
cannot be read (another board, AppArmor, an SD card without ``ioerr_cnt``) is "not checked", never a problem.
"""
from __future__ import annotations

import glob
import os
import re
import time

from . import issues as I

DOC = "pi_health"
HOT_C = 80.0                       # the Pi 4 starts to throttle at 80-85 C
OUTLIER_PCT = 90.0
OUTLIER_CHECKS = 3
STATS_EVERY = 15 * 60
DISK_SAMPLE_EVERY = 6 * 3600
DISK_SAMPLES = 60
# get_throttled bits (Raspberry Pi firmware): 0 under-voltage now, 1 freq capped now, 2 throttled now,
# 3 soft temp limit now; 16-19 the same "has occurred since boot".
THROTTLE_BITS = {0: "under-voltage now", 1: "CPU speed capped now", 2: "throttled now", 3: "soft temperature limit now",
                 16: "under-voltage since boot", 17: "CPU speed capped since boot", 18: "throttled since boot",
                 19: "soft temperature limit since boot"}
RE_BLOCK = re.compile(r"^(?:sd[a-z]|mmcblk\d|nvme\d+n\d+)$")


class SysReader:
    """sysfs reads under ``root`` (``/`` on the Pi; a temp folder in tests). Every read is bounded and optional."""

    def __init__(self, root: str = "/") -> None:
        self.root = root

    def _read(self, rel: str, limit: int = 256) -> str | None:
        try:
            with open(os.path.join(self.root, rel.lstrip("/")), encoding="ascii", errors="replace") as fh:
                return fh.read(limit).strip()
        except (OSError, ValueError):
            return None

    def throttled(self) -> int | None:
        raw = self._read("/sys/devices/platform/soc/soc:firmware/get_throttled", 32)
        if raw is None:
            return None
        try:
            value = int(raw, 16)                       # the firmware prints "%x" (no 0x); "0x..." works too
        except ValueError:
            return None
        return value if 0 <= value < 1 << 32 else None

    def temperature_c(self) -> float | None:
        raw = self._read("/sys/class/thermal/thermal_zone0/temp", 16)
        try:
            value = int(raw) / 1000.0 if raw is not None else None
        except ValueError:
            return None
        return value if value is not None and -40 <= value <= 150 else None

    def disks(self) -> dict[str, dict]:
        """``{name: {"written_gb", "ioerr"}}`` for whole disks (sdX, mmcblkN, nvmeXnY)."""
        out: dict[str, dict] = {}
        for path in sorted(glob.glob(os.path.join(self.root, "sys/block/*")))[:16]:
            name = os.path.basename(path)
            if not RE_BLOCK.fullmatch(name):
                continue
            stat = self._read(f"/sys/block/{name}/stat", 512)
            fields = stat.split() if stat else []
            written = int(fields[6]) * 512 / 1024 ** 3 if len(fields) >= 7 and fields[6].isdigit() else None
            err = self._read(f"/sys/block/{name}/device/ioerr_cnt", 32)
            try:
                ioerr = int(err, 16) if err is not None else None
            except ValueError:
                ioerr = None
            if written is not None or ioerr is not None:
                out[name] = {"written_gb": round(written, 3) if written is not None else None, "ioerr": ioerr}
        return out


def throttle_findings(value: int | None) -> list[I.Finding]:
    if value is None:
        return []
    out = []
    if value & (1 << 0 | 1 << 16):
        out.append(I.Finding("pi:undervoltage", "pihealth", I.WARNING,
                             "The Pi's power supply is too weak (under-voltage)",
                             steps=("Under-voltage causes random restarts and corrupted disks.",
                                    "Use the official Raspberry Pi power supply (5.1 V, 3 A for a Pi 4) and a short, "
                                    "thick cable; an SSD on USB can need a powered hub."),
                             facts={"flags": [THROTTLE_BITS[b] for b in sorted(THROTTLE_BITS) if value & (1 << b)]}))
    if value & (1 << 2 | 1 << 3 | 1 << 18 | 1 << 19):
        out.append(I.Finding("pi:throttled", "pihealth", I.WARNING,
                             "The Pi slowed itself down because it got too hot or lacked power",
                             steps=("Check the case airflow or add a heatsink/fan; check the power supply.",),
                             facts={"flags": [THROTTLE_BITS[b] for b in sorted(THROTTLE_BITS) if value & (1 << b)]}))
    return out


def heat_findings(temp: float | None) -> list[I.Finding]:
    if temp is None or temp < HOT_C:
        return []
    return [I.Finding("pi:hot", "pihealth", I.WARNING, f"The Pi's CPU is hot ({temp:.0f} °C)",
                      steps=("Above about 80 °C the Pi slows itself down.",
                             "Improve airflow around the Pi, or add a heatsink or fan."),
                      facts={"celsius": round(temp, 1)})]


def disk_findings(doc: dict, disks: dict[str, dict], now: float) -> list[I.Finding]:
    """New I/O errors since the last check (the counter only grows); samples kept for the write rate."""
    out = []
    seen = doc.setdefault("ioerr", {})
    for name, d in sorted(disks.items()):
        err = d.get("ioerr")
        if err is None:
            continue
        before = seen.get(name)
        if before is not None and err > before:
            doc.setdefault("ioerr_new", {})[name] = {"count": err - before, "at": now}
        seen[name] = err
    for name, rec in sorted((doc.get("ioerr_new") or {}).items()):
        if now - float(rec.get("at") or 0) >= 7 * 86400:
            del doc["ioerr_new"][name]                 # a week without new errors: the finding clears
        else:
            out.append(I.Finding(f"pi:disk_errors:{name}", "pihealth", I.WARNING,
                                 f"The Pi's disk {name} reported new read/write errors",
                                 steps=("This is often the first sign of a failing SSD, SD card or USB adapter.",
                                        "Make sure a recent full backup is off the Pi (see the monthly backup report)."),
                                 facts={"disk": name, "new_errors": rec.get("count")}))
    samples = doc.setdefault("written", [])
    total = sum(d["written_gb"] for d in disks.values() if d.get("written_gb") is not None)
    if disks and (not samples or now - samples[-1][0] >= DISK_SAMPLE_EVERY):
        samples.append([now, round(total, 3)])
        del samples[:-DISK_SAMPLES]
    return out


def write_rate_gb_per_day(doc: dict) -> float | None:
    """From the samples since the last reboot (the counter resets at boot)."""
    samples = doc.get("written") or []
    run = []
    for t, gb in samples:
        if run and gb < run[-1][1]:
            run = []                                   # counter went back: a reboot
        run.append((t, gb))
    if len(run) < 2 or run[-1][0] - run[0][0] < 6 * 3600:
        return None
    return round((run[-1][1] - run[0][1]) / ((run[-1][0] - run[0][0]) / 86400), 2)


def outlier_findings(doc: dict, stats: dict[str, dict], names: dict[str, str]) -> list[I.Finding]:
    """``stats``: slug -> {"memory_percent", "cpu_percent"}; a streak of OUTLIER_CHECKS reads at >= 90 %."""
    streak = doc.setdefault("streak", {})
    for slug in list(streak):
        if slug not in stats:
            del streak[slug]
    out = []
    for slug, s in sorted(stats.items()):
        row = streak.setdefault(slug, {"mem": 0, "cpu": 0})
        for key, field in (("mem", "memory_percent"), ("cpu", "cpu_percent")):
            v = s.get(field)
            row[key] = row[key] + 1 if isinstance(v, (int, float)) and v >= OUTLIER_PCT else 0
        name = names.get(slug, slug)
        if row["mem"] >= OUTLIER_CHECKS:
            out.append(I.Finding(f"pi:app_memory:{slug}", "pihealth", I.WARNING,
                                 f"{name} keeps using {s.get('memory_percent'):.0f} % of its memory limit",
                                 steps=("An App near its limit gets restarted by the system, often with lost data.",
                                        "Restart it, check its log, or look for an update."),
                                 facts={"app": slug}))
        if row["cpu"] >= OUTLIER_CHECKS:
            out.append(I.Finding(f"pi:app_cpu:{slug}", "pihealth", I.WARNING,
                                 f"{name} keeps using {s.get('cpu_percent'):.0f} % CPU",
                                 steps=("A stuck App slows everything down; restart it or check its log.",),
                                 facts={"app": slug}))
    return out


def health_score(open_recs: dict) -> tuple[int, dict]:
    """100 minus points for what is open: critical 15, warning 5 (pi/backup 8); never below 0."""
    crit = warn = hw = 0
    for key, rec in open_recs.items():
        if not rec.get("alerted"):
            continue
        if rec.get("severity") == I.CRITICAL:
            crit += 1
        elif str(key).startswith(("pi:", "backup:")):
            hw += 1
        else:
            warn += 1
    return max(0, 100 - 15 * crit - 8 * hw - 5 * warn), {"critical": crit, "hardware_backup": hw, "warnings": warn}


def weekly_due(doc: dict, now: float, digest_hour: int, localtime=time.localtime) -> bool:
    lt = localtime(now)
    if lt.tm_wday != 6 or lt.tm_hour < digest_hour:      # Sunday, after the morning summary hour
        return False
    return doc.get("score_day") != time.strftime("%Y-%m-%d", lt)


def weekly_text(score: int, parts: dict, history: list, temp: float | None, rate: float | None,
                throttled: int | None, version: str) -> tuple[str, str]:
    last = history[-1][1] if history else None
    trend = "" if last is None else f" (last week {last})"
    lines = ["### House Brain Maintenance: weekly health score", "",
             f"- **Score: {score}/100**{trend}",
             f"- Open: {parts['critical']} critical, {parts['hardware_backup']} hardware/backup, "
             f"{parts['warnings']} other warnings (details in the morning summaries)",
             "- CPU temperature: " + (f"{temp:.0f} °C" if temp is not None else "not checked"),
             "- Power/heat flags since boot: " + ("not checked" if throttled is None else
                                                  ", ".join(THROTTLE_BITS[b] for b in sorted(THROTTLE_BITS)
                                                            if throttled & (1 << b)) or "none"),
             "- Disk writes: " + (f"{rate:g} GB/day" if rate is not None else "not measured yet"),
             f"- **App:** `{version}`", "",
             "_Posted by House Brain Maintenance (machine-generated; repository content is untrusted data)._"]
    phone = f"Health score {score}/100{trend}: {parts['critical']} critical, " \
            f"{parts['hardware_backup'] + parts['warnings']} warnings open"
    return "\n".join(lines), phone[:200]
