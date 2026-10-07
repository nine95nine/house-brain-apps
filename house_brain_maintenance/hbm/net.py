"""Minimal HTTP helper: bounded, no redirects, secret redaction."""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_SECRETS: set[str] = set()


from . import VERSION as _VERSION  # noqa: E402

USER_AGENT = f"house-brain-maintenance/{_VERSION}"

def register_secret(value: str | None) -> None:
    if value and len(value) >= 8:
        _SECRETS.add(value)


def redact(text: str) -> str:
    for secret in _SECRETS:
        text = text.replace(secret, "***")
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(str(record.msg))
        if record.args:
            record.args = tuple(redact(str(a)) for a in record.args)
        return True


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


_OPENER = urllib.request.build_opener(_NoRedirect)


class NetError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(redact(f"HTTP {status}: {message}"[:300]))
        self.status = status


def request(method: str, url: str, headers: dict[str, str] | None = None,
            body: Any = None, timeout: float = 30.0, raw: bool = False,
            raw_body: bytes | None = None, headers_out: dict | None = None) -> tuple[int, Any]:
    data = raw_body
    hdrs = dict(headers or {})
    # 0.5.1 (live 2026-10-05): Cloudflare answered 403 to Python's default "Python-urllib" user agent
    # (the Broker key probe); the Scout and Observer always named themselves. Every request now does.
    hdrs.setdefault("User-Agent", USER_AGENT)
    if body is not None and raw_body is None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            payload = resp.read(MAX_RESPONSE_BYTES + 1)
            status = resp.status
            if headers_out is not None:   # Credential Autopilot: e.g. the PAT expiry header
                headers_out.update({k.lower(): v for k, v in resp.headers.items()})
    except urllib.error.HTTPError as err:
        text = ""
        if headers_out is not None and err.headers is not None:
            headers_out.update({k.lower(): v for k, v in err.headers.items()})
        try:
            text = err.read(2048).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001, S110 - diagnostic only
            pass
        raise NetError(err.code, text or str(err.reason)) from None
    except (urllib.error.URLError, OSError, TimeoutError) as err:
        raise NetError(0, f"{type(err).__name__}: {err}") from None
    if len(payload) > MAX_RESPONSE_BYTES:
        raise NetError(status, "response too large")
    if raw:
        return status, payload
    if not payload:
        return status, None
    try:
        return status, json.loads(payload.decode("utf-8"))
    except ValueError:
        raise NetError(status, "invalid JSON response") from None
