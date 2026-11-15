"""Findings (Systems AI 0.3.14; design docs/house-brain/A3B_CURIOSITY_MODEL_WATCH_DESIGN_R1_2026-11-15.md §4, owner pop-up
2026-11-15 "Approve and merge").

What the house read this week: the weekly curiosity routine and the AI model watch on the MacBook send findings over
POST /link/v1/findings (link.validate_finding). They are untrusted, not checked, reading material for the owner.

- Kept in Systems AI's own store (the kv table of systems.sqlite; one transaction per write): the newest 50, for
  30 days. Older ones drop off and are counted.
- A repeated id (the same role and week) replaces the earlier finding instead of being added again.
- Applied on the runtime thread only. A finding that was queued when the kill switch held Systems AI is dropped and
  counted, never kept.
- A finding never opens or changes a case, never feeds an investigation, a hypothesis, a playbook match or a
  suggestion, and never reaches O&M, Maintenance or the Deployer: this module is read only by the page's /status.
  Nothing here reads, requests or acts.
"""
from . import link

KEY = 'findings:state'
KEEP = 50
KEEP_DAYS = 30
NOTE = ('Untrusted, not checked: public pages the house read this week, for you to read. A finding never opens a '
        'case, never becomes a hypothesis or a suggestion, and never triggers a download, an install or any action.')


def source_line(items):
    """The 0.3.8 counting: one page is one source, one site alone is never corroboration."""
    sources, sites, dupes = link.independence(items)
    note = ', one site only, not corroborated' if sites == 1 else ''
    dup = f', {dupes} repeat(s) counted once' if dupes else ''
    return f'{sources} source(s) on {sites} site(s){note}{dup}'


class Findings:
    def __init__(self, store):
        self.store = store
        self.state = store.get(KEY) or {'items': {}, 'dropped': 0, 'replaced': 0, 'dropped_held': 0}

    def _save(self):
        self.store.put(KEY, self.state)

    def add(self, found, now):
        """Validated findings (link.validate_finding) into the store; the same id replaces the earlier one."""
        items = self.state['items']
        for f in found:
            if f['id'] in items:
                self.state['replaced'] += 1
            items[f['id']] = {**f, 'at': now}
        self._prune(now)
        self._save()

    def drop_held(self, found):
        """Queued while the kill switch held Systems AI: counted, never kept."""
        self.state['dropped_held'] += len(found)
        self._save()

    def prune(self, now):
        if self._prune(now):
            self._save()

    def _prune(self, now):
        items = self.state['items']
        old = [k for k, f in items.items() if now - f['at'] > KEEP_DAYS * 86400]
        for k in old:
            items.pop(k)
        extra = sorted(items, key=lambda k: (items[k]['at'], k))[:max(0, len(items) - KEEP)]
        for k in extra:
            items.pop(k)
        gone = len(old) + len(extra)
        self.state['dropped'] += gone
        return gone

    def report(self, now):
        rows = sorted(self.state['items'].values(), key=lambda f: (-f['at'], f['id']))
        out = [{'id': f['id'], 'role': f['role'], 'kind': f['kind'], 'question': f['question'], 'status': f['status'],
                'items': f['items'], 'not_checked': f['not_checked'], 'at': f['at'],
                'sources': source_line(f['items'])} for f in rows]
        return {'items': out, 'kept': len(out), 'keep': KEEP, 'keep_days': KEEP_DAYS,
                'dropped': self.state['dropped'], 'replaced': self.state['replaced'],
                'dropped_held': self.state['dropped_held'], 'per_day_cap': link.FINDINGS_PER_DAY,
                'untrusted': True, 'checked': False, 'authority': 'NO_EXECUTION_AUTHORITY', 'note': NOTE}
