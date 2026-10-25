"""Read-only transport. Cooling AI has no hands.

Every call is a GET:
- `GET /core/api/states` through the Supervisor proxy, cut down at once to the exact ids in
  `entities.READ`, and to entity id, state and times (attributes are never kept);
- `GET /addons/self/info` and `GET /addons/<prefix>_house_brain_systems_ai/info` (role `default`),
  cut down to slug, state, hostname and ingress port;
- `GET http://<prefix>-house-brain-systems-ai:<port>/archive/YYYY-MM-DD.json` on the internal
  Supervisor network (no token is sent).
No POST, no service call, no state write. No redirects, no proxy, bounded responses.
"""
import http.client
import json
import math
import re
import urllib.error
import urllib.request

from .entities import READ

SUPERVISOR = 'http://supervisor'
MAX_BYTES = 4_000_000
RE_SELF = re.compile(r'([0-9a-f]{8}|local)_house_brain_cooling_ai')
RE_DAY = re.compile(r'20[0-9]{2}-[01][0-9]-[0-3][0-9]')
ARCHIVE_SCHEMA = 'house_brain_systems_ai_archive_day.v1'
KEEP = ('entity_id', 'state', 'last_changed', 'last_updated')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ValueError('REDIRECT_REFUSED')


class TransportError(Exception):
    pass


class Client:
    def __init__(self, token, opener=None):
        if not token:
            raise ValueError('SUPERVISOR_TOKEN_REQUIRED')
        self.token = token
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.calls = 0
        self.prefix = None

    def _get(self, url, timeout=10, auth=True):
        headers = {'Authorization': 'Bearer ' + self.token} if auth else {}
        self.calls += 1
        try:
            with self.opener.open(urllib.request.Request(url, headers=headers, method='GET'), timeout=timeout) as r:
                raw = r.read(MAX_BYTES + 1)
        except http.client.HTTPException:
            raise TransportError('HTTP_PROTOCOL')   # a malformed reply never reaches the loop as a crash
        if len(raw) > MAX_BYTES:
            raise TransportError('RESPONSE_TOO_LARGE')
        return raw

    def states(self):
        """Only the listed ids, only id/state/times: nothing else is ever held in memory past this call."""
        result = json.loads(self._get(SUPERVISOR + '/core/api/states'))
        if not isinstance(result, list):
            raise TransportError('STATE_LIST_REQUIRED')
        return {r['entity_id']: {k: r.get(k) for k in KEEP} for r in result
                if isinstance(r, dict) and r.get('entity_id') in READ}

    def _info(self, slug):
        body = json.loads(self._get(f'{SUPERVISOR}/addons/{slug}/info'))
        if not isinstance(body, dict) or body.get('result') != 'ok' or not isinstance(body.get('data'), dict):
            raise TransportError('SUPERVISOR_RESULT')
        d = body['data']
        return {k: d.get(k) for k in ('slug', 'state', 'hostname', 'ingress_port')}

    def systems_ai(self):
        """(hostname, port) of a started Systems AI from the same store, or None."""
        if self.prefix is None:
            m = RE_SELF.fullmatch(str(self._info('self').get('slug')))
            if not m:
                raise TransportError('SELF_SLUG_UNEXPECTED')
            self.prefix = m.group(1)
        info = self._info(f'{self.prefix}_house_brain_systems_ai')
        host, port = info.get('hostname'), info.get('ingress_port')
        expected = f'{self.prefix}-house-brain-systems-ai' if self.prefix != 'local' else 'local-house-brain-systems-ai'
        if info.get('state') != 'started' or host != expected or type(port) is not int or not 1 <= port <= 65535:
            return None
        return host, port

    def archive_day(self, target, day):
        """One Systems AI archive day. A 404 is never "the day is gone": the route may be missing (a
        Systems AI before 0.1.2) or the read may have failed, so it is ARCHIVE_ROUTE_MISSING or
        ARCHIVE_DAY_UNREADABLE and the day is tried again later."""
        if not isinstance(day, str) or not RE_DAY.fullmatch(day):
            raise TransportError('ARCHIVE_DAY_REFUSED')
        host, port = target
        try:
            doc = json.loads(self._get(f'http://{host}:{port}/archive/{day}.json', timeout=30, auth=False),
                             parse_constant=_refuse_constant)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                try:
                    body = json.loads(error.read(4096))
                except (ValueError, OSError, http.client.HTTPException):
                    body = {}
                missing = isinstance(body, dict) and body.get('error') == 'NOT_FOUND'
                raise TransportError('ARCHIVE_ROUTE_MISSING' if missing else 'ARCHIVE_DAY_UNREADABLE')
            raise TransportError('ARCHIVE_HTTP')
        if not isinstance(doc, dict) or doc.get('schema_version') != ARCHIVE_SCHEMA or doc.get('day') != day \
                or not isinstance(doc.get('rows'), list) or not isinstance(doc.get('status'), dict):
            raise TransportError('ARCHIVE_SHAPE')
        for row in doc['rows']:
            if not isinstance(row, list) or len(row) != 5 or not isinstance(row[0], str) \
                    or isinstance(row[1], bool) or not isinstance(row[1], (int, float)) or not math.isfinite(row[1]) \
                    or row[4] not in ('5min', 'edge') or not (row[2] is None or isinstance(row[2], (int, float, str))) \
                    or not (row[3] is None or isinstance(row[3], str)):
                raise TransportError('ARCHIVE_ROW_SHAPE')
        return doc


def _refuse_constant(name):
    raise ValueError('ARCHIVE_NON_FINITE')
