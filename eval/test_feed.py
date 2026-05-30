"""
test_feed — the config-driven meta-probe.

Proves that a new SENSE can be added as config (a feed spec) rather than a new
Python module: writes a spec + synthetic prometheus/json sources to /tmp, then
asserts the `feed` probe builds its opcode table + Frame from the spec and that
genomes gate correctly on the config-defined signals. swarm/ is untouched —
this exercises the one generic engine.

  python -m eval.test_feed     # "ALL PASS" on success
"""

import json
import os
import sys
import tempfile

# CODEX_FEED_SPEC must be set BEFORE swarm.probes imports feed (it builds its
# OPCODES from the spec at import). So stage everything first, then import.
TMP = tempfile.mkdtemp(prefix='codex_feed_')
SPEC = os.path.join(TMP, 'feeds.yaml')
ETCD = os.path.join(TMP, 'etcd.prom')
NODES = os.path.join(TMP, 'nodes.json')

with open(SPEC, 'w') as f:
    f.write(f"""
feeds:
  - opcode: "Σ"
    name: etcd
    source: {{ kind: prometheus, fake_env: CODEX_ETCD_FAKE_PATH }}
    signals:
      - {{ sig: l, key: etcd.has_leader, metric: etcd_server_has_leader, agg: sum, bool: true }}
      - {{ sig: f, key: etcd.fsync_avg_ms, ratio: [etcd_disk_wal_fsync_duration_seconds_sum, etcd_disk_wal_fsync_duration_seconds_count], scale: 1000 }}
      - {{ sig: "?", key: etcd.available, present: true }}
  - opcode: "Ω"
    name: kube_node
    source: {{ kind: json, fake_env: CODEX_KUBE_NODE_FAKE_PATH }}
    signals:
      - {{ sig: r, key: node.not_ready, count: {{ items: items, condition: Ready, status_ne: "True" }} }}
      - {{ sig: u, key: node.unschedulable, count: {{ items: items, field: spec.unschedulable, truthy: true }} }}
      - {{ sig: "?", key: node.available, present: true }}
""")
with open(ETCD, 'w') as f:
    f.write("etcd_server_has_leader 0\n"
            "etcd_disk_wal_fsync_duration_seconds_sum 45.0\n"
            "etcd_disk_wal_fsync_duration_seconds_count 100\n")
with open(NODES, 'w') as f:
    json.dump({'items': [
        {'status': {'conditions': [{'type': 'Ready', 'status': 'False'}]}},
        {'spec': {'unschedulable': True},
         'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}},
        {'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}},
    ]}, f)

os.environ['CODEX_FEED_SPEC'] = SPEC
os.environ['CODEX_ETCD_FAKE_PATH'] = ETCD
os.environ['CODEX_KUBE_NODE_FAKE_PATH'] = NODES

from swarm import probes                      # noqa: E402
from swarm.genome import interpret            # noqa: E402

fails = []


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        fails.append(label)


p = probes.get('feed')
op = p.opcodes
check("opcode table built from config (Σ + Ω)", 'Σ' in op and 'Ω' in op)
check("Σf maps to etcd.fsync_avg_ms", op.get('Σ', {}).get('f') == 'etcd.fsync_avg_ms')
check("Ωr maps to node.not_ready", op.get('Ω', {}).get('r') == 'node.not_ready')

fr = p.sample_all()
check("etcd.has_leader sensed = 0", fr.get('etcd.has_leader') == 0.0)
check("etcd.fsync_avg_ms sensed = 450", abs(fr.get('etcd.fsync_avg_ms', 0) - 450.0) < 1e-6)
check("etcd.available = 1", fr.get('etcd.available') == 1.0)
check("node.not_ready counted = 1", fr.get('node.not_ready') == 1.0)
check("node.unschedulable counted = 1", fr.get('node.unschedulable') == 1.0)

# genomes gate on the config-defined signals
check("genome Σl0≡→Cg fires CRIT GATE_DOWN (no leader)",
      interpret('Σl0≡→Cg', fr, op) == ('CRITICAL', 'GATE_DOWN'))
check("genome Σf‡100>→Wg fires WARN (slow fsync)",
      interpret('Σf‡100>→Wg', fr, op) == ('WARN', 'GATE_DOWN'))
check("genome Ωr0>→Wx fires WARN NODE_NOT_READY",
      interpret('Ωr0>→Wx', fr, op) == ('WARN', 'NODE_NOT_READY'))
check("genome Ωu0>→Wx fires on cordoned node",
      interpret('Ωu0>→Wx', fr, op) == ('WARN', 'NODE_NOT_READY'))

print()
if fails:
    print(f"{len(fails)} FAILURE(S): {fails}")
    sys.exit(1)
print("ALL PASS")
