"""
feed.py — the config-driven meta-probe. Adding a SENSE becomes config, not code.

Every other probe hard-codes one domain's parsing in Python, so a new sensor
surface meant a new module. This probe closes that gap: it reads a declarative
spec (CODEX_FEED_SPEC → a YAML/JSON file) describing one or more feeds, and
builds its Frame keys AND its opcode table from that spec at import. A new
sensor — a new etcd metric, a new node condition, any Prometheus endpoint or
JSON document — is then a few lines of YAML, with no new Python and no new
opcode hard-coding.

This is the honest answer to "adding capability should be config": perception
joins decision on the data side of the line. The irreducible code is the
generic fetch+extract engine below (written ONCE); a genuinely new SOURCE FORMAT
(beyond prometheus/json) is the only thing that extends it.

Spec shape (CODEX_FEED_SPEC points at this YAML):

  feeds:
    - opcode: "Σ"                      # genome load first-char for this feed
      name: etcd
      source:
        kind: prometheus               # prometheus | json
        fake_env: CODEX_ETCD_FAKE_PATH # env naming a local file (tests/dev)
        url_env: CODEX_ETCD_METRICS_URL# env naming an http endpoint (live)
      signals:
        - { sig: l, key: etcd.has_leader, metric: etcd_server_has_leader, agg: sum, bool: true }
        - { sig: f, key: etcd.fsync_avg_ms, ratio: [..._sum, ..._count], scale: 1000 }
        - { sig: c, key: etcd.leader_changes_60s, metric: ..._total, agg: sum, delta60: true }
        - { sig: "?", key: etcd.available, present: true }
    - opcode: "Ω"                      # nodes — JSON from /api/v1/nodes-shaped doc
      name: kube_node
      source: { kind: json, fake_env: CODEX_KUBE_NODE_FAKE_PATH }
      signals:
        - { sig: n, key: node.count, count: { items: items } }
        - { sig: r, key: node.not_ready, count: { items: items, condition: Ready, status_ne: "True" } }
        - { sig: m, key: node.mem_pressure, count: { items: items, condition: MemoryPressure, status_eq: "True" } }
        - { sig: c, key: node.unschedulable, count: { items: items, field: spec.unschedulable, truthy: true } }
        - { sig: "?", key: node.available, present: true }

Signal extractors (declarative):
  metric + agg:sum         sum a Prometheus series (any labels)
  ratio:[a,b] (+scale)     a/b across series (e.g. histogram _sum/_count → avg)
  delta60:true             value seen in the last ~60s (rate, ring-windowed)
  bool:true / present:true 1/0 flags
  scale:N                  multiply the extracted value
  count:{items, ...}       count JSON array elements matching a predicate:
                             condition+status_eq/status_ne  (k8s condition arrays)
                             field (dotted) + eq/ne/gt/truthy
"""

import json
import os
import time

from swarm.probes import register


# ── spec loading ────────────────────────────────────────────────────────────

def _load_spec() -> list:
    path = os.environ.get('CODEX_FEED_SPEC')
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path) as f:
            if path.endswith(('.yaml', '.yml')):
                import yaml
                doc = yaml.safe_load(f) or {}
            else:
                doc = json.load(f)
        return doc.get('feeds') or []
    except Exception:
        return []


_SPEC = _load_spec()


# ── sources ─────────────────────────────────────────────────────────────────

def _fetch(source: dict) -> str:
    """Return raw text for a source (prometheus text or JSON string)."""
    fake = os.environ.get(source.get('fake_env', '')) if source.get('fake_env') else None
    if fake and os.path.isfile(fake):
        try:
            with open(fake) as f:
                return f.read()
        except OSError:
            return ''
    url = os.environ.get(source.get('url_env', '')) if source.get('url_env') else None
    url = url or source.get('url')
    if url:
        try:
            import urllib.request
            with urllib.request.urlopen(url, timeout=3.0) as r:
                return r.read().decode('utf-8', 'replace')
        except Exception:
            return ''
    return ''


# ── prometheus extraction ───────────────────────────────────────────────────

def _prom_sum(text: str, metric: str):
    total, found = 0.0, False
    a, b = metric + ' ', metric + '{'
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] == '#':
            continue
        if line.startswith(a) or line.startswith(b):
            try:
                total += float(line.rsplit(' ', 1)[1]); found = True
            except (ValueError, IndexError):
                pass
    return total if found else None


# ── json extraction ─────────────────────────────────────────────────────────

def _dotted(obj, path: str):
    for part in path.split('.'):
        if isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
    return obj


def _item_matches(item: dict, pred: dict) -> bool:
    if 'condition' in pred:                       # k8s status.conditions[type==X]
        conds = ((item.get('status') or {}).get('conditions') or [])
        status = next((c.get('status') for c in conds
                       if c.get('type') == pred['condition']), None)
        if 'status_eq' in pred:
            return status == pred['status_eq']
        if 'status_ne' in pred:
            return status != pred['status_ne']
        return status is not None
    if 'field' in pred:
        val = _dotted(item, pred['field'])
        if 'truthy' in pred:
            return bool(val) == bool(pred['truthy'])
        if 'eq' in pred:
            return val == pred['eq']
        if 'ne' in pred:
            return val != pred['ne']
        if 'gt' in pred:
            try:
                return float(val) > float(pred['gt'])
            except (TypeError, ValueError):
                return False
    return True                                   # no predicate → count all


def _json_count(doc, pred: dict) -> float:
    items = _dotted(doc, pred.get('items', 'items')) or []
    if not isinstance(items, list):
        return 0.0
    return float(sum(1 for it in items if isinstance(it, dict) and _item_matches(it, pred)))


# ── rate windows (delta60), shared pattern with cgroup_pods ─────────────────

_RINGS: dict = {}
_WIN, _CAP = 60.0, 32


def _delta60(key: str, now: float, total: float) -> float:
    ring = _RINGS.setdefault(key, [])
    ring.append((now, total))
    if len(ring) > _CAP:
        del ring[0]
    cutoff, base = now - _WIN, total
    for ts, v in ring:
        if ts >= cutoff:
            base = v; break
    return max(0.0, total - base)


# ── per-signal evaluation ───────────────────────────────────────────────────

def _eval_signal(sig: dict, kind: str, text: str, doc, available: bool, now: float):
    if sig.get('present'):
        return 1.0 if available else 0.0
    val = None
    if kind == 'prometheus':
        if 'ratio' in sig:
            num = _prom_sum(text, sig['ratio'][0])
            den = _prom_sum(text, sig['ratio'][1])
            val = (num / den) if (num is not None and den) else 0.0
        elif 'metric' in sig:
            val = _prom_sum(text, sig['metric'])
    elif kind == 'json':
        if 'count' in sig:
            val = _json_count(doc, sig['count'])
        elif 'path' in sig:
            val = _dotted(doc, sig['path'])
    if val is None:
        val = 0.0
    try:
        val = float(val)
    except (TypeError, ValueError):
        val = 0.0
    if sig.get('delta60') and 'key' in sig:
        val = _delta60(sig['key'], now, val)
    if sig.get('bool'):
        val = 1.0 if val > 0 else 0.0
    if 'scale' in sig:
        val *= float(sig['scale'])
    return val


# ── Frame + OPCODES, both built from the spec ───────────────────────────────

def _frame(feeds) -> dict:
    """Build a Frame from any list of feed specs (the core engine)."""
    now = time.time()
    frame = {}
    for feed in feeds:
        src = feed.get('source') or {}
        kind = src.get('kind', 'json')
        text = _fetch(src)
        available = bool(text.strip())
        doc = None
        if kind == 'json' and available:
            try:
                doc = json.loads(text)
            except ValueError:
                available = False
        for sig in feed.get('signals') or []:
            key = sig.get('key')
            if key:
                frame[key] = _eval_signal(sig, kind, text, doc, available, now)
    return frame


def _opcodes_for(feeds) -> dict:
    table = {}
    for feed in feeds:
        op = feed.get('opcode')
        if not op:
            continue
        table.setdefault(op, {})
        for sig in feed.get('signals') or []:
            if 'sig' in sig and 'key' in sig:
                table[op][str(sig['sig'])] = sig['key']
    return table


def _normalize(spec):
    if isinstance(spec, dict):
        return spec.get('feeds') or []
    return spec or []


def build_probe(spec, name='feed'):
    """Build a PER-INSTANCE probe from a spec (a list of feeds, or a dict with a
    'feeds:' key). This is what lets an AGENT carry its own sensor surface in its
    own config — perception becomes per-agent data, not a global module. Returns
    a Probe with sample_all/opcodes/describe bound to just this spec."""
    feeds = _normalize(spec)
    from swarm.probes import Probe
    return Probe(name,
                 (lambda fs=feeds: _frame(fs)),
                 _opcodes_for(feeds),
                 (lambda fs=feeds: 'feed(' + ', '.join(
                     f.get('name', f.get('opcode', '?')) for f in fs) + ')'))


# The globally-registered `feed` probe is just build_probe over the env spec.
def sample_all() -> dict:
    return _frame(_SPEC)


OPCODES = _opcodes_for(_SPEC)


def describe() -> str:
    feeds = ', '.join(f.get('name', f.get('opcode', '?')) for f in _SPEC)
    return f'feed (config-driven: {feeds or "no spec"})'


register('feed', sample_all, OPCODES, describe)


if __name__ == '__main__':
    print(describe())
    print('opcodes:', OPCODES)
    print(json.dumps(sample_all(), indent=2))
