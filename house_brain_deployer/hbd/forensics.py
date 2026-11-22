"""0.3.49: how the last run ended, and where a crash or a hang leaves its trace.

Everything here is best effort and never raises: the Deployer must not fail because it could not write a note about
failing. Files live in the App's own ``/data`` only and hold no secrets: a verdict word, an exception class name and
an upper-case code, times, and Python stack frames (file, function, line; no variable values).

* ``last_run.json``: written ``running`` at start, rewritten at a clean stop or by the outer handler on a fatal error.
  A start that still finds ``running`` knows the last run was killed or lost power (``killed_or_power_loss``).
* ``crash_trace.txt``: ``faulthandler`` output for fatal signals (segfault, abort).
* ``hang_trace.txt``: all thread stacks, written by ``faulthandler.dump_traceback_later`` when nothing has ticked for
  ``HANG_SECONDS`` (the loop and every owner wait tick, so a legitimate long wait never fires it).
  It dumps and goes on; it never kills or restarts anything.
"""
from __future__ import annotations

import faulthandler
import json
import os
import re
import time
from typing import Any

LAST_RUN = "last_run.json"
CRASH_TRACE = "crash_trace.txt"
HANG_TRACE = "hang_trace.txt"
HANG_SECONDS = 900                 # 15 minutes without a tick
UNCLEAN_PUSH_EVERY = 6 * 3600      # at most one "last run ended badly" push per 6 hours
RE_WORD = re.compile(r"[A-Za-z0-9_]{1,64}")
CLEAN = ("clean_stop",)


def _word(value: object, default: str = "UNKNOWN") -> str:
    text = str(value)
    return text if RE_WORD.fullmatch(text) else default


def crashed_verdict(err: BaseException) -> str:
    """``crashed:<ExceptionClass>`` plus an upper-case code when the message starts with one (never the message)."""
    code = str(err).split(":", 1)[0].strip()
    code = code if re.fullmatch(r"[A-Z][A-Z0-9_]{2,63}", code) else ""
    return f"crashed:{_word(type(err).__name__)}" + (f":{code}" if code else "")


class Forensics:
    def __init__(self, data_dir: str, *, hang_seconds: float = HANG_SECONDS) -> None:
        self.dir = data_dir
        self.hang_seconds = hang_seconds
        self.path = os.path.join(data_dir, LAST_RUN)
        self.previous: dict[str, Any] = {"verdict": "first_start"}
        self._crash_fh: Any = None
        self._hang_fh: Any = None
        self._hang_armed = False
        self._last_push = 0.0

    # -- the verdict file ---------------------------------------------------------------------------------
    def _read(self) -> dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                doc = json.load(fh)
            return doc if isinstance(doc, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, doc: dict[str, Any]) -> None:
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
        except OSError:
            pass   # no note is better than a failed App

    def begin(self, version: str, run_id: str = "") -> dict[str, Any]:
        """Read how the previous run ended, then mark this run ``running``. Returns the previous verdict."""
        old = self._read()
        if not old:
            self.previous = {"verdict": "first_start"}
        elif old.get("state") == "running":
            self.previous = {"verdict": "killed_or_power_loss", "version": _word(old.get("version", ""), ""),
                             "started_at": old.get("started_at")}
        else:
            self.previous = {"verdict": _verdict_text(old.get("verdict")), "version": _word(old.get("version", ""), ""),
                             "ended_at": old.get("ended_at")}
        self._last_push = float(old.get("last_unclean_push_at") or 0.0) if isinstance(old, dict) else 0.0
        self._write({"state": "running", "version": _word(version, ""), "run_id": _word(run_id, ""),
                     "started_at": int(time.time()), "last_unclean_push_at": self._last_push})
        return self.previous

    def end(self, verdict: str) -> None:
        doc = self._read()
        doc.update({"state": "ended", "verdict": _verdict_text(verdict), "ended_at": int(time.time())})
        self._write(doc)

    def previous_was_clean(self) -> bool:
        return self.previous.get("verdict") in CLEAN + ("first_start",) or str(
            self.previous.get("verdict", "")).startswith(("held:", "refused:"))

    def should_push_unclean(self, now: float | None = None) -> bool:
        """True at most once per ``UNCLEAN_PUSH_EVERY`` seconds, and only when the previous run ended badly."""
        now = time.time() if now is None else now
        if self.previous_was_clean() or now - self._last_push < UNCLEAN_PUSH_EVERY:
            return False
        self._last_push = now
        doc = self._read()
        doc["last_unclean_push_at"] = now
        self._write(doc)
        return True

    # -- traces ---------------------------------------------------------------------------------------------
    def install_traces(self) -> None:
        """Open the two trace files (truncated at start) and arm the hang dump. Never raises."""
        try:
            self._crash_fh = open(os.path.join(self.dir, CRASH_TRACE), "w", encoding="utf-8")  # noqa: SIM115
            faulthandler.enable(file=self._crash_fh, all_threads=True)
            self._hang_fh = open(os.path.join(self.dir, HANG_TRACE), "w", encoding="utf-8")  # noqa: SIM115
            self.tick()
        except (OSError, ValueError, RuntimeError):
            self._hang_armed = False

    def tick(self) -> None:
        """The loop or an owner wait is alive: push the hang dump back. Cheap; safe to call every second."""
        if self._hang_fh is None:
            return
        try:
            faulthandler.dump_traceback_later(self.hang_seconds, repeat=False, file=self._hang_fh)
            self._hang_armed = True
        except (OSError, ValueError, RuntimeError):
            self._hang_armed = False

    def close(self) -> None:
        try:
            faulthandler.cancel_dump_traceback_later()
            if faulthandler.is_enabled():
                faulthandler.disable()
        except (OSError, ValueError, RuntimeError):
            pass
        for fh in (self._hang_fh, self._crash_fh):
            try:
                if fh is not None:
                    fh.close()
            except OSError:
                pass
        self._hang_fh = self._crash_fh = None


def _verdict_text(value: object) -> str:
    text = str(value or "unknown")
    return text if re.fullmatch(r"[A-Za-z0-9_:]{1,140}", text) else "unknown"
