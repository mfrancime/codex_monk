"""
quorum.py — the GOVERNOR's eyes: correlate the live state of MANY sibling
fabrics at once.

`fabric_peer` reads ONE peer fabric. A governor sub-swarm has to reason
across the whole cluster — "≥2 nodes are CRITICAL while the control plane
is still healthy → CLUSTER_DEGRADED". That correlation is exactly what
`swarms/k8s_aggregator.yaml` planned as the agent-id-2 follow-up. This
probe is that follow-up: it opens every sibling fabric, reads each one's
gate verdict + heartbeat, and exposes cluster-wide *counts* as Frame
scalars. The genome then gates on the counts with the ordinary spine
comparators — no new opcode, no temporal state in the interpreter (the
aggregation lives here, the same way fabric_peer's 60s query-delta does).

Prefix-agnostic by design. Each k8s sub-swarm namespaces its state with a
short prefix (`nod.`, `clu.`, …), so the gate verdict lands at
`nod.sys.sev`, not `sys.sev`. Rather than hard-code prefixes, this probe
ENUMERATES every occupied state slot in each peer fabric and matches keys
by suffix (`*sys.sev`, `*sys.ts`). One fabric has one prefix, so one
`*sys.sev` key; if several are present we take the worst (max severity)
and the freshest heartbeat.

Peer discovery (resolved at sample time, so env changes are observed):
  CODEX_QUORUM_PEERS  — explicit comma-separated list of fabric paths.
                        Takes precedence over the glob when set.
  CODEX_QUORUM_GLOB   — glob for sibling fabrics. Default
                        '/dev/shm/codex.*.fabric'.
  CODEX_QUORUM_SELF   — a fabric path to exclude (the governor's own).
                        Any basename containing 'aggregat' is also skipped,
                        so the aggregator never counts itself.
  CODEX_QUORUM_STALE_S — heartbeat age (s) beyond which a peer is 'stale'.
                        Default 30.

Role is inferred from the fabric basename: a name containing 'cluster',
'control', or 'api' is a CONTROL-plane peer; everything else is a NODE
peer. (Override-free for the k8s composition: codex.k8s_node.fabric →
node, codex.k8s_cluster.fabric → control.)

Frame keys (this probe's contract):
  quorum.peers_total     — fabrics discovered
  quorum.peers_present   — fabrics that opened cleanly
  quorum.peers_stale     — present, but heartbeat older than STALE_S
  quorum.node_total      — discovered node-role peers
  quorum.node_pressured  — node peers present with sev ≥ WARN (2)
  quorum.node_critical   — node peers present with sev ≥ CRITICAL (3)
  quorum.control_total   — discovered control-plane peers
  quorum.control_present — control peers that opened cleanly
  quorum.control_ok      — 1 iff every control peer is present AND ≤ INFO
                           (1); also 1 when there are no control peers
  quorum.max_sev         — worst sev across all present peers (0..3)

Opcodes (Γ — uppercase gamma, the governor's alphabet):
  Γt → quorum.peers_total      Γn → quorum.node_total
  Γu → quorum.peers_present    Γp → quorum.node_pressured
  Γs → quorum.peers_stale      Γc → quorum.node_critical
  Γo → quorum.control_total    Γk → quorum.control_ok
  Γm → quorum.max_sev

Robustness mirrors fabric_peer: a missing/short/locked fabric is simply
not counted (peers_present excludes it); the probe never raises, so a
mutated governor genome can at worst score poorly.
"""

import glob as _glob
import os
import struct
import time

from swarm.fabric import (
    Fabric, FABRIC_SIZE,
    OFF_STATE, SS_SIZE, SS_KEY, SS_VALUE, SS_VERSION, MAX_STATE_SLOTS,
    _from_bytes,
)
from swarm.probes import register


_SEV_INT = {'OK': 0, 'INFO': 1, 'WARN': 2, 'CRITICAL': 3}
_EMPTY_KEY = b'\x00' * 24

_DEFAULT_GLOB = '/dev/shm/codex.*.fabric'
_CONTROL_HINTS = ('cluster', 'control', 'api')


def _stale_s() -> float:
    try:
        return float(os.environ.get('CODEX_QUORUM_STALE_S', '30'))
    except (TypeError, ValueError):
        return 30.0


def _discover() -> list:
    """Resolve the peer fabric list at sample time. Explicit CODEX_QUORUM_PEERS
    wins; otherwise glob and drop the governor's own fabric + any aggregator."""
    explicit = os.environ.get('CODEX_QUORUM_PEERS')
    if explicit:
        paths = [p.strip() for p in explicit.split(',') if p.strip()]
    else:
        pattern = os.environ.get('CODEX_QUORUM_GLOB', _DEFAULT_GLOB)
        paths = sorted(_glob.glob(pattern))
    self_path = os.environ.get('CODEX_QUORUM_SELF')
    out = []
    for p in paths:
        if self_path and os.path.abspath(p) == os.path.abspath(self_path):
            continue
        if 'aggregat' in os.path.basename(p).lower():
            continue
        out.append(p)
    return out


def _role(path: str) -> str:
    name = os.path.basename(path).lower()
    return 'control' if any(h in name for h in _CONTROL_HINTS) else 'node'


def _try_open(path: str) -> Fabric:
    """Best-effort open — None if the file is missing or not fabric-sized
    yet (peer mid-startup). Same contract as fabric_peer._try_open."""
    if not os.path.isfile(path):
        return None
    try:
        if os.stat(path).st_size < FABRIC_SIZE:
            return None
    except OSError:
        return None
    try:
        return Fabric(path=path, create=False)
    except Exception:
        return None


def _scan(fab: Fabric):
    """Enumerate occupied state slots → worst (sev_int, freshest ts) for the
    peer, by matching keys on suffix. Prefix-agnostic: 'nod.sys.sev' and
    'clu.sys.sev' both match '*sys.sev'. Returns (sev_int, ts, found)."""
    mm = fab.mm
    sev_int, ts, found = 0, 0.0, False
    for idx in range(MAX_STATE_SLOTS):
        off = OFF_STATE + idx * SS_SIZE
        kb = mm[off + SS_KEY: off + SS_KEY + 24]
        if kb == _EMPTY_KEY:
            continue
        key = _from_bytes(kb)
        if not key:
            continue
        if key.endswith('sys.sev'):
            val = _from_bytes(mm[off + SS_VALUE: off + SS_VALUE + 20])
            sev_int = max(sev_int, _SEV_INT.get(val.strip(), 0))
            found = True
        elif key.endswith('sys.ts'):
            val = _from_bytes(mm[off + SS_VALUE: off + SS_VALUE + 20])
            try:
                ts = max(ts, float(val))
            except (TypeError, ValueError):
                pass
    return sev_int, ts, found


def sample_all() -> dict:
    now = time.time()
    stale_s = _stale_s()
    peers = _discover()

    fr = {
        'ts':                     now,
        'quorum.peers_total':     len(peers),
        'quorum.peers_present':   0,
        'quorum.peers_stale':     0,
        'quorum.node_total':      0,
        'quorum.node_pressured':  0,
        'quorum.node_critical':   0,
        'quorum.control_total':   0,
        'quorum.control_present': 0,
        'quorum.max_sev':         0,
    }
    control_total = 0
    control_present = 0
    control_all_ok = True   # vacuously true until a control peer says otherwise

    for path in peers:
        role = _role(path)
        is_control = (role == 'control')
        if is_control:
            control_total += 1
            fr['quorum.control_total'] += 1
        else:
            fr['quorum.node_total'] += 1

        fab = _try_open(path)
        if fab is None:
            # a control peer we can't even open is NOT ok
            if is_control:
                control_all_ok = False
            continue
        try:
            sev_int, ts, found = _scan(fab)
        finally:
            try:
                fab.close()
            except Exception:
                pass

        fr['quorum.peers_present'] += 1
        fr['quorum.max_sev'] = max(fr['quorum.max_sev'], sev_int)

        hb_age = (now - ts) if ts > 0 else 1e9
        if hb_age > stale_s:
            fr['quorum.peers_stale'] += 1

        if is_control:
            control_present += 1
            # healthy control plane = present, fresh, and ≤ INFO
            if hb_age > stale_s or sev_int >= 2:
                control_all_ok = False
        else:
            if sev_int >= 2:
                fr['quorum.node_pressured'] += 1
            if sev_int >= 3:
                fr['quorum.node_critical'] += 1

    fr['quorum.control_present'] = control_present
    # control_ok: every declared control peer present AND healthy. With no
    # control peers at all, the control plane isn't a constraint → 1.
    fr['quorum.control_ok'] = 1 if (control_total == 0 or
                                    (control_present == control_total
                                     and control_all_ok)) else 0
    return fr


def describe() -> str:
    peers = _discover()
    return f'quorum ({len(peers)} peer fabric{"s" if len(peers) != 1 else ""})'


OPCODES = {
    'Γ': {
        't': 'quorum.peers_total',
        'u': 'quorum.peers_present',
        's': 'quorum.peers_stale',
        'n': 'quorum.node_total',
        'p': 'quorum.node_pressured',
        'c': 'quorum.node_critical',
        'o': 'quorum.control_total',
        'k': 'quorum.control_ok',
        'm': 'quorum.max_sev',
    },
}


register('quorum', sample_all, OPCODES, describe)


if __name__ == "__main__":
    import json
    print(describe())
    print(json.dumps(sample_all(), indent=2))
