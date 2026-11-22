"""Read-only status page (Home Assistant ingress). GET only; there is no decision or write endpoint here:
owner decisions arrive through the owner pop-up / a later, separately reviewed App page with CSRF protection.

0.1.1 pages (display of the last gated live read; nothing here calls Home Assistant):
``/coverage`` and ``/coverage.json`` (M2), ``/data-health`` and ``/data-health.json`` (M1), ``/usage`` and
``/usage.json`` (M3), ``/export`` with ``/export/<url_path>.json`` and ``.yaml`` downloads (owner pop-up
2026-11-22 "Both"). While the kill switch holds, every one of them answers 503 HELD. Links are relative so
they work behind the ingress prefix.
"""
from __future__ import annotations

import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import coverage as COV


MAX_BODY = 2 * 1024 * 1024
MAX_EXPORT = 8 * 1024 * 1024       # same ceiling as one WebSocket message: an export never truncates silently
HELD_PATHS = ("/coverage", "/data-health", "/usage", "/export")
_E = html.escape


# 0.1.2: explicit colors, the house Apps' dark style. Without them the page is transparent inside Home Assistant's
# dark ingress panel and its default black text is invisible (owner report 2026-11-22, iPhone app).
STYLE = ("<meta name=color-scheme content=dark><style>:root{color-scheme:dark}"
         "html,body{background:#111;color:#eee}"
         "body{font-family:-apple-system,system-ui,sans-serif;margin:0;padding:16px}"
         "a{color:#90caf9}h1{font-size:20px}h2{font-size:17px;margin-top:22px}summary{cursor:pointer}"
         "td,th{padding:4px 6px;text-align:left;vertical-align:top}table{border-collapse:collapse}"
         "tr{border-bottom:1px solid #333}small{color:#aaa}</style>")
HTML_PATHS = ("/", "", "/coverage", "/data-health", "/usage", "/export")


def _page(title: str, body: str) -> bytes:
    return (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
            f"<title>{_E(title)}</title>{STYLE}<p><a href='./'>Steward</a> · <a href='coverage'>Coverage</a> · <a href='data-health'>Data "
            f"health</a> · <a href='usage'>Usage</a> · <a href='export'>Export</a></p><h1>{_E(title)}</h1>{body}"
            f"<p><small>Read only. Control authority: NONE.</small></p>").encode("utf-8")


def _age(s):
    if s is None:
        return "unknown"
    return f"{s // 86400} d {s % 86400 // 3600} h" if s >= 86400 else f"{s // 3600} h {s % 3600 // 60} min"


def coverage_html(cov: dict) -> bytes:
    if not cov or cov.get("status") != "OK":
        return _page("Coverage", f"<p>{_E(str((cov or {}).get('status', 'NOT_READ')))}: main dashboard "
                                 f"<b>{_E(str((cov or {}).get('main', '')))}</b>.</p>")
    rows = "".join(
        f"<tr><td>{_E(k)}</td><td>{d['refs']}</td><td>{d['dead']}</td><td>{d['covered']}</td>"
        f"<td>{len(d['missing'])}</td><td>{len(d['missing_controls'])}</td><td>{'yes' if d['ready'] else 'no'}</td></tr>"
        for k, d in sorted(cov["dashboards"].items()))
    detail = "".join(
        f"<details><summary>{_E(k)}: {len(d['missing'])} missing</summary><p>"
        + "<br>".join(_E(e) + (" <b>(control)</b>" if e in d["missing_controls"] else "") for e in d["missing"])
        + "</p></details>" for k, d in sorted(cov["dashboards"].items()) if d["missing"])
    return _page("Coverage", f"<p>Against the main dashboard <b>{_E(cov['main'])}</b>. Missing in total: "
                 f"{cov['total_missing']} (controls {cov['total_missing_controls']}). Dead = {_E(cov['dead_rule'])}."
                 f" Ready is necessary, not sufficient: deleting still needs your pop-up and an archive copy.</p>"
                 f"<table><tr><th>Dashboard</th><th>Refs</th><th>Dead</th><th>Covered</th><th>Missing</th>"
                 f"<th>Controls</th><th>Ready</th></tr>{rows}</table>{detail}"
                 f"<p><a href='coverage.json'>JSON</a></p>")


def data_health_html(dh: dict) -> bytes:
    if not dh or dh.get("status") != "OK":
        return _page("Data health", f"<p>{_E(str((dh or {}).get('status', 'NOT_READ')))}</p>")
    rows = "".join(f"<tr><td>{_E(r['entity_id'])}</td><td>{_E(r['state'])}</td><td>{_age(r['for_s'])}</td></tr>"
                   for r in dh["entities"])
    return _page("Data health", f"<p>{dh['bad']} of {dh['refs']} entities on <b>{_E(dh['dashboard'])}</b> are "
                 f"unavailable, unknown or missing ({dh['over_24h']} for more than 24 h or since unknown).</p>"
                 f"<table><tr><th>Entity</th><th>State</th><th>For</th></tr>{rows}</table>"
                 f"<p><a href='data-health.json'>JSON</a></p>")


def usage_html(u: dict) -> bytes:
    parts = []
    for cat in COV.TIME_CATEGORIES + ("unknown",):
        c = u.get("by_category", {}).get(cat)
        if not c:
            continue
        parts.append(f"<h2>{_E(cat)}</h2><ol>" + "".join(
            f"<li>{_E(e['entity_id'])} <small>({_E(e['placement'].replace('_', ' '))})</small></li>"
            for e in c["entities"]) + "</ol>")
    return _page("Usage", f"<p>Status: {_E(u.get('status', ''))} {_E(u.get('reason', ''))}</p>"
                 f"<p><small>{_E(u.get('user_scope_note', ''))}</small></p>"
                 + ("".join(parts) or "<p>No list read yet. One time of day is read per hour.</p>")
                 + f"<p>Pins (first view of the main dashboard): {len(u.get('pins', []))}. "
                 f"<a href='usage.json'>JSON</a></p>")


def export_index_html(pub: dict) -> bytes:
    rows = "".join(f"<tr><td>{_E(k)}</td><td><a href='export/{_E(k)}.yaml' download>YAML</a></td>"
                   f"<td><a href='export/{_E(k)}.json' download>JSON</a></td></tr>"
                   for k in sorted(pub.get("configs") or {}) if COV.RE_URL_PATH.match(k))
    return _page("Export", "<p>Download a dashboard's full config as last read live. The file is household "
                 "data: it is saved only where you choose. Secret-looking values are replaced by &lt;set&gt; and "
                 "the file carries the live hash so a repository copy can be checked.</p>"
                 f"<table><tr><th>Dashboard</th><th></th><th></th></tr>{rows}</table>")


def make_handler(steward):
    class Handler(BaseHTTPRequestHandler):
        server_version = "DashboardSteward"
        sys_version = ""

        def log_message(self, *a):
            return

        def _send(self, code, body: bytes, ctype, attachment: str = "", limit: int = MAX_BODY):
            if len(body) > limit:
                code, body, ctype, attachment = 507, b"too large", "text/plain", ""
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            if attachment:
                self.send_header("Content-Disposition", f'attachment; filename="{attachment}"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            pub = steward.public()             # published snapshot; this thread never opens the database
            if not pub:
                if path in HTML_PATHS:       # a styled page: plain text is as invisible as an unstyled page
                    return self._send(503, _page("Dashboard Steward", "<p>Starting. Reload in a minute.</p>"),
                                      "text/html; charset=utf-8")
                return self._send(503, b"starting", "text/plain")
            if path == "/health":                   # house_brain_app_health.v1 (Systems AI reads this)
                return self._send(200, json.dumps(pub["app_health"], sort_keys=True).encode(), "application/json")
            if path == "/status":                   # the four-part detail: service, observation, inference, deployment
                return self._send(200, json.dumps(pub["health"], sort_keys=True).encode(), "application/json")
            if path == "/proposals":
                return self._send(200, json.dumps(pub["digest"], sort_keys=True).encode(), "application/json")
            if path.startswith("/proposals/") and path.endswith(".yaml"):
                y = pub["proposal_yaml"].get(path[len("/proposals/"):-5])
                if y is None:
                    return self._send(404, b"not found", "text/plain")
                return self._send(200, y.encode(), "text/plain; charset=utf-8")
            if path.startswith(HELD_PATHS) and pub.get("hold"):
                if path in HTML_PATHS:
                    return self._send(503, _page("Held", f"<p>HELD: {_E(str(pub['hold']))}. The kill switch holds "
                                                 "this page.</p>"), "text/html; charset=utf-8")
                return self._send(503, f"HELD: {pub['hold']}".encode(), "text/plain")
            js = "application/json"
            if path == "/coverage.json":
                return self._send(200, json.dumps(pub.get("coverage"), sort_keys=True).encode(), js)
            if path == "/coverage":
                return self._send(200, coverage_html(pub.get("coverage")), "text/html; charset=utf-8")
            if path == "/data-health.json":
                return self._send(200, json.dumps(pub.get("data_health"), sort_keys=True).encode(), js)
            if path == "/data-health":
                return self._send(200, data_health_html(pub.get("data_health")), "text/html; charset=utf-8")
            if path == "/usage.json":
                return self._send(200, json.dumps(pub.get("usage"), sort_keys=True).encode(), js)
            if path == "/usage":
                return self._send(200, usage_html(pub.get("usage") or {}), "text/html; charset=utf-8")
            if path == "/export":
                return self._send(200, export_index_html(pub), "text/html; charset=utf-8")
            if path.startswith("/export/"):
                name, _, ext = path[len("/export/"):].rpartition(".")
                cfg = (pub.get("configs") or {}).get(name)
                if ext not in ("json", "yaml") or not COV.RE_URL_PATH.match(name) or cfg is None:
                    return self._send(404, b"not found", "text/plain")
                doc = COV.export(name, cfg, pub["configs_at"], pub.get("version", ""))
                if ext == "json":
                    return self._send(200, COV.export_json(doc), js, f"{name}.json", MAX_EXPORT)
                try:
                    body = COV.export_yaml(doc)
                except ValueError:
                    return self._send(422, b"YAML emitter refused this config; use the JSON download", "text/plain")
                return self._send(200, body, "application/yaml; charset=utf-8", f"{name}.yaml", MAX_EXPORT)
            if path == "/debt":
                return self._send(200, json.dumps(pub["debt"], sort_keys=True).encode(), "application/json")
            if path in ("/", ""):
                h = pub["health"]
                rows = "".join(f"<tr><th>{html.escape(k)}</th><td>{html.escape(str(h[k].get('status')))}</td></tr>"
                               for k in ("service", "observation", "inference", "deployment"))
                body = (f"<p>Overall: <b>{html.escape(h['overall'])}</b></p>"
                        f"<table>{rows}</table><p>Pending proposals: {h['proposals']['pending']}</p>")
                return self._send(200, _page("Dashboard Steward", body), "text/html; charset=utf-8")
            return self._send(404, b"not found", "text/plain")

        def do_POST(self):
            return self._send(405, b"read only", "text/plain")

        do_PUT = do_DELETE = do_PATCH = do_POST
    return Handler


def serve(steward, host="0.0.0.0", port=8099):
    srv = ThreadingHTTPServer((host, port), make_handler(steward))
    srv.daemon_threads = True
    return srv
