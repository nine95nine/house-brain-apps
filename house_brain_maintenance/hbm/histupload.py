"""0.9.60 HISTORY_UPLOAD: a scrubbed copy of the history archive in the owner's own private data repository, so every
House Brain AI (cloud chat AIs included) can read it (owner pop-up 2026-11-08 "Separate data repo").

The 0.9.54 archive (``hbm/histarchive.py``) keeps every UTC day on the Pi. This module copies each archived day, once,
to a separate PRIVATE GitHub repository the owner creates (option ``history_upload_repo``, e.g.
``nine95nine/house-brain-history``) with its OWN fine-grained token (option ``history_upload_token``: Contents read and
write on that one repository only). The house's main code repository stays read-only to the house: this client never
uses the App's main GitHub credentials, the data token is never sent anywhere but the data repository, and the data
repository may not be the main ``github_repo`` (refused).

Files in the data repository (default branch; ONE commit per day through the GitHub Git Data API: blobs -> tree with
``base_tree`` -> commit -> fast-forward ``refs/heads/<default branch>``; a failing step changes nothing):

    days/YYYY/MM/YYYY-MM-DD.jsonl.xz      the day, re-encoded with secrets scrubbed (every other value unchanged)
    days/YYYY/MM/YYYY-MM-DD.meta.json     both SHA-256s (the Pi's file and the uploaded copy), counts, scrub counts
    summaries/YYYY/MM/DD/SS.json.gz       the summary in 64 shards (what chat AIs and the connector read)
    summaries/YYYY/MM/DD/index.json       the shard file names, sizes, SHA-256 and entity count per shard
    index.json                            every uploaded day: file names, sizes, SHA-256; skipped days with the reason
    README.md                             the formats in plain English (in the first commit only)

Shard rule (the connector Worker uses the same formula; Cloudflare Free allows ~10 ms CPU per request, so it opens one
small shard, never a whole day): ``SS = int(sha256(entity_id UTF-8).hexdigest()[:8], 16) % 64``, two decimal digits.

Scrub rules for the uploaded copy (the Pi's file is never changed):
* attribute keys ``entity_picture``, ``access_token``, ``token``, ``api_key``, ``password``, ``secret`` and any key
  ending ``_token``, ``_key`` or ``_secret`` are dropped;
* a string value that looks like a secret (a secret this App knows, ``net.redact``; a token shape,
  ``issues.RE_TOKEN_SHAPES``; ``keyword=value`` / ``keyword: value`` for password, token, secret, api key, access key,
  auth; ``Bearer <long value>``) is replaced whole by ``[redacted]``;
* everything else keeps exactly its value, so the copy stays lossless for the data.

Summary (per shard, a JSON object keyed by entity id): ``rows``, ``first_t``, ``last_t`` (seconds since the UTC day
start); numeric states: ``min``, ``max``, ``mean``, ``twa`` (time-weighted mean), ``series5m`` ``[[t_start_s, mean],
...]``; other states: ``state_seconds`` (top 20) and ``transitions``.

Rules: uploads only days already archived and verified on the Pi (its SHA-256 is checked first); ``uploaded.json`` in
the archive root remembers what each repository already has (idempotent; nothing is uploaded twice); a file over
``MAX_FILE_BYTES`` (50 MB; GitHub's limit is 100 MB) is skipped with a visible reason; failures stop the run and are
retried later. Read-only toward Home Assistant. ``CONTROL_AUTHORITY=NONE``.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import lzma
import math
import os
import re
import time
import urllib.parse

from . import issues as I, net
from .github import RE_REPO
from .histarchive import ARCHIVE_ROOT, PRESET, day_paths, days_between
from .jobs import DONE, FAILED, REFUSED, Result

SCHEMA = "house_brain_history_upload.v1"
SUMMARY_SCHEMA = "house_brain_history_summary.v1"
STATE_FILE = "uploaded.json"
MAX_FILE_BYTES = 50 * 1024 ** 2          # GitHub's contents API refuses files over 100 MB
HARD_LIMIT_BYTES = 95 * 1024 ** 2        # never send anything GitHub would refuse anyway
SHARDS = 64
SHARD_RULE = "int(sha256(entity_id utf-8).hexdigest()[:8], 16) % 64, two decimal digits"
RE_GIT_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
RE_BRANCH = re.compile(r"[A-Za-z0-9._/-]{1,100}")
NIGHT_MAX_DAYS = 10                      # at most this many days a night, oldest first
NIGHT_MINUTES = 30
JOB_MINUTES = 60
MAX_RANGE_DAYS = 45
DAY_S = 86400
BUCKET_S = 300                           # the summary's 5-minute series
TOP_STATES = 20
MARKER = "[redacted]"
DROP_KEYS = frozenset({"entity_picture", "access_token", "token", "api_key", "password", "secret"})
DROP_SUFFIXES = ("_token", "_key", "_secret")
MISSING = frozenset({None, "", "unavailable", "unknown"})
RE_KEYWORD_VALUE = re.compile(r"(?i)\b(?:bearer|token|password|passwd|pwd|secret|api[_-]?key|access[_-]?key|auth)"
                              r"\s*[=:]\s*[^\s,;'\"]+")
RE_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{16,}=*")
PUT_TIMEOUT = 300.0
_monotonic = time.monotonic              # tests replace it
_now = time.time


class SkipDay(Exception):
    """This day cannot be uploaded as it is (too big, damaged on the Pi); remembered with the reason, not retried."""


# -- scrubbing -----------------------------------------------------------------------------------------------------------
def drop_key(key) -> bool:
    k = str(key).lower()
    return k in DROP_KEYS or k.endswith(DROP_SUFFIXES)


_SEEN: dict[str, bool] = {}              # the same values repeat all day; a bounded memo


def secret_like(text: str) -> bool:
    hit = _SEEN.get(text)
    if hit is None:
        hit = _secret_like(text)
        if len(_SEEN) < 100_000:
            _SEEN[text] = hit
    return hit


def _secret_like(text: str) -> bool:
    return (net.redact(text) != text or bool(I.RE_TOKEN_SHAPES.search(text)) or bool(RE_KEYWORD_VALUE.search(text))
            or bool(RE_BEARER.search(text)))


def clean(value, counts: dict):
    """The value with secret-like strings replaced and secret attribute keys dropped (recursively)."""
    if isinstance(value, str):
        if secret_like(value):
            counts["redacted_values"] += 1
            return MARKER
        return value
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if drop_key(k):
                counts["dropped_attribute_keys"] += 1
                continue
            out[k] = clean(v, counts)
        return out
    if isinstance(value, list):
        return [clean(v, counts) for v in value]
    return value


def _line(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), default=str).encode() + b"\n"


# -- summary -------------------------------------------------------------------------------------------------------------
def _num(state) -> float | None:
    if isinstance(state, bool) or state is None:
        return None
    try:
        v = float(state)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _r(x: float) -> float:
    return round(x, 4)


def summarize(rows: list) -> dict:
    """One entity's day: ``rows`` as in the day file (``[t, state, changed attributes]``)."""
    pts = sorted(((min(max(float(r[0]), 0.0), float(DAY_S)), r[1]) for r in rows
                  if isinstance(r, list) and len(r) >= 2 and isinstance(r[0], (int, float))
                  and not isinstance(r[0], bool)), key=lambda p: p[0])
    out: dict = {"rows": len(rows)}
    if not pts:
        return out
    out["first_t"], out["last_t"] = _r(pts[0][0]), _r(pts[-1][0])
    present = [s for _, s in pts if s not in MISSING]
    numeric = bool(present) and all(_num(s) is not None for s in present)
    segs = [(t, pts[i + 1][0] if i + 1 < len(pts) else float(DAY_S), s) for i, (t, s) in enumerate(pts)]
    if numeric:
        values = [_num(s) for s in present]
        out.update(kind="numeric", min=_r(min(values)), max=_r(max(values)), mean=_r(sum(values) / len(values)))
        total = weighted = 0.0
        buckets: dict[int, list[float]] = {}
        for t0, t1, s in segs:
            v = _num(s)
            if v is None or t1 <= t0:
                continue
            total += t1 - t0
            weighted += v * (t1 - t0)
            b = int(t0 // BUCKET_S)
            while b * BUCKET_S < t1:
                lo, hi = max(t0, b * BUCKET_S), min(t1, (b + 1) * BUCKET_S)
                if hi > lo:
                    acc = buckets.setdefault(b, [0.0, 0.0])
                    acc[0] += v * (hi - lo)
                    acc[1] += hi - lo
                b += 1
        out["twa"] = _r(weighted / total) if total > 0 else None
        out["covered_s"] = round(total, 1)
        out["series5m"] = [[b * BUCKET_S, _r(acc[0] / acc[1])] for b, acc in sorted(buckets.items())]
    else:
        seconds: dict[str, float] = {}
        for t0, t1, s in segs:
            key = "null" if s is None else str(s)
            seconds[key] = seconds.get(key, 0.0) + max(0.0, t1 - t0)
        top = sorted(seconds.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_STATES]
        out.update(kind="state", state_seconds={k: round(v, 1) for k, v in top}, states_distinct=len(seconds),
                   transitions=sum(1 for i in range(1, len(pts)) if pts[i][1] != pts[i - 1][1]))
    return out


# -- the copy ------------------------------------------------------------------------------------------------------------
def build_copy(local_path: str) -> tuple[bytes, dict, dict]:
    """Re-encode one archived day: (scrubbed xz bytes, summary document, counts)."""
    counts = {"lines": 0, "entities": 0, "rows": 0, "statistics_ids": 0, "statistics_rows": 0,
              "dropped_attribute_keys": 0, "redacted_values": 0}
    _SEEN.clear()                         # secrets registered since the last day count too
    comp = lzma.LZMACompressor(preset=PRESET)
    parts: list[bytes] = []
    size = 0
    header: dict = {}
    entities: dict[str, dict] = {}
    with lzma.open(local_path, "rb") as fh:
        for raw in fh:
            obj = json.loads(raw)
            if counts["lines"] == 0 and isinstance(obj, dict) and "schema" in obj:
                header = obj
                obj = {**obj, "upload_copy": {"schema": SCHEMA, "marker": MARKER,
                                              "dropped_keys": sorted(DROP_KEYS) + ["*" + s for s in DROP_SUFFIXES]}}
            elif isinstance(obj, dict) and "e" in obj:
                rows = [[r[0], *(clean(x, counts) for x in r[1:])] if isinstance(r, list) and r else clean(r, counts)
                        for r in obj.get("r") or []]
                obj = {**obj, "r": rows}
                counts["entities"] += 1
                counts["rows"] += len(rows)
                entities[str(obj["e"])] = summarize(rows)
            elif isinstance(obj, dict) and "st" in obj:
                obj = {**obj, "r": clean(obj.get("r") or [], counts)}
                counts["statistics_ids"] += 1
                counts["statistics_rows"] += len(obj["r"])
            else:
                obj = clean(obj, counts)
            counts["lines"] += 1
            chunk = comp.compress(_line(obj))
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                raise SkipDay("TOO_BIG_DAY_FILE")
            parts.append(chunk)
    parts.append(comp.flush())
    data = b"".join(parts)
    if len(data) > MAX_FILE_BYTES:
        raise SkipDay("TOO_BIG_DAY_FILE")
    summary = {"schema": SUMMARY_SCHEMA, "day_utc": header.get("day_utc"), "window_utc": header.get("window_utc"),
               "t_unit": "seconds since the UTC day start", "bucket_s": BUCKET_S,
               "entities": dict(sorted(entities.items())), "control_authority": "NONE"}
    return data, summary, counts


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def shard_of(entity_id: str) -> int:
    """The summary shard of one entity: ``int(sha256(entity_id utf-8).hexdigest()[:8], 16) % 64``. The connector
    Worker uses the same formula to open only one small file (Cloudflare Free: ~10 ms CPU per request)."""
    return int(hashlib.sha256(entity_id.encode("utf-8")).hexdigest()[:8], 16) % SHARDS


def repo_paths(day: str) -> dict[str, str]:
    folder = f"{day[:4]}/{day[5:7]}"
    return {"day": f"days/{folder}/{day}.jsonl.xz", "meta": f"days/{folder}/{day}.meta.json",
            "summary_dir": f"summaries/{folder}/{day[8:10]}",
            "summary_index": f"summaries/{folder}/{day[8:10]}/index.json"}


def shard_files(summary: dict) -> tuple[dict[str, bytes], dict]:
    """The day's summary as SHARDS gzip files ``SS.json.gz`` (every shard written, ``{}`` when empty) + their index."""
    groups: dict[int, dict] = {s: {} for s in range(SHARDS)}
    for eid, value in summary["entities"].items():
        groups[shard_of(eid)][eid] = value
    files, listing = {}, []
    for s in range(SHARDS):
        name = f"{s:02d}.json.gz"
        data = gzip.compress(json.dumps(groups[s], separators=(",", ":"), sort_keys=True).encode(), mtime=0)
        if len(data) > MAX_FILE_BYTES:
            raise SkipDay("TOO_BIG_SUMMARY")
        files[name] = data
        listing.append({"file": name, "bytes": len(data), "entities": len(groups[s]), "sha256": _sha(data)})
    index = {"schema": SUMMARY_SCHEMA + ".index", "day_utc": summary.get("day_utc"),
             "window_utc": summary.get("window_utc"), "shards": SHARDS, "shard_rule": SHARD_RULE,
             "t_unit": summary.get("t_unit"), "bucket_s": BUCKET_S, "entities": len(summary["entities"]),
             "files": listing, "control_authority": "NONE"}
    return files, index


# -- the client ----------------------------------------------------------------------------------------------------------
class DataRepo:
    """GitHub Git Data client for the data repository, signed ONLY by the data token (never the App's own). One day =
    one commit: blobs -> tree (``base_tree`` = the branch head's tree) -> commit -> fast-forward the branch ref."""

    def __init__(self, repo: str, token: str, api: str = "https://api.github.com") -> None:
        if not RE_REPO.fullmatch(repo or ""):
            raise ValueError("invalid data repository name")
        if not token:
            raise ValueError("data repository token missing")
        self.repo = repo
        self.api = api.rstrip("/")
        self._token = token
        net.register_secret(token)
        self.calls = 0
        self._branch: str | None = None

    def _call(self, method: str, path: str, body=None, timeout: float = 60.0):
        self.calls += 1
        url = f"{self.api}/repos/{self.repo}{path}"
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "house-brain-maintenance"}
        _, data = net.request(method, url, headers, body=body, timeout=timeout)
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _sha_field(value) -> str:
        if not isinstance(value, str) or not RE_GIT_SHA.fullmatch(value):
            raise net.NetError(0, "GitHub answered without a valid SHA")
        return value

    def branch(self) -> str:
        if self._branch is None:
            name = self._call("GET", "").get("default_branch")
            if not isinstance(name, str) or not RE_BRANCH.fullmatch(name):
                raise net.NetError(0, "the data repository has no default branch (create it with a README)")
            self._branch = name
        return self._branch

    def commit_files(self, files: dict[str, bytes], message: str) -> str:
        """Write every ``files`` path in ONE commit on the default branch; returns the new commit SHA. Any failing
        step raises (nothing is changed: the branch moves only at the last step, and only fast-forward)."""
        for path, data in files.items():
            if len(data) > HARD_LIMIT_BYTES:
                raise SkipDay("TOO_BIG_FILE")
        branch = urllib.parse.quote(self.branch(), safe="/")
        head = self._sha_field(self._call("GET", f"/git/ref/heads/{branch}").get("object", {}).get("sha"))
        base_tree = self._sha_field(self._call("GET", f"/git/commits/{head}").get("tree", {}).get("sha"))
        blobs: dict[bytes, str] = {}
        entries = []
        for path, data in sorted(files.items()):
            key = hashlib.sha256(data).digest()
            if key not in blobs:                       # identical files (empty shards) share one blob
                blobs[key] = self._sha_field(self._call(
                    "POST", "/git/blobs", {"content": base64.b64encode(data).decode("ascii"), "encoding": "base64"},
                    timeout=PUT_TIMEOUT).get("sha"))
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blobs[key]})
        tree = self._sha_field(self._call("POST", "/git/trees", {"base_tree": base_tree, "tree": entries}).get("sha"))
        commit = self._sha_field(self._call("POST", "/git/commits", {"message": message, "tree": tree,
                                                                     "parents": [head]}).get("sha"))
        self._call("PATCH", f"/git/refs/heads/{branch}", {"sha": commit, "force": False})
        return commit


# -- state ---------------------------------------------------------------------------------------------------------------
def _iso(t: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(_now() if t is None else t))


def load_state(root: str) -> dict:
    try:
        with open(os.path.join(root, STATE_FILE), encoding="utf-8") as fh:
            doc = json.load(fh)
        if not isinstance(doc, dict) or not isinstance(doc.get("repos"), dict):
            raise ValueError
        return doc
    except (OSError, ValueError):
        return {"schema": SCHEMA + ".state", "repos": {}}


def save_state(root: str, doc: dict) -> None:
    path = os.path.join(root, STATE_FILE)
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True)
    os.replace(path + ".tmp", path)


def _repo_state(doc: dict, repo: str) -> dict:
    rs = doc["repos"].setdefault(repo, {})
    rs.setdefault("days", {})
    rs.setdefault("skipped", {})
    rs.setdefault("readme", False)
    rs.setdefault("index_dirty", False)
    return rs


def local_days(root: str) -> list[str]:
    try:
        with open(os.path.join(root, "index.json"), encoding="utf-8") as fh:
            idx = json.load(fh)
        days = idx.get("days") if isinstance(idx, dict) else None
    except (OSError, ValueError):
        return []
    return sorted(d for d in (days or {}) if isinstance(d, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d))


# -- one day -------------------------------------------------------------------------------------------------------------
def prepare_day(root: str, day: str) -> tuple[dict[str, bytes], dict]:
    """The day's files for the data repository (path -> bytes) and what ``uploaded.json`` keeps about them."""
    final, meta_path = day_paths(root, day)
    try:
        with open(meta_path, encoding="utf-8") as fh:
            local_meta = json.load(fh)
    except (OSError, ValueError):
        raise SkipDay("LOCAL_META_UNREADABLE") from None
    if not os.path.exists(final):
        raise SkipDay("LOCAL_DAY_MISSING")
    local_sha = _file_sha(final)
    if local_sha != local_meta.get("sha256"):
        raise SkipDay("LOCAL_SHA_MISMATCH")
    data, summary, counts = build_copy(final)
    shards, shard_index = shard_files(summary)
    shard_index_bytes = json.dumps(shard_index, indent=1, sort_keys=True).encode()
    summary_bytes = sum(len(b) for b in shards.values()) + len(shard_index_bytes)
    paths = repo_paths(day)
    meta = {"schema": SCHEMA + ".meta", "day_utc": day, "file": paths["day"].rsplit("/", 1)[1],
            "local_sha256": local_sha, "local_bytes": os.path.getsize(final), "sha256": _sha(data),
            "bytes": len(data), "summary_dir": paths["summary_dir"], "summary_index": paths["summary_index"],
            "summary_shards": SHARDS, "shard_rule": SHARD_RULE, "summary_bytes": summary_bytes,
            "summary_index_sha256": _sha(shard_index_bytes),
            **counts, "window_utc": local_meta.get("window_utc"), "unread": local_meta.get("unread"),
            "left_out_private": local_meta.get("left_out_private"), "left_out_rules": local_meta.get("left_out_rules"),
            "scrub": {"marker": MARKER, "dropped_keys": sorted(DROP_KEYS) + ["*" + s for s in DROP_SUFFIXES]},
            "uploaded_at": _iso(), "control_authority": "NONE"}
    meta_bytes = json.dumps(meta, indent=1, sort_keys=True).encode()
    if len(meta_bytes) > MAX_FILE_BYTES or len(shard_index_bytes) > MAX_FILE_BYTES:
        raise SkipDay("TOO_BIG_META")
    files = {paths["day"]: data, paths["meta"]: meta_bytes, paths["summary_index"]: shard_index_bytes,
             **{f"{paths['summary_dir']}/{name}": b for name, b in shards.items()}}
    info = {"sha256": meta["sha256"], "local_sha256": local_sha, "bytes": len(data), "summary_bytes": summary_bytes,
            "summary_index_sha256": meta["summary_index_sha256"], "meta_bytes": len(meta_bytes),
            "rows": counts["rows"], "entities": counts["entities"], "at": meta["uploaded_at"]}
    return files, info


def repo_index(rs: dict) -> dict:
    days = {}
    for day, info in sorted(rs["days"].items()):
        p = repo_paths(day)
        days[day] = {"file": p["day"], "bytes": info.get("bytes"), "sha256": info.get("sha256"), "meta": p["meta"],
                     "summary_dir": p["summary_dir"], "summary_index": p["summary_index"],
                     "summary_bytes": info.get("summary_bytes"), "summary_index_sha256": info.get("summary_index_sha256"),
                     "local_sha256": info.get("local_sha256"), "rows": info.get("rows"),
                     "entities": info.get("entities"), "commit": info.get("commit")}
    return {"schema": SCHEMA + ".index", "days": days,
            "skipped": {d: v.get("reason") for d, v in sorted(rs["skipped"].items())},
            "summary_shards": SHARDS, "shard_rule": SHARD_RULE,
            "formats": "README.md", "updated_at": _iso(), "control_authority": "NONE"}


def _index_bytes(rs: dict) -> bytes:
    return json.dumps(repo_index(rs), indent=1, sort_keys=True).encode()


README = """# House Brain history (private data repository)

This repository is written by the House Brain Maintenance App on the owner's Home Assistant (version 0.9.60 or later).
It holds a copy of every day of Home Assistant history, so any AI the owner allows can read it. Nothing here
controls the house. Each day arrives as one commit.

## Files

- `index.json`: every uploaded day, with file names, sizes and SHA-256 checksums. Days that could not be uploaded
  are listed under `skipped` with the reason.
- `summaries/YYYY/MM/DD/`: start here. The day's summary, split into 64 small files so a reader opens only the one it
  needs:
  - `SS.json.gz` (`00` to `63`): plain JSON, gzip compressed, one entry per entity (keyed by entity id).
  - **Shard rule:** an entity is in shard `int(sha256(entity_id as UTF-8).hexdigest()[:8], 16) % 64`, written as two
    digits. Example in Python: `int(hashlib.sha256(b"sensor.x").hexdigest()[:8], 16) % 64`. All 64 files are always
    written (`{}` when empty).
  - `index.json`: the shard file names, sizes, SHA-256 and the number of entities in each.
  - per entity: `rows` (recorded changes that day); `first_t` / `last_t` (seconds since the start of the UTC day).
    Number sensors (`kind: numeric`): `min`, `max`, `mean` (of the recorded values), `twa` (time-weighted mean,
    usually the better average), `covered_s` (seconds with a number) and `series5m`, a list of
    `[seconds since day start, mean of those 5 minutes]`. Other entities (`kind: state`): `state_seconds` (the 20
    states with the most time), `states_distinct` and `transitions` (how often the state changed).
- `days/YYYY/MM/YYYY-MM-DD.jsonl.xz`: the full day, xz compressed JSON Lines (one JSON object per line):
  - line 1: a header (`schema`, `day_utc`, `window_utc`, `start_epoch`, `row` = the column names, `upload_copy`).
  - entity lines: `{"e": entity_id, "r": [[seconds since window start, state, changed attributes only], ...]}`.
    The first row of an entity carries all its attributes; later rows carry only what changed (`null` = removed).
  - statistics lines: `{"st": statistic_id, "p": "5minute", "r": [rows as Home Assistant returns them]}`.
- `days/YYYY/MM/YYYY-MM-DD.meta.json`: counts and both checksums: `local_sha256` (the file kept on the Pi) and
  `sha256` (this copy).

## What is left out

People, trackers, cameras, locks, alarm panels, zones, images, geo locations, free-text helpers and the health
sensors are never read at all. In this copy, attributes named `entity_picture`, `access_token`, `token`, `api_key`,
`password`, `secret` or ending in `_token`, `_key` or `_secret` are removed, and any text that looks like a secret is
replaced by `[redacted]`. Every other value is exactly what Home Assistant recorded.

Read in Python: `import lzma, json; [json.loads(l) for l in lzma.open(path)]` and
`import gzip, json; json.load(gzip.open(path))`.
"""


# -- runs ----------------------------------------------------------------------------------------------------------------
def run_upload(client: DataRepo, root: str, days: list[str] | None, max_days: int, minutes: int) -> Result:
    """Upload every archived day (in ``days`` when given) that this repository does not have yet, oldest first: one
    commit per day (its files, the updated ``index.json`` and, the first time, ``README.md``)."""
    began = _monotonic()
    deadline = began + minutes * 60
    have = local_days(root)
    asked = None if days is None else set(days)
    wanted = [d for d in have if asked is None or d in asked]
    not_archived = sorted(set(days or []) - set(have))
    doc = load_state(root)
    rs = _repo_state(doc, client.repo)
    todo = [d for d in wanted if d not in rs["days"] and d not in rs["skipped"]]
    uploaded, skipped, left = [], {}, []
    total = 0
    for day in todo:
        if len(uploaded) >= max_days or _monotonic() >= deadline:
            left.append(day)
            continue
        try:
            files, info = prepare_day(root, day)
        except SkipDay as skip:
            rs["skipped"][day] = {"reason": str(skip), "at": _iso()}
            rs["index_dirty"] = True
            skipped[day] = str(skip)
            save_state(root, doc)
            continue
        trial = {**rs, "days": {**rs["days"], day: info}}
        files["index.json"] = _index_bytes(trial)
        if not rs["readme"]:
            files["README.md"] = README.encode()
        info["commit"] = client.commit_files(files, f"history: {day}")
        rs["days"][day] = info
        rs["readme"] = True
        rs["index_dirty"] = False
        save_state(root, doc)
        uploaded.append(day)
        total += sum(len(b) for b in files.values())
    if rs["index_dirty"]:                              # only skips since the last commit: one index-only commit
        files = {"index.json": _index_bytes(rs)}
        if not rs["readme"]:
            files["README.md"] = README.encode()
        client.commit_files(files, "history: index")
        rs["index_dirty"] = False
        rs["readme"] = True
        save_state(root, doc)
    facts = {"repo": client.repo, "uploaded": uploaded, "already_uploaded": len([d for d in wanted if d in rs["days"]])
             - len(uploaded), "skipped": skipped, "left_for_next_run": left, "not_archived": not_archived[:45],
             "bytes_uploaded": total, "github_calls": client.calls, "seconds": round(_monotonic() - began, 1),
             "control_authority": "NONE"}
    reasons = [f"uploaded {len(uploaded)} day(s) ({total / 1024 ** 2:.1f} MB, one commit each) to the data "
               f"repository; {facts['already_uploaded']} already there; {len(left)} left for the next run"]
    for day, why in skipped.items():
        reasons.append(f"{day} skipped: {why} (files over {MAX_FILE_BYTES // 1024 ** 2} MB are not uploaded)"
                       if why.startswith("TOO_BIG") else f"{day} skipped: {why}")
    if not_archived:
        reasons.append(f"{len(not_archived)} day(s) in the range are not archived on the Pi yet")
    reasons.append("read-only toward Home Assistant; only the data repository's own token is used")
    return Result(DONE, reasons, facts)


def refusal(repo: str, token: str, main_repo: str) -> str | None:
    """Why the configured upload cannot run (None = it can)."""
    if not repo:
        return None
    if not RE_REPO.fullmatch(repo):
        return "HISTORY_UPLOAD_REPO_INVALID"
    if repo.lower() == (main_repo or "").lower():
        return "HISTORY_UPLOAD_SAME_REPO"
    if not token:
        return "HISTORY_UPLOAD_TOKEN_MISSING"
    return None


def nightly(client: DataRepo, root: str | None = None) -> Result:
    return run_upload(client, root or ARCHIVE_ROOT, None, NIGHT_MAX_DAYS, NIGHT_MINUTES)


def run(client: DataRepo | None, why: str | None, m, root: str | None = None) -> Result:
    """The job (``maint/requests``): ``from``..``to`` UTC days, at most 45."""
    days = days_between(str(m.upload_from), str(m.upload_to))
    if client is None:
        return Result(REFUSED, [f"the history upload is not set up ({why or 'HISTORY_UPLOAD_OFF'}); nothing was sent"],
                      {"reason": why or "HISTORY_UPLOAD_OFF", "control_authority": "NONE"})
    try:
        return run_upload(client, root or ARCHIVE_ROOT, days, MAX_RANGE_DAYS, JOB_MINUTES)
    except net.NetError as err:
        return Result(FAILED, [f"the data repository did not accept the upload: {net.redact(str(err))[:200]}; "
                               "days already sent are remembered, the rest is retried"],
                      {"repo": client.repo, "status": err.status, "control_authority": "NONE"})
