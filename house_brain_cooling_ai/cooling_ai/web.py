"""Read-only HTTP: the ingress page for the owner, and three reads for the internal network.

Ingress (the Supervisor proxy and loopback only): `/` page, `/status` and `/episodes.json` (the
latest cooling_thermal_episode.v1 records, plant data only).
Internal Supervisor network and loopback (never the LAN): `/health` (house_brain_app_health.v1,
read by Systems AI), `/digest` (one line) and `/proposals` (always empty in 0.1). There is no POST
route: this App takes no commands.
"""
from http.server import HTTPServer, BaseHTTPRequestHandler
import ipaddress
import json
from pathlib import Path

INGRESS_PEERS = {"172.30.32.2", "127.0.0.1", "::1"}
SUPERVISOR_NETWORK = ipaddress.ip_interface("172.30.32.2/23").network
HEALTH_BROKEN = {"CYCLE_OVERDUE", "HEALTH_SELF_CHECK_FAILED"}


def internal_peer(address):
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.is_loopback or ip in SUPERVISOR_NETWORK


class Server:
    def __init__(self, status, health, digest, proposals, episodes, port=8099):
        owner = self
        self.status, self.health, self.digest, self.proposals, self.episodes = status, health, digest, proposals, episodes

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
                if peer not in INGRESS_PEERS:
                    return self.respond(403, {"error": "INGRESS_REQUIRED"})
                if self.path in ("/", ""):
                    page = (Path(__file__).parent / "dashboard.txt").read_text()
                    return self.respond(200, page, "text/html; charset=utf-8")
                if self.path == "/status":
                    return self.respond(200, owner.status())
                if self.path == "/episodes.json":
                    return self.respond(200, owner.episodes())
                return self.respond(404, {"error": "NOT_FOUND"})

            def refuse(self):
                return self.respond(405, {"error": "READ_ONLY_APP"})

            do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = refuse

        self.server = HTTPServer(("0.0.0.0", port), Handler)
        self.server.timeout = 2

    def run(self):
        self.server.serve_forever(poll_interval=.5)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
