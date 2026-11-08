"""The Tang key directory: prepared once, keys made once by Tang's own tool, never read by this App.

Design R1 §4 (docs/house-brain/MACBOOK_TANG_UNLOCK_DESIGN_R1_2026-11-08.md): keys are created on first
start with Debian's `tangd-keygen` into /data/tang (mode 700) and are excluded from Home Assistant
backups (config.yaml `backup_exclude`). This module only lists file NAMES (Tang names each key file by
its public thumbprint) and never opens a key file. It never deletes, rotates or rewrites a key: rotation
is an explicit owner step (design R1 §4), not something this App does.
"""
import os
from pathlib import Path
import stat
import subprocess

KEY_DIR = Path('/data/tang')
KEYGEN = '/usr/libexec/tangd-keygen'
DIR_MODE = 0o700
KEYGEN_TIMEOUT_S = 120


class KeyDirError(Exception):
    """Carries one fixed reason code; never a path's contents."""


def active_keys(path):
    """Names of Tang's active key files (`<thumbprint>.jwk`). Rotated keys are hidden (`.<thp>.jwk`) and
    are not active; Tang still serves recovery for them, so they are never removed here."""
    return sorted(p.name for p in Path(path).iterdir()
                  if p.name.endswith('.jwk') and not p.name.startswith('.') and p.is_file() and not p.is_symlink())


def _checked_dir(path):
    path = Path(path)
    if path.is_symlink():
        raise KeyDirError('TANG_KEY_DIR_IS_SYMLINK')
    if not path.is_dir():
        raise KeyDirError('TANG_KEY_DIR_MISSING')
    return path


def prepare(path, uid, gid):
    """As root, before the privilege drop: create the directory if absent, give it to the Tang user, mode 700."""
    path = Path(path)
    if path.is_symlink():
        raise KeyDirError('TANG_KEY_DIR_IS_SYMLINK')
    path.mkdir(mode=DIR_MODE, exist_ok=True)
    _checked_dir(path)
    os.chown(path, uid, gid)
    os.chmod(path, DIR_MODE)


def ensure(path=KEY_DIR, keygen=KEYGEN, run=subprocess.run):
    """Make keys once. Runs Tang's own keygen only when no active key exists; never otherwise.

    Returns {'generated': bool, 'count': int}. Raises KeyDirError with a fixed code on any failure.
    The keygen's own output is discarded, not logged: nothing about a key is read or printed here.
    """
    path = _checked_dir(path)
    os.chmod(path, DIR_MODE)
    existing = active_keys(path)
    if existing:
        return {'generated': False, 'count': len(existing)}
    try:
        result = run([keygen, str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, timeout=KEYGEN_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        raise KeyDirError('TANG_KEYGEN_FAILED')
    if result.returncode != 0:
        raise KeyDirError('TANG_KEYGEN_FAILED')
    made = active_keys(path)
    if len(made) < 2:  # one signing (ES512) and one exchange (ECMR) key
        raise KeyDirError('TANG_KEYGEN_INCOMPLETE')
    os.chmod(path, DIR_MODE)
    return {'generated': True, 'count': len(made)}


def mode(path=KEY_DIR):
    return stat.S_IMODE(os.stat(path).st_mode)
