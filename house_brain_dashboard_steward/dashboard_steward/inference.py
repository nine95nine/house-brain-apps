"""Model adapter. Owner decision 2026-11-08 "Local model only": inference runs on a local, $0 open model
(the AI family's T1 slot on the MacBook, served by an OpenAI-compatible local server such as llama.cpp's
``llama-server`` or Ollama). No household data leaves the house; no paid API; no cloud fallback.

Enforced here, not by convention:
* the model URL must be http(s) to loopback, a private RFC 1918 address or the Tailscale/CGNAT range; any
  public host is refused at configuration time (``INFERENCE_HOST_NOT_LOCAL``); no proxy is ever used;
* a per-day call budget, checked and counted before each call (fail closed);
* per-call timeout and response-size caps;
* when no model is configured or it does not answer, the caller gets ``ModelUnavailable`` and reports
  ``AI_DEGRADED``. The service never pretends deterministic work is AI.
"""
from __future__ import annotations

import ipaddress
import json
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_DAILY_CALLS = 48
DEFAULT_TIMEOUT_S = 120
MAX_RESPONSE_BYTES = 256 * 1024
MAX_PROMPT_CHARS = 48_000


class ModelUnavailable(Exception):
    def __init__(self, code: str, detail: str = ""):
        self.code, self.detail = code, detail
        super().__init__(f"{code}: {detail}")


class BudgetExceeded(ModelUnavailable):
    pass


def check_local_url(url: str) -> str:
    """Return the normalized base URL or raise ValueError(INFERENCE_HOST_NOT_LOCAL / INFERENCE_URL_INVALID)."""
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise ValueError("INFERENCE_URL_INVALID")
    host = u.hostname
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        if host != "localhost":
            # Names are refused: a name can be re-pointed at a public host after this check.
            raise ValueError("INFERENCE_HOST_NOT_LOCAL") from None
        ip = ipaddress.ip_address("127.0.0.1")
    cgnat = ipaddress.ip_network("100.64.0.0/10")
    if not (ip.is_loopback or (ip.version == 4 and (ip.is_private or ip in cgnat)) or
            (ip.version == 6 and ip in ipaddress.ip_network("fc00::/7"))):
        raise ValueError("INFERENCE_HOST_NOT_LOCAL")
    if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        raise ValueError("INFERENCE_HOST_NOT_LOCAL")
    return url.rstrip("/")


class NoModel:
    """No qualified model configured. Every call reports AI_DEGRADED with this reason."""
    name = "none"

    def __init__(self, reason: str = "NO_MODEL_CONFIGURED"):
        self.reason = reason

    def probe(self) -> tuple[bool, str]:
        return False, self.reason

    def complete(self, system: str, user: str) -> str:
        raise ModelUnavailable(self.reason)


class LocalChatModel:
    """OpenAI-compatible ``/v1/chat/completions`` on a local server. stdlib only, no proxy, no redirects."""

    def __init__(self, base_url: str, model: str, *, budget=None, timeout: float = DEFAULT_TIMEOUT_S,
                 opener=None):
        self.base = check_local_url(base_url)
        if not model or len(model) > 120 or any(c in model for c in "\n\r\"'"):
            raise ValueError("INFERENCE_MODEL_NAME_INVALID")
        self.name = model
        self.timeout = timeout
        self.budget = budget            # callable() -> bool (counts one call; False = refused)
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def _post(self, path: str, body: dict) -> dict:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        return self._read(req)

    def _read(self, req) -> dict:
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                raw = r.read(MAX_RESPONSE_BYTES + 1)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ModelUnavailable("MODEL_UNREACHABLE", str(e)[:120]) from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ModelUnavailable("MODEL_RESPONSE_TOO_LARGE")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ModelUnavailable("MODEL_RESPONSE_NOT_JSON") from None

    def probe(self) -> tuple[bool, str]:
        try:
            out = self._read(urllib.request.Request(self.base + "/v1/models", method="GET"))
        except ModelUnavailable as e:
            return False, e.code
        ids = [m.get("id") for m in out.get("data", []) if isinstance(m, dict)] if isinstance(out, dict) else []
        return (True, "MODEL_READY") if self.name in ids or not ids else (False, "MODEL_NOT_LOADED")

    def complete(self, system: str, user: str) -> str:
        if len(system) + len(user) > MAX_PROMPT_CHARS:
            raise ModelUnavailable("PROMPT_TOO_LARGE")
        if self.budget is not None and not self.budget():
            raise BudgetExceeded("MODEL_DAILY_BUDGET_SPENT")
        t0 = time.monotonic()
        out = self._post("/v1/chat/completions", {
            "model": self.name, "temperature": 0.2, "max_tokens": 4096, "stream": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
        try:
            text = out["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise ModelUnavailable("MODEL_RESPONSE_SHAPE") from None
        if not isinstance(text, str) or not text.strip():
            raise ModelUnavailable("MODEL_EMPTY_RESPONSE")
        self.last_latency_s = time.monotonic() - t0
        return text


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        raise urllib.error.URLError("REDIRECT_REFUSED")
