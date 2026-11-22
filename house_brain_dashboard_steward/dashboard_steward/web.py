"""Read-only status page (Home Assistant ingress later). GET only; there is no decision or write endpoint here:
owner decisions arrive through the owner pop-up / a later, separately reviewed App page with CSRF protection.
"""
from __future__ import annotations

import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


MAX_BODY = 2 * 1024 * 1024


def make_handler(steward):
    class Handler(BaseHTTPRequestHandler):
        server_version = "DashboardSteward"
        sys_version = ""

        def log_message(self, *a):
            return

        def _send(self, code, body: bytes, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(body[:MAX_BODY])

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            pub = steward.public()             # published snapshot; this thread never opens the database
            if not pub:
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
            if path == "/debt":
                return self._send(200, json.dumps(pub["debt"], sort_keys=True).encode(), "application/json")
            if path in ("/", ""):
                h = pub["health"]
                rows = "".join(f"<tr><th>{html.escape(k)}</th><td>{html.escape(str(h[k].get('status')))}</td></tr>"
                               for k in ("service", "observation", "inference", "deployment"))
                body = (f"<!doctype html><meta charset=utf-8><title>Dashboard Steward</title>"
                        f"<h1>Dashboard Steward</h1><p>Overall: <b>{html.escape(h['overall'])}</b></p>"
                        f"<table>{rows}</table><p>Pending proposals: {h['proposals']['pending']}</p>"
                        f"<p>Read only. Control authority: NONE.</p>")
                return self._send(200, body.encode(), "text/html; charset=utf-8")
            return self._send(404, b"not found", "text/plain")

        def do_POST(self):
            return self._send(405, b"read only", "text/plain")

        do_PUT = do_DELETE = do_PATCH = do_POST
    return Handler


def serve(steward, host="0.0.0.0", port=8099):
    srv = ThreadingHTTPServer((host, port), make_handler(steward))
    srv.daemon_threads = True
    return srv
