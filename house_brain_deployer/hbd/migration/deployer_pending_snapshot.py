"""Bounded private working history reader; optional pending txn stays non-authority."""
import os
import stat

from .. import history


def read_working_snapshot(root):
    """Same fixed file inventory; retain non-null txn for recovery admission only.

    Working Journal's normal writes create0644 files inside a private0700 root.
    Group/world writable, foreign-owned, linked or drifting files refuse.
    """
    parent = None
    handles = []
    try:
        if os.path.realpath(root) != root:
            raise history.HistoryError('UNSAFE_ROOT')
        parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        held = os.fstat(parent)
        if held.st_uid != os.geteuid() or stat.S_IMODE(held.st_mode) != 0o700:
            raise history.HistoryError('UNSAFE_ROOT')
        signature = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_uid, s.st_nlink,  # noqa: E731 - local helper kept as written
                                s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        def observed(name):
            try:
                return signature(os.stat(name, dir_fd=parent, follow_symlinks=False))
            except FileNotFoundError:
                return None
        before = {name: observed(name) for name in history.FILES}
        result, total = {}, 0
        for name in history.FILES:
            if before[name] is None:
                continue
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            handles.append(fd)
            st = os.fstat(fd)
            if (signature(st) != before[name] or not stat.S_ISREG(st.st_mode) or st.st_nlink != 1
                    or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) not in (0o600, 0o644)):
                raise history.HistoryError('UNSAFE_WORKING_FILE')
            total += st.st_size
            if total > history.MAX_BYTES:
                raise history.HistoryError('SIZE_LIMIT')
            raw, left = bytearray(), st.st_size + 1
            while left:
                chunk = os.read(fd, min(left, 65536))
                if not chunk:
                    break
                raw.extend(chunk)
                left -= len(chunk)
            if len(raw) != st.st_size or signature(os.fstat(fd)) != before[name]:
                raise history.HistoryError('SNAPSHOT_DRIFT')
            result[name] = bytes(raw)
        if any(observed(name) != value for name, value in before.items()):
            raise history.HistoryError('SNAPSHOT_DRIFT')
        visible = os.stat(root, follow_symlinks=False)
        if signature(held) != signature(os.fstat(parent)) or signature(held) != signature(visible):
            raise history.HistoryError('ROOT_DRIFT')
        regular = {k: v for k, v in result.items() if k != 'txn.json'}
        history._decode(regular)
        if 'txn.json' in result:
            txn = history._json(result['txn.json'])
            if txn is not None and not isinstance(txn, dict):
                raise history.HistoryError('TRANSACTION_SHAPE')
        return result
    except history.HistoryError:
        raise
    except (OSError, ValueError, TypeError):
        raise history.HistoryError('WORKING_UNAVAILABLE') from None
    finally:
        for fd in handles:
            os.close(fd)
        if parent is not None:
            os.close(parent)
