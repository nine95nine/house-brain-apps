"""Supervisor ingress only; CSRF-protected local reviews never call equipment."""
from http.server import HTTPServer, BaseHTTPRequestHandler
import hmac
import json
import secrets
from pathlib import Path
from queue import Queue
from threading import Event


class Ingress:
    def __init__(self, status, commands, port=8099):
        self.status=status
        self.commands=commands
        self.csrf=secrets.token_urlsafe(32)
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(3)

            def log_message(self,*args):
                pass

            def respond(self,code,data,kind="application/json"):
                raw=data.encode() if isinstance(data,str) else json.dumps(data,allow_nan=False).encode()
                self.send_response(code)
                self.send_header("Content-Type",kind)
                self.send_header("Cache-Control","no-store")
                self.send_header("X-Content-Type-Options","nosniff")
                self.send_header("Content-Length",str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def admitted(self):
                return self.client_address[0] in {"172.30.32.2","127.0.0.1","::1"}

            def do_GET(self):
                if not self.admitted():
                    return self.respond(403,{"error":"INGRESS_REQUIRED"})
                if self.path in ("/",""):
                    page=(Path(__file__).parent/"dashboard.txt").read_text()
                    return self.respond(200,page.replace("__CSRF__",owner.csrf),"text/html; charset=utf-8")
                if self.path=="/status":
                    return self.respond(200,owner.status())
                return self.respond(404,{"error":"NOT_FOUND"})

            def do_POST(self):
                if not self.admitted() or not hmac.compare_digest(self.headers.get("X-Energy-CSRF",""),owner.csrf):
                    return self.respond(403,{"error":"INGRESS_CSRF_REQUIRED"})
                try:
                    size=int(self.headers.get("Content-Length","0"))
                    if not 0<size<=4096:
                        raise ValueError()
                    payload=json.loads(self.rfile.read(size))
                    if self.path not in {"/pause","/review","/model/rollback"} or not isinstance(payload,dict):
                        raise ValueError()
                    done=Event()
                    result={}
                    owner.commands.put_nowait((self.path,payload,done,result))
                    if not done.wait(3):
                        return self.respond(202,{"status":"PENDING_READBACK"})
                    return self.respond(200 if "error" not in result else 400,result)
                except (ValueError,TypeError):
                    return self.respond(400,{"error":"INVALID_REQUEST"})
                except Exception:
                    return self.respond(503,{"error":"BUSY"})
        self.server=HTTPServer(("0.0.0.0",port),Handler)
        self.server.timeout=2

    def run(self):
        self.server.serve_forever(poll_interval=.5)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
