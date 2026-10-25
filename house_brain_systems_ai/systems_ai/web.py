"""Read-only HTTP: the ingress page for the owner, and three reads for the internal network.

Ingress (the Supervisor proxy and loopback only): `/` page, `/status` and `/archive/<day>.csv` (one
local day of the cooling archive).
Internal Supervisor network and loopback (never the LAN): `/health` (house_brain_app_health.v1),
`/digest` (the family line Maintenance relays) and `/proposals` (what Maintenance may show under
"Needs your decision"). There is no POST route: this App takes no commands.
"""
from http.server import HTTPServer, BaseHTTPRequestHandler
import ipaddress
import json
import re
from pathlib import Path

INGRESS_PEERS = {"172.30.32.2", "127.0.0.1", "::1"}
SUPERVISOR_NETWORK = ipaddress.ip_interface("172.30.32.2/23").network
HEALTH_BROKEN = {"CYCLE_OVERDUE", "HEALTH_SELF_CHECK_FAILED"}
RE_ARCHIVE = re.compile(r"/archive/(20[0-9]{2}-[01][0-9]-[0-3][0-9])\.csv")
RE_ARCHIVE_JSON = re.compile(r"/archive/(20[0-9]{2}-[01][0-9]-[0-3][0-9])\.json")   # 0.1.2: internal only


def internal_peer(address):
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.is_loopback or ip in SUPERVISOR_NETWORK


class Server:
    def __init__(self, status, health, digest, proposals, archive_csv=None, port=8099, archive_json=None):
        owner = self
        self.status, self.health, self.digest, self.proposals = status, health, digest, proposals
        self.archive_csv = archive_csv
        self.archive_json = archive_json

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(3)

            def log_message(self, *args):
                pass

            def respond(self, code, data, kind="application/json"):
                raw = data.encode() if isinstance(data, str) else json.dumps(data, allow_nan=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                peer = self.client_address[0]
                if self.path in ("/health", "/digest", "/proposals"):
                    if not internal_peer(peer):
                        return self.respond(403, {"error": "INTERNAL_PEER_REFUSED"})
                    if self.path == "/health":
                        doc = owner.health()
                        return self.respond(503 if HEALTH_BROKEN & set(doc["reasons"]) else 200, doc)
                    return self.respond(200, owner.digest() if self.path == "/digest" else owner.proposals())
                match = RE_ARCHIVE_JSON.fullmatch(self.path)
                if match and owner.archive_json is not None:      # 0.1.2: Cooling AI, internal network only
                    if not internal_peer(peer):
                        return self.respond(403, {"error": "INTERNAL_PEER_REFUSED"})
                    try:
                        return self.respond(200, owner.archive_json(match.group(1)))
                    except Exception:  # noqa: BLE001 - a bad day or an unreadable file is a refusal, not a crash
                        return self.respond(404, {"error": "ARCHIVE_DAY_UNAVAILABLE"})
                if peer not in INGRESS_PEERS:
                    return self.respond(403, {"error": "INGRESS_REQUIRED"})
                if self.path in ("/", ""):
                    page = (Path(__file__).parent / "dashboard.txt").read_text()
                    return self.respond(200, page, "text/html; charset=utf-8")
                if self.path == "/status":
                    return self.respond(200, owner.status())
                match = RE_ARCHIVE.fullmatch(self.path)
                if match and owner.archive_csv is not None:
                    try:
                        return self.respond(200, owner.archive_csv(match.group(1)), "text/csv; charset=utf-8")
                    except Exception:  # noqa: BLE001 - a bad day or an unreadable file is a refusal, not a crash
                        return self.respond(404, {"error": "ARCHIVE_DAY_UNAVAILABLE"})
                return self.respond(404, {"error": "NOT_FOUND"})

            def refuse(self):
                return self.respond(405, {"error": "READ_ONLY_APP"})

            do_POST = do_PUT = do_PATCH = do_DELETE = refuse

        self.server = HTTPServer(("0.0.0.0", port), Handler)
        self.server.timeout = 2

    def run(self):
        self.server.serve_forever(poll_interval=.5)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
