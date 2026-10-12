"""0.3.16 read-only native self-report, logged once at startup (owner pop-up 2026-10-12).

It supplies the native facts the shared-writer layout proposal needs
(docs/architecture/DEPLOYER_SHARED_WRITER_LAYOUT_R1.md): effective user/group,
umask, capabilities, AppArmor label, Python/arch, and mode/owner/inode/mount facts
for the App's data, packages and the (not yet mapped) share paths.

Strictly local reads: stat and /proc/self only. No write, network call, chmod,
directory creation or lock. Every probe fails closed to a short reason, and the
report as a whole never raises, so it cannot stop the App from starting. Each
line stays under the Maintenance log-window line cap (300 characters) and uses
the plain keyword ``HBD-NATIVE`` for read-back through ``APP_LOG_WINDOW``.
"""
from __future__ import annotations

import errno
import os
import platform
import stat
import sys

KEYWORD = "HBD-NATIVE"
MAX_LINE = 280
MAX_PROC_BYTES = 256 * 1024
PATHS = ("/data", "/homeassistant", "/homeassistant/packages", "/share",
         "/share/house_brain_writer_v1", "/tmp")  # noqa: S108 - only stat()ed, never written
STATUS_FIELDS = ("Umask", "CapEff", "CapBnd", "NoNewPrivs", "Seccomp")
SCHEMA = "hbd.native_selfreport.v1"


def _read(path: str) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        raw = os.read(fd, MAX_PROC_BYTES)
    finally:
        os.close(fd)
    return raw.decode("utf-8", "replace")


def _reason(err: OSError) -> str:
    return errno.errorcode.get(err.errno or 0, "OSERROR")


def _clean(text: str, limit: int = 80) -> str:
    """Printable, space-free and bounded: a value can never forge another field."""
    out = "".join(c if 33 <= ord(c) <= 126 and c not in "\"'" else "_" for c in str(text))
    return out[:limit] or "-"


def _kind(mode: int) -> str:
    for test, name in ((stat.S_ISDIR, "dir"), (stat.S_ISREG, "file"), (stat.S_ISLNK, "symlink")):
        if test(mode):
            return name
    return "other"


def _mounts() -> list[tuple[str, str, str, str, str]]:
    """(mount point, major:minor, root, fstype, rw/ro) from /proc/self/mountinfo."""
    rows = []
    for line in _read("/proc/self/mountinfo").splitlines():
        left, sep, right = line.partition(" - ")
        fields, tail = left.split(), right.split()
        if not sep or len(fields) < 6 or not tail:
            continue
        flags = "ro" if "ro" in fields[5].split(",") else "rw"
        rows.append((fields[4], fields[2], fields[3], tail[0], flags))
    return rows


def _mount_for(path: str, rows) -> tuple[str, str, str, str, str] | None:
    best = None
    for row in rows:
        point = row[0]
        if (path == point or path.startswith(point.rstrip("/") + "/")) and (
                best is None or len(point) > len(best[0])):
            best = row
    return best


def _path_line(path: str, rows) -> str:
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError as err:
        return f"{KEYWORD} path={path} st={_reason(err)}"
    line = (f"{KEYWORD} path={path} st=ok type={_kind(st.st_mode)} mode={stat.S_IMODE(st.st_mode):04o} "
            f"uid={st.st_uid} gid={st.st_gid} dev={os.major(st.st_dev)}.{os.minor(st.st_dev)} "
            f"ino={st.st_ino} nlink={st.st_nlink}")
    mount = _mount_for(path, rows) if rows is not None else None
    if mount is not None:
        point, majmin, root, fstype, flags = mount
        line += (f" mnt={_clean(point, 40)} mdev={_clean(majmin, 12)} fs={_clean(fstype, 16)} {flags}"
                 f" root={_clean(root, 70)}")
    return line


def lines() -> list[str]:
    """The report lines. Never raises; each line is bounded to MAX_LINE characters."""
    out: list[str] = []
    try:
        out.append(f"{KEYWORD} schema={SCHEMA} python={_clean(platform.python_version(), 16)} "
                   f"impl={_clean(sys.implementation.name, 16)} arch={_clean(platform.machine(), 16)} "
                   f"euid={os.geteuid()} egid={os.getegid()} uid={os.getuid()} gid={os.getgid()} "
                   f"groups={_clean(','.join(str(g) for g in os.getgroups()), 60)} pid={os.getpid()}")
        try:
            status = {}
            for row in _read("/proc/self/status").splitlines():
                key, _, value = row.partition(":")
                if key in STATUS_FIELDS:
                    status[key] = _clean(value.strip(), 20)
            out.append(f"{KEYWORD} " + " ".join(f"{k.lower()}={status.get(k, '-')}" for k in STATUS_FIELDS))
        except OSError as err:
            out.append(f"{KEYWORD} status={_reason(err)}")
        label = None
        for source in ("/proc/self/attr/apparmor/current", "/proc/self/attr/current"):
            try:
                label = _clean(_read(source).strip("\x00\n ").replace(" ", "_"), 80)
                break
            except OSError as err:
                label = _reason(err)
        out.append(f"{KEYWORD} apparmor={label}")
        try:
            rows = _mounts()
        except OSError as err:
            rows = None
            out.append(f"{KEYWORD} mountinfo={_reason(err)}")
        for path in PATHS:
            out.append(_path_line(path, rows))
        out.append(f"{KEYWORD} end lines={len(out) + 1} writes=0 network=0")
    except Exception as err:  # noqa: BLE001 - a report must never stop the App from starting
        out.append(f"{KEYWORD} unavailable={_clean(type(err).__name__, 40)}")
    return [line[:MAX_LINE] for line in out]


def log_report(log) -> None:
    for line in lines():
        log.info("%s", line)
