"""
test_fabric_peer.py — fabric introspection across a sibling swarm.

The premise: codex_monk and vajrayana share fabric DNA (byte-identical
layout). This test builds a synthetic peer fabric in /tmp, populates the
state slots vajrayana's agents would write, and verifies the
fabric_peer probe surfaces them as Frame keys + that genomes correctly
gate on them.

Layers:
  1. ABSENT peer → peer.available = 0, all else zero/sentinel.
  2. HEALTHY peer → all readings present, sev=OK, gateway connected,
     RAG indexed, recent heartbeat.
  3. DEGRADED peer → sev=CRITICAL, stale heartbeat, RAG gate scoring
     suspiciously high on out-of-corpus query (the BM25 brittleness
     pattern documented in vajrayana/STACK.md).
  4. Probe survives peer restart: delete + recreate the fabric file
     mid-test, sample_all() still works.
  5. Genome interpret() against each Frame fires the expected severities:
     Ψa<1 → CRIT GATE_DOWN, Ψs>‡5 → WARN POD_PRESSURE (treating high
     BM25 score on suspect query as the 'gate brittle' signal).

Run:  cd /home/k8s/git/codex_monk && python -m eval.test_fabric_peer
"""

import os
import sys
import tempfile

# point the probe at a synthetic peer BEFORE importing it
_PEER_PATH = os.path.join(tempfile.gettempdir(),
                          'codex_test_peer_fabric.fabric')
os.environ['CODEX_PEER_FABRIC_PATH'] = _PEER_PATH

from swarm.fabric import Fabric              # noqa: E402
from swarm.probes import fabric_peer          # noqa: E402
from swarm.probes import get as get_probe     # noqa: E402
from swarm.genome import interpret            # noqa: E402


_FAILS = 0
def _check(label, cond):
    global _FAILS
    if cond:
        print(f'    [PASS] {label}')
    else:
        print(f'    [FAIL] {label}')
        _FAILS += 1


def _fresh_peer() -> Fabric:
    """Create a new peer fabric at _PEER_PATH and return the handle."""
    if os.path.exists(_PEER_PATH):
        os.remove(_PEER_PATH)
    return Fabric(path=_PEER_PATH, create=True)


def _reset_query_ring():
    """The probe keeps a 60s rolling ring of query counts. Tests that don't
    care about delta-rate shouldn't have prior runs polluting the baseline."""
    fabric_peer._QUERY_RING.clear()


def main():
    print()
    print('== fabric_peer probe ==')
    print(f'  synthetic peer path: {_PEER_PATH}')

    p = get_probe('fabric_peer')
    _check('probe registered',          p.name == 'fabric_peer')
    _check('Ψ opcodes present',         'Ψ' in p.opcodes)
    _check('Ψa maps to peer.available',
           p.opcodes['Ψ'].get('a') == 'peer.available')
    _check('opcode count >= 12',        len(p.opcodes['Ψ']) >= 12)

    # ── case 1: peer absent ─────────────────────────────────────────────
    if os.path.exists(_PEER_PATH):
        os.remove(_PEER_PATH)
    _reset_query_ring()
    f = p.sample_all()
    _check('absent: peer.available = 0',           f['peer.available'] == 0)
    _check('absent: heartbeat_age = sentinel',     f['peer.sys.heartbeat_age_s'] >= 999)
    _check('absent: rag.docs = 0',                 f['peer.rag.docs'] == 0)
    _check('absent: gw.status_ok = 0',             f['peer.gw.status_ok'] == 0)

    # absent → genome should fire CRIT GATE_DOWN
    genome = 'Ψa1<→Cg;'
    sev, code = interpret(genome, f, p.opcodes)
    _check('absent: genome fires GATE_DOWN',
           sev == 'CRITICAL' and code == 'GATE_DOWN')

    # ── case 2: healthy peer ────────────────────────────────────────────
    peer = _fresh_peer()
    try:
        import time
        now = int(time.time())
        peer.state_set('sys.sev',          'OK')
        peer.state_set('sys.code',         'OK')
        peer.state_set('sys.ts',           str(now))
        peer.state_set('sys.psi.some10',   '1.2')
        peer.state_set('sys.psi.full10',   '0.0')
        peer.state_set('sys.mem.usedpct',  '42.5')
        peer.state_set('rag.docs',         '120')
        peer.state_set('rag.r.score',      '4.8')
        peer.state_set('rag.r.count',      '37')
        peer.state_set('gw.status',        'connected')
        # mark a few agents alive (state=1 = S_READY in fabric.py)
        from swarm.fabric import ACB_STATE, S_READY
        for aid in (1, 2, 3, 7):
            peer.acb_w(aid, ACB_STATE, S_READY)
    finally:
        peer.close()

    _reset_query_ring()
    f = p.sample_all()
    print(f"  healthy frame: sev_int={f['peer.sys.sev_int']} "
          f"psi_some={f['peer.sys.psi_some10']} docs={f['peer.rag.docs']} "
          f"score={f['peer.rag.last_score']} alive={f['peer.alive_agent_count']}")

    _check('healthy: peer.available = 1',          f['peer.available'] == 1)
    _check('healthy: sev_int = 0 (OK)',            f['peer.sys.sev_int'] == 0)
    _check('healthy: psi_some10 = 1.2',
           abs(f['peer.sys.psi_some10'] - 1.2) < 0.01)
    _check('healthy: mem_usedpct = 42.5',
           abs(f['peer.sys.mem_usedpct'] - 42.5) < 0.01)
    _check('healthy: rag.docs = 120',              f['peer.rag.docs'] == 120)
    _check('healthy: rag.last_score = 4.8',
           abs(f['peer.rag.last_score'] - 4.8) < 0.01)
    _check('healthy: rag.query_count = 37',        f['peer.rag.query_count'] == 37)
    _check('healthy: gw.status_ok = 1',            f['peer.gw.status_ok'] == 1)
    _check('healthy: alive_agent_count = 4',       f['peer.alive_agent_count'] == 4)
    _check('healthy: heartbeat_age < 60s',         f['peer.sys.heartbeat_age_s'] < 60)

    # ── case 3: degraded peer (BM25 brittleness pattern) ────────────────
    peer = _fresh_peer()
    try:
        # ancient heartbeat to look hung
        peer.state_set('sys.sev',          'CRITICAL')
        peer.state_set('sys.code',         'MEM_PSI_CRIT')
        peer.state_set('sys.ts',           '0')
        peer.state_set('sys.psi.some10',   '14.0')
        peer.state_set('sys.psi.full10',   '7.0')
        peer.state_set('sys.mem.usedpct',  '92.0')
        peer.state_set('rag.docs',         '120')
        # the brittleness signal: high BM25 score on an OOV query.
        # vajrayana's STACK.md documents 5.267 on a Cisco-IOS-XR question
        # against a corpus that contains no Cisco docs — gate accepts wrongly.
        peer.state_set('rag.r.score',      '5.3')
        peer.state_set('rag.r.count',      '52')
        peer.state_set('gw.status',        'error')
        # only one agent alive — others crashed
        from swarm.fabric import ACB_STATE, S_READY
        peer.acb_w(1, ACB_STATE, S_READY)
    finally:
        peer.close()

    _reset_query_ring()
    f = p.sample_all()
    print(f"  degraded frame: sev_int={f['peer.sys.sev_int']} "
          f"psi_some={f['peer.sys.psi_some10']} score={f['peer.rag.last_score']} "
          f"hb_age={f['peer.sys.heartbeat_age_s']:.0f} gw_ok={f['peer.gw.status_ok']}")

    _check('degraded: sev_int = 3 (CRITICAL)',     f['peer.sys.sev_int'] == 3)
    _check('degraded: psi_some10 = 14.0',
           abs(f['peer.sys.psi_some10'] - 14.0) < 0.01)
    _check('degraded: rag.last_score = 5.3',
           abs(f['peer.rag.last_score'] - 5.3) < 0.01)
    _check('degraded: gw.status_ok = 0',           f['peer.gw.status_ok'] == 0)
    _check('degraded: heartbeat_age large',        f['peer.sys.heartbeat_age_s'] > 100)
    _check('degraded: alive_agent_count = 1',      f['peer.alive_agent_count'] == 1)

    # genome: Ψa1<→Cg (peer down? — no, peer.available=1 here);
    #         Ψv2≥→Cc  (peer's own sev_int ≥ 2 → escalate CRIT);
    #         Ψs‡5>→Wp (BM25 brittleness pattern → WARN POD_PRESSURE).
    genome = 'Ψa1<→Cg;Ψv2≥→Cc;Ψs‡5>→Wp;'
    sev, code = interpret(genome, f, p.opcodes)
    _check('degraded: genome fires CRITICAL',      sev == 'CRITICAL')

    # ── case 4: peer restart mid-test ───────────────────────────────────
    os.remove(_PEER_PATH)
    _reset_query_ring()
    f = p.sample_all()
    _check('after-delete: peer.available = 0',     f['peer.available'] == 0)

    peer = _fresh_peer()
    try:
        peer.state_set('sys.sev', 'OK')
        peer.state_set('sys.ts',  str(int(__import__('time').time())))
    finally:
        peer.close()
    _reset_query_ring()
    f = p.sample_all()
    _check('after-restart: peer.available = 1',    f['peer.available'] == 1)
    _check('after-restart: sev_int = 0',           f['peer.sys.sev_int'] == 0)

    # ── case 5: query delta over the 60s ring ────────────────────────────
    # First sample established baseline (rag.r.count missing → 0). Bump
    # count and resample — delta should be positive.
    peer = Fabric(path=_PEER_PATH, create=False)
    try:
        peer.state_set('rag.r.count', '10')
    finally:
        peer.close()
    f = p.sample_all()
    _check('delta queries_60s > 0 after bump',     f['peer.delta.queries_60s'] >= 10)

    print()
    if _FAILS:
        print(f'  {_FAILS} FAIL(s)')
        sys.exit(1)
    print('ALL PASS')


if __name__ == '__main__':
    try:
        main()
    finally:
        if os.path.exists(_PEER_PATH):
            os.remove(_PEER_PATH)
