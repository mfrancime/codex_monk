"""
k8s_api.py — Kubernetes control-plane health via raw HTTPS to the apiserver.

Pure stdlib (urllib + ssl + json). No kubernetes-python-client dependency,
which keeps the DaemonSet image small.

Two auth modes:

  (1) In-cluster: read token + CA from the projected service account
      directory and apiserver host/port from env vars. This is the
      production deployment path.

  (2) Override: explicit K8S_API_HOST / K8S_API_TOKEN / K8S_API_CA env
      vars for dev outside a cluster. Token is the only required.

When neither mode succeeds, the probe returns a Frame with k8s.api.healthy=0
and zeroed counters. The genome handles "apiserver unreachable" the same
way it handles any other CRITICAL.

Frame keys (10s default cadence — this is not free):
  k8s.api.healthy        — 0 / 1
  k8s.api.latency_ms     — last /healthz round-trip
  k8s.nodes.total
  k8s.nodes.not_ready
  k8s.events.warnings_60s
  k8s.deployments.degraded
  k8s.api.auth_present   — 1 if token + host were resolved at startup

Opcodes (K):
  Ka → k8s.api.healthy
  Kl → k8s.api.latency_ms
  Kn → k8s.nodes.total
  Kx → k8s.nodes.not_ready
  Ke → k8s.events.warnings_60s
  Kd → k8s.deployments.degraded
  K? → k8s.api.auth_present

For tests, the probe respects three injection seams:
  - CODEX_K8S_API_FAKE_PATH      a JSON file mapping endpoint→response
  - CODEX_K8S_API_FAKE_HEALTHY   '0' to force unhealthy
  - Plain-old monkeypatching `k8s_api._GET = stub`
"""

import json
import os
import ssl
import time
import urllib.request

from swarm.probes import register


_SA_DIR = '/var/run/secrets/kubernetes.io/serviceaccount'
_GET_TIMEOUT_S = 5.0


def _read_text_silently(path: str) -> str:
    try:
        with open(path, 'r') as f:
            return f.read().strip()
    except OSError:
        return ''


def _resolve_auth() -> dict:
    """Pick the best available auth context at startup. Returns a dict with
    host, port, token, ca_path (or None for each). Missing pieces are fine —
    the probe will report k8s.api.healthy=0."""
    # Override path first (dev / out-of-cluster).
    token = os.environ.get('K8S_API_TOKEN') or _read_text_silently(
        os.path.join(_SA_DIR, 'token'))
    host = os.environ.get('K8S_API_HOST') or os.environ.get(
        'KUBERNETES_SERVICE_HOST')
    port = os.environ.get('K8S_API_PORT') or os.environ.get(
        'KUBERNETES_SERVICE_PORT', '443')
    ca_path = os.environ.get('K8S_API_CA')
    if not ca_path:
        # in-cluster default
        sa_ca = os.path.join(_SA_DIR, 'ca.crt')
        if os.path.isfile(sa_ca):
            ca_path = sa_ca
    return {'host': host, 'port': port, 'token': token, 'ca_path': ca_path}


_AUTH = _resolve_auth()
_AUTH_PRESENT = bool(_AUTH['host'] and _AUTH['token'])


def _make_ssl_ctx() -> ssl.SSLContext:
    ca = _AUTH.get('ca_path')
    if ca and os.path.isfile(ca):
        return ssl.create_default_context(cafile=ca)
    # No CA available — relax verification. Honest about the fallback.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _GET(path: str) -> tuple:
    """HTTP GET against the apiserver. Returns (status_code, body_dict_or_None,
    elapsed_ms). Swallows errors → returns (0, None, elapsed)."""
    if not _AUTH_PRESENT:
        return 0, None, 0
    url = f'https://{_AUTH["host"]}:{_AUTH["port"]}{path}'
    req = urllib.request.Request(url, headers={
        'Authorization': f'Bearer {_AUTH["token"]}',
        'Accept': 'application/json',
    })
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=_GET_TIMEOUT_S,
                                     context=_make_ssl_ctx()) as resp:
            status = resp.status
            raw = resp.read()
    except Exception:
        return 0, None, int((time.time() - t0) * 1000)
    elapsed = int((time.time() - t0) * 1000)
    body = None
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        pass
    return status, body, elapsed


def _fake_get_factory():
    """Test hook: if CODEX_K8S_API_FAKE_PATH points at a JSON file, return
    a stub _GET that reads from it. Otherwise return None."""
    fake_path = os.environ.get('CODEX_K8S_API_FAKE_PATH')
    if not fake_path or not os.path.isfile(fake_path):
        return None
    with open(fake_path, 'r') as f:
        table = json.load(f)
    healthy = os.environ.get('CODEX_K8S_API_FAKE_HEALTHY', '1') != '0'

    def stub(path):
        if path == '/healthz' and not healthy:
            return 0, None, 50
        body = table.get(path)
        if body is None:
            return 0, None, 50
        return 200, body, 20

    return stub


def _events_recent_warnings(body, window_s=60) -> int:
    """Count Events of type Warning in the last `window_s` seconds. Falls
    back to the entire item list if timestamps are missing."""
    if not isinstance(body, dict):
        return 0
    items = body.get('items') or []
    if not items:
        return 0
    now = time.time()
    cutoff = now - window_s
    n = 0
    for it in items:
        if it.get('type') != 'Warning':
            continue
        ts_str = (it.get('lastTimestamp') or it.get('eventTime')
                  or it.get('firstTimestamp'))
        if ts_str:
            # parse RFC3339 — quick and lenient
            try:
                # python's fromisoformat doesn't accept the 'Z' suffix
                # pre-3.11; strip it.
                from datetime import datetime, timezone
                if ts_str.endswith('Z'):
                    ts_str = ts_str[:-1] + '+00:00'
                ts = datetime.fromisoformat(ts_str).timestamp()
                if ts < cutoff:
                    continue
            except (ValueError, TypeError):
                pass
        n += 1
    return n


def _nodes_not_ready(body) -> tuple:
    """Returns (total, not_ready_count)."""
    items = body.get('items') if isinstance(body, dict) else None
    if not items:
        return 0, 0
    total = len(items)
    not_ready = 0
    for n in items:
        conds = ((n.get('status') or {}).get('conditions')) or []
        ready_status = 'Unknown'
        for c in conds:
            if c.get('type') == 'Ready':
                ready_status = c.get('status', 'Unknown')
                break
        if ready_status != 'True':
            not_ready += 1
    return total, not_ready


def _deployments_degraded(body) -> int:
    items = body.get('items') if isinstance(body, dict) else None
    if not items:
        return 0
    degraded = 0
    for d in items:
        st = d.get('status') or {}
        replicas = st.get('replicas', 0) or 0
        ready    = st.get('readyReplicas', 0) or 0
        if replicas > 0 and ready < replicas:
            degraded += 1
    return degraded


def sample_all() -> dict:
    # Pick the GET function: fake stub for tests, otherwise the real one.
    get_fn = _fake_get_factory() or _GET

    status_h, _, latency = get_fn('/healthz')
    healthy = 1 if status_h == 200 else 0

    nodes_total = 0
    nodes_not_ready = 0
    warnings = 0
    degraded = 0

    if healthy:
        _, nodes_body, _ = get_fn('/api/v1/nodes')
        nodes_total, nodes_not_ready = _nodes_not_ready(nodes_body)

        _, ev_body, _ = get_fn(
            '/api/v1/events?fieldSelector=type%3DWarning&limit=50')
        warnings = _events_recent_warnings(ev_body)

        _, dep_body, _ = get_fn('/apis/apps/v1/deployments')
        degraded = _deployments_degraded(dep_body)

    return {
        'ts':                       time.time(),
        'k8s.api.healthy':          healthy,
        'k8s.api.latency_ms':       latency,
        'k8s.api.auth_present':     1 if _AUTH_PRESENT else 0,
        'k8s.nodes.total':          nodes_total,
        'k8s.nodes.not_ready':      nodes_not_ready,
        'k8s.events.warnings_60s':  warnings,
        'k8s.deployments.degraded': degraded,
    }


def describe() -> str:
    if _AUTH_PRESENT:
        return f'k8s_api (apiserver={_AUTH["host"]}:{_AUTH["port"]})'
    return 'k8s_api (no auth — unreachable)'


OPCODES = {
    'K': {
        'a': 'k8s.api.healthy',
        'l': 'k8s.api.latency_ms',
        'n': 'k8s.nodes.total',
        'x': 'k8s.nodes.not_ready',
        'e': 'k8s.events.warnings_60s',
        'd': 'k8s.deployments.degraded',
        '?': 'k8s.api.auth_present',
    },
}


register('k8s_api', sample_all, OPCODES, describe)


if __name__ == "__main__":
    print(describe())
    print(json.dumps(sample_all(), indent=2))
