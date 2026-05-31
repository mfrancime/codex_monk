"""
test_per_agent_probe — perception as PER-AGENT config.

The deepest step of "behavior is data": an agent declares not just how it DECIDES
(genome) but what it SENSES, via a `probe_spec` in its own config — no shared
global probe, no new module. This builds two agents from the SAME class with
DIFFERENT per-agent sensor surfaces (etcd vs nodes) and checks each senses and
gates correctly on its own signals.

  python -m eval.test_per_agent_probe     # "ALL PASS" on success
"""

import json
import os
import sys
import tempfile

TMP = tempfile.mkdtemp(prefix='codex_peragent_')
ETCD = os.path.join(TMP, 'etcd.prom')
NODES = os.path.join(TMP, 'nodes.json')
with open(ETCD, 'w') as f:
    f.write("etcd_server_has_leader 0\n")
with open(NODES, 'w') as f:
    json.dump({'items': [
        {'status': {'conditions': [{'type': 'Ready', 'status': 'False'}]}},
        {'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}},
    ]}, f)
os.environ['CODEX_ETCD_FAKE_PATH'] = ETCD
os.environ['CODEX_KUBE_NODE_FAKE_PATH'] = NODES

from swarm.agents.declarative import DeclarativeAgent     # noqa: E402
from swarm.genome import interpret                         # noqa: E402

# Two per-agent sensor surfaces, declared as data:
ETCD_SPEC = [{
    'opcode': 'Σ', 'name': 'etcd',
    'source': {'kind': 'prometheus', 'fake_env': 'CODEX_ETCD_FAKE_PATH'},
    'signals': [{'sig': 'l', 'key': 'etcd.has_leader',
                 'metric': 'etcd_server_has_leader', 'agg': 'sum', 'bool': True}],
}]
NODE_SPEC = [{
    'opcode': 'Ω', 'name': 'kube_node',
    'source': {'kind': 'json', 'fake_env': 'CODEX_KUBE_NODE_FAKE_PATH'},
    'signals': [{'sig': 'r', 'key': 'node.not_ready',
                 'count': {'items': 'items', 'condition': 'Ready', 'status_ne': 'True'}}],
}]

fails = []


def check(label, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        fails.append(label)


# Same agent class, two different per-agent perceptions — selected purely by config.
etcd_agent = DeclarativeAgent(91, 'declarative', 1,
                              genome='Σl0≡→Cg', probe_spec=ETCD_SPEC)
node_agent = DeclarativeAgent(92, 'declarative', 1,
                              genome='Ωr0>→Wx', probe_spec=NODE_SPEC)

check("etcd agent built its own opcode table (Σ)", 'Σ' in etcd_agent._probe.opcodes)
check("node agent built its own opcode table (Ω)", 'Ω' in node_agent._probe.opcodes)
check("the two agents have DIFFERENT sensor surfaces",
      set(etcd_agent._probe.opcodes) != set(node_agent._probe.opcodes))

ef = etcd_agent._probe.sample_all()
nf = node_agent._probe.sample_all()
check("etcd agent senses etcd.has_leader=0", ef.get('etcd.has_leader') == 0.0)
check("node agent senses node.not_ready=1", nf.get('node.not_ready') == 1.0)

check("etcd agent's genome fires CRIT GATE_DOWN on its sense",
      interpret(etcd_agent.genome, ef, etcd_agent._probe.opcodes) == ('CRITICAL', 'GATE_DOWN'))
check("node agent's genome fires WARN NODE_NOT_READY on its sense",
      interpret(node_agent.genome, nf, node_agent._probe.opcodes) == ('WARN', 'NODE_NOT_READY'))
# cross-check: each agent is blind to the other's signal (disjoint surfaces)
check("node agent does NOT sense etcd keys", 'etcd.has_leader' not in nf)

print()
if fails:
    print(f"{len(fails)} FAILURE(S): {fails}")
    sys.exit(1)
print("ALL PASS")
