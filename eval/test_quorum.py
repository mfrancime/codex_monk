"""
test_quorum.py — the governor's cross-fabric correlation probe.

Builds synthetic NODE and CONTROL-plane fabrics in /tmp, writes the
*prefixed* state keys each k8s sub-swarm would (nod.sys.sev, clu.sys.sev),
and verifies the `quorum` probe:
  - discovers peers and infers role from the fabric basename,
  - reads each peer's verdict prefix-agnostically (suffix match),
  - aggregates into cluster-wide counts,
  - excludes the aggregator's own fabric,
  - and that the GOVERNOR genome fires the right cluster judgement.

Governor genome under test (from swarms/k8s_aggregator.yaml):
    Γc0>Γk∧→Cd ; Γk0≡→Cg ; Γp0>→Wp

Run:  cd /home/k8s/git/codex_monk && python -m eval.test_quorum
"""

import os
import sys
import tempfile
import time

from swarm.fabric import Fabric                  # noqa: E402
from swarm.probes import quorum                   # noqa: E402
from swarm.probes import get as get_probe         # noqa: E402
from swarm.genome import interpret                # noqa: E402


_TMP = tempfile.gettempdir()
_NODE = os.path.join(_TMP, 'codex_test_q_node.fabric')
_CLUSTER = os.path.join(_TMP, 'codex_test_q_cluster.fabric')
_AGG = os.path.join(_TMP, 'codex_test_q_aggregator.fabric')
_ALL = (_NODE, _CLUSTER, _AGG)

GOVERNOR = 'Γc0>Γk∧→Cd;Γk0≡→Cg;Γp0>→Wp'

_FAILS = 0


def _check(label, cond):
    global _FAILS
    if cond:
        print(f'    [PASS] {label}')
    else:
        print(f'    [FAIL] {label}')
        _FAILS += 1


def _write(path, prefix, sev, ts=None):
    """Create a fresh fabric and write a sub-swarm's prefixed verdict."""
    if os.path.exists(path):
        os.remove(path)
    f = Fabric(path=path, create=True)
    try:
        f.state_set(prefix + 'sys.sev', sev)
        f.state_set(prefix + 'sys.code', 'TEST')
        f.state_set(prefix + 'sys.ts', str(int(ts if ts is not None else time.time())))
    finally:
        f.close()


def _cleanup():
    for p in _ALL:
        if os.path.exists(p):
            os.remove(p)


def _peers(*paths):
    os.environ['CODEX_QUORUM_PEERS'] = ','.join(paths)


def main():
    print()
    print('== quorum probe (the governor) ==')

    p = get_probe('quorum')
    _check('probe registered',            p.name == 'quorum')
    _check('Γ opcodes present',           'Γ' in p.opcodes)
    _check('Γc maps to node_critical',
           p.opcodes['Γ'].get('c') == 'quorum.node_critical')
    _check('Γk maps to control_ok',
           p.opcodes['Γ'].get('k') == 'quorum.control_ok')

    # ── case 1: all healthy ─────────────────────────────────────────────
    _write(_NODE, 'nod.', 'OK')
    _write(_CLUSTER, 'clu.', 'OK')
    _peers(_NODE, _CLUSTER)
    f = p.sample_all()
    print(f"  healthy: present={f['quorum.peers_present']} "
          f"node_total={f['quorum.node_total']} "
          f"control_total={f['quorum.control_total']} "
          f"control_ok={f['quorum.control_ok']}")
    _check('healthy: 2 peers present',        f['quorum.peers_present'] == 2)
    _check('healthy: 1 node peer',            f['quorum.node_total'] == 1)
    _check('healthy: 1 control peer',         f['quorum.control_total'] == 1)
    _check('healthy: control_ok = 1',         f['quorum.control_ok'] == 1)
    _check('healthy: node_critical = 0',      f['quorum.node_critical'] == 0)
    sev, code = interpret(GOVERNOR, f, p.opcodes)
    _check('healthy: governor stays OK',      sev == 'OK')

    # ── case 2: prefix-agnostic read of a CRITICAL node, control healthy ─
    _write(_NODE, 'nod.', 'CRITICAL')
    _write(_CLUSTER, 'clu.', 'OK')
    _peers(_NODE, _CLUSTER)
    f = p.sample_all()
    print(f"  node-crit: node_critical={f['quorum.node_critical']} "
          f"node_pressured={f['quorum.node_pressured']} "
          f"max_sev={f['quorum.max_sev']} control_ok={f['quorum.control_ok']}")
    _check('node-crit: read prefixed nod.sys.sev',  f['quorum.max_sev'] == 3)
    _check('node-crit: node_critical = 1',          f['quorum.node_critical'] == 1)
    _check('node-crit: node_pressured = 1',         f['quorum.node_pressured'] == 1)
    _check('node-crit: control_ok = 1',             f['quorum.control_ok'] == 1)
    sev, code = interpret(GOVERNOR, f, p.opcodes)
    _check('node-crit: governor → CRITICAL CLUSTER_DEGRADED',
           sev == 'CRITICAL' and code == 'CLUSTER_DEGRADED')

    # ── case 3: control plane itself CRITICAL (apiserver down) ──────────
    _write(_NODE, 'nod.', 'CRITICAL')
    _write(_CLUSTER, 'clu.', 'CRITICAL')
    _peers(_NODE, _CLUSTER)
    f = p.sample_all()
    print(f"  ctrl-down: control_ok={f['quorum.control_ok']} "
          f"node_critical={f['quorum.node_critical']}")
    _check('ctrl-down: control_ok = 0',             f['quorum.control_ok'] == 0)
    sev, code = interpret(GOVERNOR, f, p.opcodes)
    _check('ctrl-down: governor → CRITICAL GATE_DOWN (rule 2 dominates)',
           sev == 'CRITICAL' and code == 'GATE_DOWN')

    # ── case 4: node merely pressured (WARN), control healthy ───────────
    _write(_NODE, 'nod.', 'WARN')
    _write(_CLUSTER, 'clu.', 'OK')
    _peers(_NODE, _CLUSTER)
    f = p.sample_all()
    _check('pressured: node_pressured = 1',         f['quorum.node_pressured'] == 1)
    _check('pressured: node_critical = 0',          f['quorum.node_critical'] == 0)
    sev, code = interpret(GOVERNOR, f, p.opcodes)
    _check('pressured: governor → WARN POD_PRESSURE',
           sev == 'WARN' and code == 'POD_PRESSURE')

    # ── case 5: control peer absent → control_ok = 0 ────────────────────
    _write(_NODE, 'nod.', 'OK')
    if os.path.exists(_CLUSTER):
        os.remove(_CLUSTER)
    _peers(_NODE, _CLUSTER)
    f = p.sample_all()
    _check('ctrl-absent: control_present = 0',      f['quorum.control_present'] == 0)
    _check('ctrl-absent: control_ok = 0',           f['quorum.control_ok'] == 0)
    sev, code = interpret(GOVERNOR, f, p.opcodes)
    _check('ctrl-absent: governor → GATE_DOWN',
           sev == 'CRITICAL' and code == 'GATE_DOWN')

    # ── case 6: stale control heartbeat → not ok ────────────────────────
    _write(_NODE, 'nod.', 'OK')
    _write(_CLUSTER, 'clu.', 'OK', ts=1)            # ancient heartbeat
    _peers(_NODE, _CLUSTER)
    os.environ['CODEX_QUORUM_STALE_S'] = '30'
    f = p.sample_all()
    _check('stale-ctrl: peers_stale >= 1',          f['quorum.peers_stale'] >= 1)
    _check('stale-ctrl: control_ok = 0',            f['quorum.control_ok'] == 0)

    # ── case 7: aggregator self-exclusion ───────────────────────────────
    _write(_NODE, 'nod.', 'OK', ts=int(time.time()))
    _write(_CLUSTER, 'clu.', 'OK', ts=int(time.time()))
    _write(_AGG, 'agg.', 'OK', ts=int(time.time()))
    _peers(_NODE, _CLUSTER, _AGG)
    f = p.sample_all()
    print(f"  exclude-agg: peers_total={f['quorum.peers_total']} "
          f"(aggregator should be dropped)")
    _check('exclude-agg: aggregator not counted',   f['quorum.peers_total'] == 2)

    print()
    if _FAILS:
        print(f'  {_FAILS} FAIL(s)')
        sys.exit(1)
    print('ALL PASS')


if __name__ == '__main__':
    try:
        main()
    finally:
        _cleanup()
        os.environ.pop('CODEX_QUORUM_PEERS', None)
        os.environ.pop('CODEX_QUORUM_STALE_S', None)
