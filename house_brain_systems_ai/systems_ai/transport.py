"""Read-only transport. Fixed Supervisor routes, sibling /health reads, one Core WebSocket.

Every call is a GET (states, `.../info`, the archive entities' recorder history) or a read-only
WebSocket command from a fixed allowlist. There is no POST, no service call and no state write anywhere in this App: Systems AI has no hands. Supervisor
access is the `default` role, which only answers `.../info` routes. No redirects, no proxy,
bounded responses.
"""
import base64
import json
import os
import re
import socket
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .archive import allowed as archive_allowed

SUPERVISOR = 'http://supervisor'
MAX_BYTES = 4_000_000
MAX_WS_BYTES = 4 * 1024 * 1024
STATIC_INFO = frozenset({'/info', '/host/info', '/resolution/info', '/addons/self/info'})
RE_ADDON_INFO = re.compile(r'/addons/(?:local|[0-9a-f]{8})_house_brain_[a-z0-9_]{1,48}/info')
RE_HOSTNAME = re.compile(r'[a-z0-9][a-z0-9-]{0,62}')
# Read-only Core WebSocket commands, each with exactly these keys.
WS_ALLOW = {'system_log/list': {'type'}, 'repairs/list_issues': {'type'},
            'system_health/info': {'type'}, 'recorder/list_statistic_ids': {'type'}}
# Fields kept from a sibling App's info; everything else (its options among them) is dropped at once.
ADDON_FIELDS = ('slug', 'name', 'version', 'version_latest', 'update_available', 'state', 'boot',
                'hostname', 'ingress_port', 'watchdog')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('REDIRECT_REFUSED')


class TransportError(Exception):
    pass


def project_addon(data):
    if not isinstance(data, dict):
        raise TransportError('ADDON_INFO_SHAPE')
    return {k: data.get(k) for k in ADDON_FIELDS}


class Client:
    def __init__(self, token, opener=None, connect=None):
        if not token:
            raise ValueError('SUPERVISOR_TOKEN_REQUIRED')
        self.token = token
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.connect = connect or socket.create_connection
        self.calls = 0
        self.socket = None
        self.retry_at = 0.

    def _get(self, url, timeout=10, auth=True):
        headers = {'Authorization': 'Bearer ' + self.token} if auth else {}
        request = urllib.request.Request(url, headers=headers, method='GET')
        self.calls += 1
        with self.opener.open(request, timeout=timeout) as response:
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise TransportError('RESPONSE_TOO_LARGE')
        return raw

    def states(self):
        result = json.loads(self._get(SUPERVISOR + '/core/api/states'))
        if not isinstance(result, list):
            raise TransportError('STATE_LIST_REQUIRED')
        return {r['entity_id']: r for r in result if isinstance(r, dict) and isinstance(r.get('entity_id'), str)}

    def info(self, path):
        """GET a Supervisor `.../info` route; sibling App info is projected to ADDON_FIELDS."""
        if path not in STATIC_INFO and not RE_ADDON_INFO.fullmatch(path):
            raise TransportError('ENDPOINT_REFUSED')
        body = json.loads(self._get(SUPERVISOR + path))
        if not isinstance(body, dict) or body.get('result') != 'ok' or not isinstance(body.get('data'), dict):
            raise TransportError('SUPERVISOR_RESULT')
        data = body['data']
        return project_addon(data) if path.startswith('/addons/') else data

    def health(self, hostname, port):
        """A sibling's house_brain_app_health.v1 body (bytes), or None when it does not answer."""
        if not isinstance(hostname, str) or not RE_HOSTNAME.fullmatch(hostname) or type(port) is not int \
                or not 1 <= port <= 65535:
            raise TransportError('HEALTH_TARGET_REFUSED')
        try:
            return self._get(f'http://{hostname}:{port}/health', timeout=5, auth=False)
        except urllib.error.HTTPError as error:
            # 503 still carries the block (the App's loop is broken); read it, bounded.
            raw = error.read(4097)
            return raw if error.code == 503 else None
        except (OSError, ValueError, TransportError):
            return None

    def history(self, entity, start, end):
        """Recorder history of one archive entity, at most 26 hours: states only, no attributes."""
        if not archive_allowed(entity):
            raise TransportError('HISTORY_ENTITY_REFUSED')
        if not (isinstance(start, (int, float)) and isinstance(end, (int, float)) and 0 < end - start <= 26 * 3600):
            raise TransportError('HISTORY_WINDOW_REFUSED')
        iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
        query = urllib.parse.urlencode({'filter_entity_id': entity, 'end_time': iso(end)})
        url = f"{SUPERVISOR}/core/api/history/period/{urllib.parse.quote(iso(start))}?{query}" \
              '&minimal_response&no_attributes'
        return json.loads(self._get(url, timeout=30))

    # -- Core WebSocket: one long-lived socket, reconnect with backoff ----------------------
    def ws(self, message, timeout=20):
        if not isinstance(message, dict) or message.get('type') not in WS_ALLOW \
                or set(message) != WS_ALLOW[message['type']]:
            raise TransportError('WS_COMMAND_REFUSED')
        if self.socket is None:
            if time.monotonic() < self.retry_at:
                raise TransportError('WS_BACKOFF')
            try:
                self.socket = CoreSocket(self.connect, self.token)
            except (OSError, ValueError, TransportError):
                self.retry_at = time.monotonic() + 60
                raise TransportError('WS_CONNECT')
        try:
            self.calls += 1
            if message['type'] == 'system_health/info':
                return self.socket.system_health(timeout)
            return self.socket.command(message, timeout)
        except (OSError, ValueError, TransportError):
            self.close()
            raise TransportError('WS_READ')

    def close(self):
        if self.socket is not None:
            self.socket.close()
            self.socket = None


class CoreSocket:
    """Minimal RFC 6455 client (text frames, masked, bounded) for ws://supervisor/core/websocket."""

    def __init__(self, connect, token, host='supervisor', path='/core/websocket'):
        self.sock = connect((host, 80), 10)
        self.buffer = b''
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f'GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                           f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
        head = self._until(b'\r\n\r\n', 16384)
        if not head.startswith(b'HTTP/1.1 101'):
            raise TransportError('WS_HANDSHAKE')
        self.next_id = 1
        if self.recv(20).get('type') != 'auth_required':
            raise TransportError('WS_HANDSHAKE')
        self.send({'type': 'auth', 'access_token': token})
        if self.recv(20).get('type') != 'auth_ok':
            raise TransportError('WS_AUTH')

    def _read(self, n, deadline):
        while len(self.buffer) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransportError('WS_TIMEOUT')
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(65536)
            if not chunk:
                raise TransportError('WS_CLOSED')
            self.buffer += chunk
        data, self.buffer = self.buffer[:n], self.buffer[n:]
        return data

    def _until(self, marker, limit):
        deadline = time.monotonic() + 10
        while marker not in self.buffer:
            if len(self.buffer) > limit:
                raise TransportError('WS_HANDSHAKE')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransportError('WS_TIMEOUT')
            self.sock.settimeout(remaining)
            chunk = self.sock.recv(4096)
            if not chunk:
                raise TransportError('WS_CLOSED')
            self.buffer += chunk
        head, self.buffer = self.buffer.split(marker, 1)
        return head

    def send(self, message):
        payload = json.dumps(message).encode()
        n = len(payload)
        if n < 126:
            header = bytes([0x81, 0x80 | n])
        elif n < 65536:
            header = bytes([0x81, 0x80 | 126]) + struct.pack('!H', n)
        else:
            header = bytes([0x81, 0x80 | 127]) + struct.pack('!Q', n)
        mask = os.urandom(4)
        self.sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def recv(self, timeout):
        deadline = time.monotonic() + timeout
        parts, total = [], 0
        while True:
            b0, b1 = self._read(2, deadline)
            opcode, length = b0 & 0x0F, b1 & 0x7F
            if b1 & 0x80:
                raise TransportError('WS_MASKED_FROM_SERVER')
            if length == 126:
                length = struct.unpack('!H', self._read(2, deadline))[0]
            elif length == 127:
                length = struct.unpack('!Q', self._read(8, deadline))[0]
            total += length
            if total > MAX_WS_BYTES:
                raise TransportError('WS_MESSAGE_TOO_LARGE')
            payload = self._read(length, deadline)
            if opcode == 0x8:
                raise TransportError('WS_CLOSED')
            if opcode == 0x9:  # ping: answer, keep reading
                self._control(0xA, payload)
                total -= length
                continue
            if opcode == 0xA:
                total -= length
                continue
            if opcode not in (0x0, 0x1):
                raise TransportError('WS_OPCODE')
            parts.append(payload)
            if b0 & 0x80:
                data = json.loads(b''.join(parts).decode('utf-8'))
                if not isinstance(data, dict):
                    raise TransportError('WS_SHAPE')
                return data

    def _control(self, opcode, payload):
        mask = os.urandom(4)
        self.sock.sendall(bytes([0x80 | opcode, 0x80 | len(payload)]) + mask
                          + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def command(self, message, timeout=20):
        msg_id = self.next_id
        self.next_id += 1
        self.send({'id': msg_id, **message})
        deadline = time.monotonic() + timeout
        while True:
            data = self.recv(max(.01, deadline - time.monotonic()))
            if data.get('id') == msg_id and data.get('type') == 'result':
                if data.get('success') is not True:
                    raise TransportError('WS_COMMAND_FAILED')
                return data.get('result')

    def system_health(self, timeout=20):
        """The recorder's estimated_db_size from the system_health subscription (Maintenance lineage)."""
        msg_id = self.next_id
        self.command({'type': 'system_health/info'}, timeout)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            data = self.recv(max(.01, deadline - time.monotonic()))
            if data.get('id') != msg_id or data.get('type') != 'event':
                continue
            event = data.get('event') or {}
            if event.get('type') == 'initial':
                info = ((event.get('data') or {}).get('recorder') or {}).get('info') or {}
                if 'estimated_db_size' in info:
                    return {'estimated_db_size': info['estimated_db_size']}
            elif event.get('type') == 'update' and event.get('domain') == 'recorder' \
                    and event.get('key') == 'estimated_db_size' and event.get('success') is not False:
                return {'estimated_db_size': event.get('data')}
            elif event.get('type') == 'finish':
                return {'estimated_db_size': None}
        return {'estimated_db_size': None}

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass
