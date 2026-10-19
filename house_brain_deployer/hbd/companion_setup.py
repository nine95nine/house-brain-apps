"""House Brain Companion set-up in Home Assistant (0.3.30). Two closed request kinds, no caller-supplied values.

Owner pop-ups 2026-10-19: "Yes, one tap" (the Deployer may set the Companion up in Home Assistant after one approval)
and "Pi makes it, nobody sees it" (the key is made here and never shown; only its SHA-256 leaves the house).

``house_brain_companion_key_request.v1``
    Makes a 48-hex key in this App's private ``/data`` (mode 0600) when there is none, and reports only its
    SHA-256 fingerprint. Nothing in Home Assistant changes and nothing is restarted, so no approval is asked
    (lookup allowance). ``rotate: true`` makes a new key; it asks first, because the old key then stops working.

``house_brain_companion_setup_request.v1``
    After the owner's approval, and only then, through Home Assistant's own config flows:
      1. adds the llama.cpp integration for the Companion URL with the key (literals in ``ha.py``);
      2. picks the Companion model; Home Assistant creates its conversation agent with its recommended options,
         which include the Assist control API;
      3. at once re-saves that agent as "recommended + model" WITHOUT ``llm_hass_api`` (no device control), and
         checks the agent no longer reports the CONTROL feature;
      4. creates the "House Brain Companion" Assist pipeline with that agent, copying the language and voice
         settings of the preferred pipeline (no wake word; Home Assistant's own sentence matching off).
    Any failure after step 1 deletes the entry this run created (fail closed). The bodies are pinned by
    ``ha._body_guard`` / ``ha._ws_guard``; nothing here can name another integration, URL, model or pipeline.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass

from . import approval, net
from .ha import (COMPANION_DOMAIN, COMPANION_MODEL, COMPANION_PIPELINE, PIPELINE_KEYS,
                 RE_COMPANION_KEY, RE_CONVERSATION_ENTITY, RE_ENTRY_ID, companion_url_ok)
from .manifest import RE_REQUEST_ID, REQUESTERS, ManifestError

KEY_SCHEMA = "house_brain_companion_key_request.v1"
SETUP_SCHEMA = "house_brain_companion_setup_request.v1"
KEY_FILE = "companion_key"
KEY_READY = "COMPANION_KEY_READY"
SETUP_DONE = "COMPANION_SETUP_DONE"
SETUP_FAILED = "COMPANION_SETUP_FAILED"
ALREADY_SET_UP = "COMPANION_ALREADY_SET_UP"
CONTROL_FEATURE = 1          # conversation.ConversationEntityFeature.CONTROL
VERIFY_SECONDS = 30.0


@dataclass(frozen=True)
class CompanionRequest:
    kind: str                # "key" | "setup"
    request_id: str
    requested_by: str
    tracking_issue: int
    rotate: bool
    digest: str
    url: str = ""            # setup only: must hash to ha.COMPANION_URL_SHA256


def kind(raw_bytes: bytes) -> str | None:
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return {KEY_SCHEMA: "key", SETUP_SCHEMA: "setup"}.get(data.get("schema"))


def parse(raw_bytes: bytes) -> CompanionRequest:
    if len(raw_bytes) > 4096:
        raise ManifestError("TOO_LARGE")

    def _no_dupes(pairs):
        out = {}
        for key, value in pairs:
            if key in out:
                raise ManifestError("DUPLICATE_KEY", str(key)[:40])
            out[key] = value
        return out

    try:
        data = json.loads(raw_bytes.decode("utf-8", errors="strict"), object_pairs_hook=_no_dupes)
    except ManifestError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as err:
        raise ManifestError("JSON") from err
    if not isinstance(data, dict):
        raise ManifestError("TYPE")
    which = {KEY_SCHEMA: "key", SETUP_SCHEMA: "setup"}.get(data.get("schema"))
    if which is None:
        raise ManifestError("SCHEMA_VERSION")
    required = {"schema", "request_id", "requested_by", "tracking_issue"}
    if which == "setup":
        required.add("companion_url")
    optional = {"rotate"} if which == "key" else set()
    if required - set(data):
        raise ManifestError("MISSING_KEY", sorted(required - set(data))[0])
    if set(data) - required - optional:
        raise ManifestError("UNKNOWN_KEY", sorted(set(data) - required - optional)[0][:40])
    rid = data["request_id"]
    if not isinstance(rid, str) or not RE_REQUEST_ID.fullmatch(rid):
        raise ManifestError("PATTERN", "request_id")
    if data["requested_by"] not in REQUESTERS:
        raise ManifestError("REQUESTER")
    issue = data["tracking_issue"]
    if isinstance(issue, bool) or not isinstance(issue, int) or not 1 <= issue <= 10_000_000:
        raise ManifestError("RANGE", "tracking_issue")
    rotate = data.get("rotate", False)
    if rotate is not False and rotate is not True:
        raise ManifestError("TYPE", "rotate")
    url = data.get("companion_url", "")
    if which == "setup" and not companion_url_ok(url):
        raise ManifestError("PATTERN", "companion_url")
    return CompanionRequest(which, rid, data["requested_by"], issue, rotate, hashlib.sha256(raw_bytes).hexdigest(),
                            url)


# ---------------------------------------------------------------- the key (never leaves this App's /data)
def key_path(data_dir: str) -> str:
    return os.path.join(data_dir, KEY_FILE)


def read_key(data_dir: str) -> str | None:
    try:
        with open(key_path(data_dir), encoding="ascii") as fh:
            key = fh.read().strip()
    except (OSError, UnicodeDecodeError):
        return None
    return key if RE_COMPANION_KEY.fullmatch(key) else None


def fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode("ascii")).hexdigest()


def make_key(data_dir: str, replace: bool = False) -> str:
    """A new 48-hex key, written 0600 (atomically when replacing). Returns the key; callers report only its hash."""
    key = secrets.token_hex(24)
    path = key_path(data_dir)
    tmp = path + ".new"
    try:
        os.unlink(tmp)
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(key)
        fh.flush()
        os.fsync(fh.fileno())
    if not replace and os.path.exists(path):
        os.unlink(tmp)
        raise ManifestError("KEY_EXISTS")
    os.replace(tmp, path)
    return key


def run_key(engine, req: CompanionRequest, data_dir: str):
    from .engine import REJECTED, TIMED_OUT, Result

    engine.j.audit(req.request_id, "RECEIVED", digest=req.digest, requested_by=req.requested_by, kind="companion_key")
    existing = read_key(data_dir)
    if existing and not req.rotate:
        return Result(KEY_READY, ["The Companion key already exists; nothing changed."],
                      {"companion_key_sha256": fingerprint(existing), "created": False})
    if req.rotate:
        outcome = engine._ask(req.request_id, "LOOKUP", "House Brain Deployer: new Companion key?",
                              f"{req.request_id} from {req.requested_by}\nMakes a NEW Companion key on the Pi. The "
                              "old key stops working until the Companion Worker has the new fingerprint and Home "
                              "Assistant is set up again. Nothing else changes.",
                              engine.s.timing.approval_timeout)
        if outcome != approval.APPROVE:
            return Result(REJECTED if outcome == approval.REJECT else TIMED_OUT, [outcome])
    key = make_key(data_dir, replace=bool(existing))
    return Result(KEY_READY, ["A new Companion key was made on the Pi. Only its SHA-256 is reported."],
                  {"companion_key_sha256": fingerprint(key), "created": True})


# ---------------------------------------------------------------- the set-up
class SetupError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def _expect(result: dict, kind_: str, step: str | None = None) -> dict:
    if result.get("type") != kind_ or (step is not None and result.get("step_id") != step):
        errors = result.get("errors") or {}
        reason = result.get("reason") or (",".join(f"{k}={v}" for k, v in errors.items()) if errors else "")
        raise SetupError("FLOW_UNEXPECTED", f"{result.get('type')}/{result.get('step_id')} {str(reason)[:120]}")
    return result


def _flow_id(result: dict) -> str:
    fid = result.get("flow_id")
    if not isinstance(fid, str) or len(fid) != 32:
        raise SetupError("FLOW_ID_SHAPE")
    return fid


def setup(ha, key: str, url: str, sleep=time.sleep, monotonic=time.monotonic, progress: dict | None = None) -> dict:
    """The four steps above. Returns facts; raises SetupError. ``progress`` (the same dict as the facts) is filled
    as steps finish, so the caller can delete ``progress['entry_id']`` when a later step fails."""
    facts: dict = progress if progress is not None else {}
    # 1. server
    first = _expect(ha.config_flow(None, {"handler": COMPANION_DOMAIN, "show_advanced_options": False}), "form", "user")
    flow = _flow_id(first)
    server = ha.config_flow(flow, {"base_url": url, "api_key": key})
    if server.get("type") == "abort" and server.get("reason") == "already_configured":
        raise SetupError("ALREADY_CONFIGURED")
    _expect(server, "form", "model")
    # 2. model -> entry with one conversation subentry
    created = _expect(ha.config_flow(flow, {"chat_model": COMPANION_MODEL}), "create_entry")
    entry_id = (created.get("result") or {}).get("entry_id")
    if not isinstance(entry_id, str) or not RE_ENTRY_ID.fullmatch(entry_id):
        raise SetupError("ENTRY_ID_SHAPE")
    facts["entry_id"] = entry_id
    subs = ha.ws_once({"type": "config_entries/subentries/list", "entry_id": entry_id})
    convo = [s for s in subs or [] if isinstance(s, dict) and s.get("subentry_type") == "conversation"]
    if len(convo) != 1 or not RE_ENTRY_ID.fullmatch(str(convo[0].get("subentry_id"))):
        raise SetupError("SUBENTRY_NOT_FOUND")
    # 3. re-save without the control API
    start = _expect(ha.subentry_flow(None, {"handler": [entry_id, "conversation"],
                                           "subentry_id": convo[0]["subentry_id"]}), "form", "init")
    saved = ha.subentry_flow(_flow_id(start), {"recommended": True, "chat_model": COMPANION_MODEL})
    if saved.get("type") != "abort" or saved.get("reason") != "reconfigure_successful":
        raise SetupError("CONTROL_NOT_REMOVED", f"{saved.get('type')}/{saved.get('reason')}")
    entities = [e for e in ha.ws_once({"type": "config/entity_registry/list"}, timeout=60) or []
                if isinstance(e, dict) and e.get("config_entry_id") == entry_id
                and RE_CONVERSATION_ENTITY.fullmatch(str(e.get("entity_id")))]
    if len(entities) != 1:
        raise SetupError("AGENT_NOT_FOUND")
    agent = entities[0]["entity_id"]
    facts["agent"] = agent
    deadline = monotonic() + VERIFY_SECONDS
    while True:   # the entry reloads after the re-save; the CONTROL feature must be gone
        state = ha.get_state(agent) or {}
        features = (state.get("attributes") or {}).get("supported_features")
        if isinstance(features, int) and not features & CONTROL_FEATURE and state.get("state") != "unavailable":
            break
        if monotonic() > deadline:
            raise SetupError("CONTROL_STILL_ON", f"supported_features={features}")
        sleep(1.0)
    facts["control"] = "none"
    # 4. pipeline
    listed = ha.ws_once({"type": "assist_pipeline/pipeline/list"}) or {}
    pipelines = [p for p in listed.get("pipelines") or [] if isinstance(p, dict)]
    existing = [p for p in pipelines if p.get("name") == COMPANION_PIPELINE]
    if existing:
        facts["pipeline"] = {"id": existing[0].get("id"), "created": False}
        return facts
    preferred = next((p for p in pipelines if p.get("id") == listed.get("preferred_pipeline")), {})
    lang = preferred.get("language") or "en"
    pipeline = {
        "conversation_engine": agent, "conversation_language": preferred.get("conversation_language") or lang,
        "language": lang, "name": COMPANION_PIPELINE,
        "stt_engine": preferred.get("stt_engine"), "stt_language": preferred.get("stt_language"),
        "tts_engine": preferred.get("tts_engine"), "tts_language": preferred.get("tts_language"),
        "tts_voice": preferred.get("tts_voice"), "wake_word_entity": None, "wake_word_id": None,
        "prefer_local_intents": False,
    }
    if set(pipeline) != PIPELINE_KEYS:
        raise SetupError("PIPELINE_FIELDS")
    made = ha.ws_once({"type": "assist_pipeline/pipeline/create", **pipeline}) or {}
    facts["pipeline"] = {"id": made.get("id"), "created": True}
    return facts


def run_setup(engine, req: CompanionRequest, data_dir: str, dry_run: bool):
    from .engine import REJECTED, TIMED_OUT, Result

    engine.j.audit(req.request_id, "RECEIVED", digest=req.digest, requested_by=req.requested_by, kind="companion_setup")
    key = read_key(data_dir)
    if not key:
        return Result(SETUP_FAILED, ["No Companion key on the Pi yet: file a Companion key request first."])
    message = (f"{req.request_id} from {req.requested_by}\nSet up the House Brain Companion in Home Assistant:\n"
               f"- add llama.cpp for {req.url} (model {COMPANION_MODEL}) with the key made on the Pi\n"
               "- its agent gets NO device control (explain only)\n"
               f"- add the voice assistant \"{COMPANION_PIPELINE}\"\nNo files, no restart.")
    outcome = engine._ask(req.request_id, "DEPLOY", "House Brain Deployer: set up the Companion?", message,
                          engine.s.timing.approval_timeout)
    if outcome != approval.APPROVE:
        return Result(REJECTED if outcome == approval.REJECT else TIMED_OUT, [outcome])
    if dry_run:
        return Result("DRY_RUN_OK", ["Dry-run mode: nothing was set up."], {"would_do": ["llama.cpp entry",
                      "agent without control", "pipeline"], "companion_key_sha256": fingerprint(key)})
    net.register_secret(key)   # scrubbed from every log line and report, like the Supervisor token
    facts: dict = {"companion_key_sha256": fingerprint(key)}
    progress: dict = {}
    try:
        facts.update(setup(engine.ha, key, req.url, progress=progress))
    except Exception as err:  # noqa: BLE001 - every failure is reported and undone, never retried
        if isinstance(err, SetupError) and err.code == "ALREADY_CONFIGURED":
            return Result(ALREADY_SET_UP, ["A llama.cpp entry for the Companion URL already exists; nothing changed."],
                          facts)
        reasons = [f"setup stopped: {net.redact(str(err))[:300]}"]
        entry = progress.get("entry_id")
        if entry:
            try:
                engine.ha.delete_entry(entry)
                reasons.append(f"the new llama.cpp entry {entry} was deleted again (nothing left behind)")
            except Exception as undo_err:  # noqa: BLE001
                reasons.append(f"UNDO FAILED: delete llama.cpp entry {entry} by hand ({str(undo_err)[:120]})")
        facts["error"] = getattr(err, "code", type(err).__name__)
        facts.update({k: v for k, v in progress.items() if k != "entry_id"})
        return Result(SETUP_FAILED, reasons, facts)
    return Result(SETUP_DONE, ["The Companion is set up. In the Home Assistant app: Assist, choose "
                               f"\"{COMPANION_PIPELINE}\"."], facts)
