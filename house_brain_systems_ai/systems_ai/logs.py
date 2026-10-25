"""Home Assistant system log: message templates (Drain-style, bounded) and error baselines.

Messages are masked before anything is kept: numbers, hex, IP addresses, UUIDs, URLs, e-mail
addresses and entity ids become placeholders, so a template never holds a reading, an address or
a name. Templates stay in this App's own database and show only on its own page; the digest and
/health carry counts only.
"""
import hashlib
import re
import time
from datetime import datetime, timedelta, timezone

from .learn import robust_z

LEVELS = {'WARNING', 'ERROR', 'CRITICAL'}
MAX_TEMPLATES = 2000
MAX_TOKENS = 40
SIMILARITY = .5
KEEP_DAYS = 30
PHOENIX = timezone(timedelta(hours=-7))  # America/Phoenix, no daylight saving time
MASKS = [(re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'), '<EMAIL>'),
         (re.compile(r'\b[a-z]+://\S+'), '<URL>'),
         (re.compile(r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b'), '<UUID>'),
         (re.compile(r'\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?::[0-9]+)?\b'), '<IP>'),
         (re.compile(r'\b(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}\b'), '<MAC>'),
         (re.compile(r'\b[a-z_]+\.[a-z0-9_]+\b'), '<ENTITY>'),
         (re.compile(r'\b(?:0[xX])?[0-9a-fA-F]{8,}\b'), '<HEX>'),
         (re.compile(r'[-+]?\b[0-9]+(?:[.,][0-9]+)*\b'), '<N>')]


def mask(text):
    text = str(text)[:500]
    for pattern, token in MASKS:
        text = pattern.sub(token, text)
    return text


def integration(logger):
    """`homeassistant.components.<x>` or `custom_components.<x>` -> x; otherwise the logger's root."""
    parts = str(logger).split('.')
    for marker in ('components', 'custom_components'):
        if marker in parts and parts.index(marker) + 1 < len(parts):
            return parts[parts.index(marker) + 1][:48]
    return (parts[0] or 'unknown')[:48]


def local_day(at):
    return datetime.fromtimestamp(at, PHOENIX).date().isoformat()


class LogLearner:
    def __init__(self, store):
        self.store = store
        self.templates = store.get('log:templates', [])
        self.seen = store.get('log:seen', {})
        self.days = store.get('log:days', {})
        self.started = store.get('log:started', None)

    def _template(self, logger, text, now):
        tokens = mask(text).split()[:MAX_TOKENS]
        if not tokens:
            tokens = ['<EMPTY>']
        best, best_score = None, 0.
        for t in self.templates:
            if t['logger'] != logger or len(t['tokens']) != len(tokens):
                continue
            same = sum(a == b or a == '<*>' for a, b in zip(t['tokens'], tokens)) / len(tokens)
            if same > best_score:
                best, best_score = t, same
        if best is not None and best_score >= SIMILARITY:
            best['tokens'] = [a if a == b else '<*>' for a, b in zip(best['tokens'], tokens)]
            best['last_seen'] = now
            return best
        new = {'id': hashlib.sha256((logger + ' '.join(tokens)).encode()).hexdigest()[:12], 'logger': logger,
               'tokens': tokens, 'count': 0, 'first_seen': now, 'last_seen': now}
        self.templates.append(new)
        if len(self.templates) > MAX_TEMPLATES:
            self.templates.sort(key=lambda t: t['last_seen'])
            del self.templates[:len(self.templates) - MAX_TEMPLATES]
        return new

    def observe(self, entries, now):
        """Fold one system_log/list read in. Counts only grow from each entry's own counter."""
        if self.started is None:
            self.started = now
        if not isinstance(entries, list):
            raise ValueError('SYSTEM_LOG_SHAPE')
        today = local_day(now)
        counts = self.days.setdefault(today, {})
        fresh = {}
        for e in entries[:500]:
            if not isinstance(e, dict) or e.get('level') not in LEVELS:
                continue
            logger = str(e.get('name') or 'unknown')[:120]
            message = e.get('message')
            text = message[0] if isinstance(message, list) and message else str(message or '')
            ident = hashlib.sha256(f"{logger}|{e.get('source')}|{text}".encode()).hexdigest()[:16]
            count = e.get('count') if type(e.get('count')) is int and e.get('count') > 0 else 1
            before = self.seen.get(ident)
            delta = count - before if before is not None and count >= before else count
            fresh[ident] = count
            if delta <= 0:
                continue
            template = self._template(logger, text, now)
            template['count'] += delta
            name = integration(logger)
            counts[name] = counts.get(name, 0) + delta
        # Entries rotate out of the 50-entry log; keep only identities seen in this read.
        self.seen = fresh
        for day in sorted(self.days)[:-KEEP_DAYS]:
            self.days.pop(day)
        self.store.put('log:templates', self.templates)
        self.store.put('log:seen', self.seen)
        self.store.put('log:days', self.days)
        self.store.put('log:started', self.started)

    def report(self, now):
        today = local_day(now)
        past = [d for d in sorted(self.days) if d < today][-14:]
        unusual = []
        names = {n for d in past + [today] for n in self.days.get(d, {})}
        for name in sorted(names):
            history = [self.days[d].get(name, 0) for d in past]
            if past:
                z = robust_z(history[:-1], history[-1]) if len(history) > 7 else None
                if z is not None and z > 4 and history[-1] >= 20:
                    unusual.append({'integration': name, 'day': past[-1], 'count': history[-1], 'robust_z': round(z, 1)})
            so_far = self.days.get(today, {}).get(name, 0)
            typical = sorted(history)[len(history) // 2] if history else 0
            if len(history) >= 7 and so_far >= 50 and so_far > 3 * max(1, typical):
                unusual.append({'integration': name, 'day': today, 'count': so_far, 'robust_z': None,
                                'note': 'today so far, over 3x a typical day'})
        warmed = self.started is not None and now - self.started >= 86400
        new = [t for t in self.templates if warmed and now - t['first_seen'] <= 86400]
        top = sorted(self.days.get(today, {}).items(), key=lambda kv: -kv[1])[:10]
        return {'status': 'LEARNING' if not warmed else 'WATCHING', 'templates': len(self.templates),
                'new_templates_24h': [{'id': t['id'], 'integration': integration(t['logger']), 'count': t['count'],
                                       'template': ' '.join(t['tokens'])[:300]} for t in new[-20:]],
                'unusual_integrations': unusual[:20], 'today_top': [{'integration': k, 'count': v} for k, v in top],
                'days_kept': len(self.days)}
