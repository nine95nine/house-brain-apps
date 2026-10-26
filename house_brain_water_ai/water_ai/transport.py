"""Read-only transport. Water AI has no hands.

Every call is a GET through the Supervisor proxy:
- `GET /core/api/states`, cut down at once to the exact ids in `entities.READ`, keeping only entity
  id, state and times (attributes are never kept);
- `GET /core/api/history/period/...` for the ids in `entities.HISTORY` only, at most 26 hours, states
  only (`minimal_response`, `no_attributes`).
No POST, no service call, no state write. No redirects, no proxy, bounded responses.
"""
from datetime import datetime, timezone
import http.client
import json
import urllib.error
import urllib.parse
import urllib.request

from .entities import HISTORY, READ, REFUSED_DOMAINS

SUPERVISOR = 'http://supervisor'
MAX_BYTES = 4_000_000
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

    def _get(self, url, timeout=10):
        self.calls += 1
        request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + self.token}, method='GET')
        try:
            with self.opener.open(request, timeout=timeout) as r:
                raw = r.read(MAX_BYTES + 1)
        except http.client.HTTPException:
            raise TransportError('HTTP_PROTOCOL')   # a malformed reply never reaches the loop as a crash
        if len(raw) > MAX_BYTES:
            raise TransportError('RESPONSE_TOO_LARGE')
        return raw

    @staticmethod
    def _json(raw):
        try:
            return json.loads(raw)
        except RecursionError:
            raise TransportError('JSON_TOO_DEEP')   # deeply nested JSON is refused, never a crash

    def states(self):
        """Only the listed ids, only id/state/times (strings, else None): nothing else is ever held in memory
        past this call."""
        result = self._json(self._get(SUPERVISOR + '/core/api/states'))
        if not isinstance(result, list):
            raise TransportError('STATE_LIST_REQUIRED')
        return {r['entity_id']: {k: (r.get(k) if isinstance(r.get(k), str) else None) for k in KEEP} for r in result
                if isinstance(r, dict) and isinstance(r.get('entity_id'), str) and r['entity_id'] in READ}

    def history(self, entity, start, end):
        """Recorder history of one listed series, at most 26 hours: states only, no attributes."""
        if entity not in HISTORY or entity.split('.', 1)[0] in REFUSED_DOMAINS:
            raise TransportError('HISTORY_ENTITY_REFUSED')
        if not (isinstance(start, (int, float)) and isinstance(end, (int, float)) and 0 < end - start <= 26 * 3600):
            raise TransportError('HISTORY_WINDOW_REFUSED')
        iso = lambda t: datetime.fromtimestamp(t, timezone.utc).isoformat()
        query = urllib.parse.urlencode({'filter_entity_id': entity, 'end_time': iso(end)})
        url = f"{SUPERVISOR}/core/api/history/period/{urllib.parse.quote(iso(start))}?{query}" \
              '&minimal_response&no_attributes'
        return self._json(self._get(url, timeout=30))
