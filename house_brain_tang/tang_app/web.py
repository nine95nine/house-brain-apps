"""Read-only HTTP on 8099 for one route: GET /health (house_brain_app_health.v1), internal Supervisor
network and loopback only. Port 8099 is not published to the host (config.yaml `ports` lists 7500 only).
Every other path is 404 and every other method 405. Tang itself is on 7500 and is not served here.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json

SUPERVISOR_NETWORK = ipaddress.ip_interface('172.30.32.2/23').network
HEALTH_PORT = 8099


def internal_peer(address):
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.is_loopback or ip in SUPERVISOR_NETWORK


class Server:
    def __init__(self, health, port=HEALTH_PORT, host='0.0.0.0'):
        owner = self
        self.health = health

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(5)

            def log_message(self, *args):
                pass

            def respond(self, code, data):
                raw = json.dumps(data, allow_nan=False, sort_keys=True, separators=(',', ':')).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path != '/health':
                    return self.respond(404, {'error': 'NOT_FOUND'})
                if not internal_peer(self.client_address[0]):
                    return self.respond(403, {'error': 'INTERNAL_PEER_REFUSED'})
                doc = owner.health()
                return self.respond(200 if doc.get('health') == 'OK' else 503, doc)

            def refuse(self):
                return self.respond(405, {'error': 'READ_ONLY_APP'})

            do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = refuse

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True

    def run(self):
        self.server.serve_forever(poll_interval=.5)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
