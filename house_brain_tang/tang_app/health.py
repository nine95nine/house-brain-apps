"""House Brain Tang's own house_brain_app_health.v1 block.

OK only when, at the moment of asking, Tang itself answered a valid advertisement on 127.0.0.1:7500.
Anything else is FAILED with one fixed reason code. The block carries no key, thumbprint or body.
"""
import math
import resource
import threading
import time

from . import __version__
from . import probe
from .app_health import SCHEMA_VERSION, validate

APP = 'house_brain_tang'
INTERVAL_S = 60
CACHE_S = 2


def peak_rss_mb():
    try:
        return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except (OSError, ValueError):
        return 0


class Health:
    """Each /health read asks Tang afresh (at most once every CACHE_S seconds); nothing is stored but times."""

    def __init__(self, check=probe.check, clock=time.time, monotonic=time.monotonic):
        self.check, self.clock, self.monotonic = check, clock, monotonic
        self.lock = threading.Lock()
        self.last_good_at = None
        self.cached = None
        self.cached_at = None

    def _probe(self):
        started = self.monotonic()
        try:
            ok, reason = self.check()
        except Exception:  # a broken probe is a failure, never OK
            ok, reason = False, probe.REASON_UNREACHABLE
        if ok is not True:
            ok = False
            reason = reason if reason in (probe.REASON_UNREACHABLE, probe.REASON_INVALID) else probe.REASON_UNREACHABLE
        return ok, reason, max(0., self.monotonic() - started)

    def __call__(self):
        with self.lock:
            now_m = self.monotonic()
            if self.cached is None or self.cached_at is None or not 0 <= now_m - self.cached_at < CACHE_S:
                self.cached, self.cached_at = self._probe(), now_m
            ok, reason, seconds = self.cached
            now = self.clock()
            at = int(now) if isinstance(now, (int, float)) and math.isfinite(now) and now >= 1 else 1
            if ok:
                self.last_good_at = at
            return build(at, ok, reason, seconds, self.last_good_at)


def build(at, ok, reason, seconds, last_good_at):
    good = 1 if ok else 0
    last_good = last_good_at if type(last_good_at) is int and 1 <= last_good_at <= at else None
    doc = {'schema_version': SCHEMA_VERSION, 'app': APP, 'version': __version__, 'at': at,
           'health': 'OK' if ok else 'FAILED', 'activity': 'RUNNING', 'reasons': [] if ok else [reason],
           'cycle': {'interval_s': INTERVAL_S, 'last_at': at, 'last_good_at': last_good,
                     'last_seconds': round(seconds, 3) if isinstance(seconds, float) and 0 <= seconds <= 3600 else None},
           'data': {'status': 'GOOD' if ok else 'DEGRADED', 'inputs': 1, 'good': good, 'idle': 0, 'bad': 1 - good},
           'resources': {'peak_rss_mb': peak_rss_mb(), 'db_bytes': None, 'address_cap_mb': None},
           'physical_actions': False, 'control_authority': 'NONE'}
    if validate(doc):
        doc.update(health='FAILED', reasons=['HEALTH_SELF_CHECK_FAILED'],
                   cycle={'interval_s': INTERVAL_S, 'last_at': None, 'last_good_at': None, 'last_seconds': None},
                   data={'status': 'UNKNOWN', 'inputs': 0, 'good': 0, 'idle': 0, 'bad': 0},
                   resources={'peak_rss_mb': 0, 'db_bytes': None, 'address_cap_mb': None})
    return doc
