"""House readings for the House link (Systems AI 0.3.6; design MACBOOK_HOUSE_LINK_R1 §3, owner pop-up 2026-11-08
"Also include house readings").

The MacBook gets exactly the privacy-filtered entity snapshot that Claude and ChatGPT get: the accepted repository
builder ``tools/build_house_brain_entity_snapshot.py`` and its policy contracts run unchanged. They are embedded
byte-for-byte in ``embedded/`` (``tools/sync_systems_ai_embedded_sources.py``), every digest is verified against the
manifest before use, and they are laid out in a private temporary directory, as the Inspector does.

The snapshot is built on the runtime thread at most every 5 minutes, from the states Systems AI already read and the
entity registry it reads hourly (one more read-only WebSocket command). The link only serves the cached copy. Any
problem (missing or altered embedded file, a builder refusal, no registry yet) means no readings, with a reason code,
never raw states. Nothing here writes to Home Assistant.
"""
import datetime as _dt
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

EMBED_DIR = Path(__file__).resolve().parent.parent / 'embedded'
EMBED_MANIFEST = 'embedded.MANIFEST.json'
MANIFEST_SCHEMA = 'house_brain_inspector_embedded_sources.v2'
LAYOUT_RE = re.compile(r'(?:tools/[a-z0-9_]{1,64}\.py|contracts/[a-z0-9_.]{1,96}\.json)')
ENTITY_ID_RE = re.compile(r'[a-z0-9_]{1,64}\.[a-z0-9_]{1,128}')
BUILD_EVERY_S = 300
STALE_S = 900
MAX_IDS = 50


class ReadingsError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def materialize(embed_dir=EMBED_DIR):
    """Verify every embedded file against the manifest and lay them out as tools/ and contracts/ in a fresh private
    directory. Any mismatch refuses the whole builder."""
    try:
        manifest = json.loads((embed_dir / EMBED_MANIFEST).read_text(encoding='utf-8'))
        rows = manifest['files']
        if manifest.get('schema') != MANIFEST_SCHEMA or type(rows) is not list or not rows:
            raise ValueError
        root = Path(tempfile.mkdtemp(prefix='hb-systems-ai-embedded-'))
        os.chmod(root, 0o700)
        for row in rows:
            flat, rel, digest = row['flat'], row['path'], row['sha256']
            if not LAYOUT_RE.fullmatch(rel) or flat != 'embedded.' + rel.replace('/', '.', 1) or '/' in flat:
                raise ValueError
            data = (embed_dir / flat).read_bytes()
            if hashlib.sha256(data).hexdigest() != digest:
                raise ReadingsError('READINGS_EMBEDDED_DIGEST_MISMATCH')
            target = root / rel
            target.parent.mkdir(mode=0o700, exist_ok=True)
            with open(target, 'xb') as fh:
                fh.write(data)
        # A regular package here always wins over any other 'tools' on the path (namespace portions never do).
        with open(root / 'tools' / '__init__.py', 'xb'):
            pass
    except ReadingsError:
        raise
    except (OSError, ValueError, KeyError, TypeError):
        raise ReadingsError('READINGS_EMBEDDED_INVALID') from None
    return root


def load_builder(embed_dir=EMBED_DIR):
    root = materialize(embed_dir)
    saved = {m: sys.modules.pop(m) for m in [m for m in sys.modules if m == 'tools' or m.startswith('tools.')]}
    sys.path.insert(0, str(root))
    try:
        import tools  # noqa: F401 - must resolve to the verified copy only
        from tools import build_house_brain_entity_snapshot as builder
    finally:
        sys.path.remove(str(root))
        for m in [m for m in sys.modules if m == 'tools' or m.startswith('tools.')]:
            del sys.modules[m]                      # the builder keeps its own references; nothing else sees the copy
        sys.modules.update(saved)
    if Path(builder.__file__).resolve().parent.parent != root.resolve():
        raise ReadingsError('READINGS_BUILDER_NOT_EMBEDDED')
    return builder


def registry_map(rows):
    """entity_id -> {platform, device_id}; rows the builder could not vouch for are left out (fail closed)."""
    out = {}
    for row in rows if type(rows) is list else []:
        if type(row) is not dict:
            continue
        eid, platform, device = row.get('entity_id'), row.get('platform'), row.get('device_id')
        if (type(eid) is str and ENTITY_ID_RE.fullmatch(eid) and type(platform) is str
                and (device is None or type(device) is str) and eid not in out):
            out[eid] = {'platform': platform, 'device_id': device}
    return out


def state_rows(states):
    rows = list(states.values()) if isinstance(states, dict) else []
    kept, seen = [], set()
    for row in rows:
        eid = row.get('entity_id') if type(row) is dict else None
        if (type(eid) is not str or not ENTITY_ID_RE.fullmatch(eid) or eid in seen
                or type(row.get('state')) is not str or type(row.get('attributes', {})) is not dict):
            continue
        seen.add(eid)
        kept.append(row)
    return kept


def _utc(now):
    return _dt.datetime.fromtimestamp(int(now), _dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


class Readings:
    def __init__(self, loader=load_builder):
        self.loader = loader
        self.builder = None
        self.snapshot = None
        self.built_at = None
        self.reason = 'READINGS_NOT_BUILT_YET'

    def update(self, states, registry_rows, now):
        """Rebuild the snapshot at most every BUILD_EVERY_S. Called on the runtime thread only."""
        if self.built_at is not None and now - self.built_at < BUILD_EVERY_S:
            return
        self.built_at = now
        if registry_rows is None:
            self.snapshot, self.reason = None, 'READINGS_REGISTRY_UNREAD'
            return
        if not isinstance(states, dict):
            self.snapshot, self.reason = None, 'READINGS_STATES_UNREAD'
            return
        try:
            if self.builder is None:
                self.builder = self.loader()
            snap = self.builder.build(state_rows(states), registry_map(registry_rows), now=_utc(now), mode='export')
        except ReadingsError as error:
            self.snapshot, self.reason = None, error.code
            return
        except Exception as error:  # noqa: BLE001 - a builder refusal is a reason code, never raw states
            code = str(error) if re.fullmatch(r'[A-Z][A-Z0-9_]{2,63}', str(error)) else type(error).__name__.upper()
            self.snapshot, self.reason = None, 'READINGS_REFUSED:' + code[:64]
            return
        self.snapshot, self.reason = snap, None

    def view(self, now, ids=None):
        """What /link/v1/readings serves: without ids, the exported ids only; with ids (at most 50), those entities."""
        if self.snapshot is None:
            return {'schema': 'house_brain_link_readings.v1', 'available': False, 'reason': self.reason,
                    'control_authority': 'NONE'}
        generated = self.snapshot.get('generated_at')
        stale = self.built_at is None or now - self.built_at > STALE_S
        base = {'schema': 'house_brain_link_readings.v1', 'available': True, 'generated_at': generated,
                'stale': stale, 'control_authority': 'NONE'}
        entities = self.snapshot.get('entities') or []
        if ids is None:
            return {**base, 'ids': [e['entity_id'] for e in entities]}
        wanted = set(ids)
        found = [e for e in entities if e.get('entity_id') in wanted]
        known = {e['entity_id'] for e in found}
        return {**base, 'entities': found, 'not_available': sorted(wanted - known)}


def parse_ids(query):
    """``ids=a,b`` from the query string: at most 50 well-formed entity ids, else None (refused)."""
    if not query:
        return None, None
    params = dict(p.split('=', 1) for p in query.split('&') if '=' in p)
    if set(params) != {'ids'}:
        return None, 'READINGS_QUERY_INVALID'
    ids = [i for i in params['ids'].split(',') if i]
    if not ids or len(ids) > MAX_IDS or not all(ENTITY_ID_RE.fullmatch(i) for i in ids):
        return None, 'READINGS_QUERY_INVALID'
    return ids, None
