"""
test_declarative.py — end-to-end proof that codex_monk's only agent class
actually drives the swarm contract on a live mmap fabric.

Three layers, all deterministic (the kernel sampler is monkey-patched to
hand back synthetic TelemetryFrames so the test passes regardless of host
state):

  1. PROBE WITH HAND-CODED GENOME — the multi-rule sensor genome from
     swarm.yaml expresses (PSI full crit / PSI some + no-swap crit / PSI
     some warn / no-swap info). Drive ticks across (calm, spike, calm) and
     assert the state slots are written AND a single edge alert (WARN,
     MEM_PSI_WARN) lands in narrator id=1's inbox AND there's no duplicate
     when steady.

  2. PROBE WITH EVOLVED GENOME — the 7-char genome the (1+λ) loop
     discovered (`≡cψs→Ww`) must produce the SAME edge on the spike. If it
     doesn't, the evolutionary loop is producing genomes that pass the
     fitness oracle but don't actually drive the live swarm — that would
     be a hole in the architecture.

  3. SINK ROLE — a second declarative instance with `consume_types=[700]`
     and a `persist_path` must persist a received alert to the jsonl
     AND fail gracefully (OSError → VERB_ERROR in fabric event log, not
     silent swallow — the gotcha from the vajrayana prototype).

Run:  cd /home/k8s/git/codex_monk && python -m eval.test_declarative
"""

import json
import os
import sys
import tempfile

from swarm.fabric import Fabric, VERB_ERROR, VERB_NAMES
from swarm import template, dna_storage
import swarm.agents.declarative as declarative


# ── synthetic frame factory ───────────────────────────────────────────────
# Build kernel-probe-shaped Frame dicts. Keys mirror what swarm/probes/kernel.py
# emits at sample_all(); the agent reads them via frame.get(key).

def _frame(psi_some, psi_full, used_pct, swap_present):
    total = 100_000
    avail = int(total * (1.0 - used_pct / 100.0))
    swap_kb = 1_048_576 if swap_present else 0
    return {
        'ts':                  0.0,
        'psi.available':       True,
        'psi.some.avg10':      psi_some,
        'psi.some.avg60':      0.0,
        'psi.full.avg10':      psi_full,
        'psi.full.avg60':      0.0,
        'mem.total_kb':        total,
        'mem.available_kb':    avail,
        'mem.used_pct':        used_pct,
        'mem.avail_pct':       100.0 - used_pct,
        'mem.swap_total_kb':   swap_kb,
        'mem.swap_present':    swap_present,
        'mem.swap_total_mb':   swap_kb / 1024.0,
        'cgroup.available':    False,
        'cgroup.current_bytes': 0,
        'cgroup.oom_kills':    0,
    }


# ── shared test scaffolding ───────────────────────────────────────────────

class FrameStub:
    """Monkey-patched stand-in for swarm.probes.kernel.sample_all. The test
    sets `current` and the next on_tick() call sees that frame."""
    def __init__(self):
        self.current = _frame(0, 0, 30, True)
    def __call__(self):
        # the next call must reset the agent's adaptive-cadence timer so
        # the test isn't blocked by sleep windows
        return self.current


def _fabric_path(suffix):
    return os.path.join(tempfile.gettempdir(), f'codex_monk_test_{suffix}.fabric')


def _drain_inbox(fabric, aid):
    msgs = []
    while True:
        m = fabric.inbox_recv(aid)
        if m is None:
            break
        msgs.append(m)
    return msgs


def _make_agent(genome='', narrator_id=None, consume_types=None,
                persist_path=None):
    cfg = {
        'genome': genome,
        'narrator_id': narrator_id,
        'consume_types': consume_types or [],
        'persist_path': persist_path,
        'calm_interval': 0,    # disable cadence sleeping in test
        'alert_interval': 0,
    }
    aid = 7 if genome else 1
    return template.create_agent('declarative', aid, aid, 1, cfg)


def _drive(agent, frames, stub):
    """Drive agent through a sequence of frames, one on_tick per frame."""
    for f in frames:
        stub.current = f
        agent._next_due = 0.0       # bypass cadence wait in test
        agent.on_tick()


# ── layer 1: probe with hand-coded genome ─────────────────────────────────

HANDCODED = "ψf‡5>→Cc;ψs‡10>~S0≡∧→Cn;ψs‡10>→Ww;~S0≡→Ia;"
EVOLVED   = "≡cψs→Ww"


def test_probe(genome, label):
    failures = 0
    stub = FrameStub()
    declarative.sample_all = stub    # monkey-patch for determinism

    path = _fabric_path(label)
    if os.path.exists(path): os.remove(path)
    fabric = Fabric(path=path, create=True)
    try:
        sensor = _make_agent(genome=genome, narrator_id=1)
        sensor.attach(fabric)

        calm  = _frame(psi_some=0,  psi_full=0, used_pct=30, swap_present=True)
        spike = _frame(psi_some=12, psi_full=0, used_pct=50, swap_present=True)

        # 1. first tick (calm) — system announces baseline OK:OK
        #    (edge from (None, None) → (OK, OK), same contract as old SensorAgent)
        _drive(sensor, [calm], stub)
        sev_after_calm, _   = fabric.state_get('sys.sev')
        code_after_calm, _  = fabric.state_get('sys.code')
        psi_some, _         = fabric.state_get('sys.psi.some10')
        # v2: genome lives in the dna.7.0..N chain (fabric-DNA). The first
        # tick seeds the chain from the constructor genome, and we expect
        # a lossless round-trip.
        dna_full = dna_storage.read(fabric, 7)
        msgs_after_calm = _drain_inbox(fabric, 1)

        # 2. spike: WARN/MEM_PSI_WARN AND ONE 700 msg
        _drive(sensor, [spike], stub)
        sev_after_spike, _  = fabric.state_get('sys.sev')
        code_after_spike, _ = fabric.state_get('sys.code')
        msgs_after_spike = _drain_inbox(fabric, 1)

        # 3. spike again: steady, no duplicate alert
        _drive(sensor, [spike], stub)
        msgs_steady = _drain_inbox(fabric, 1)

        # 4. back to calm: edge msg again
        _drive(sensor, [calm], stub)
        sev_after_return, _ = fabric.state_get('sys.sev')
        msgs_after_return = _drain_inbox(fabric, 1)

        print(f"\n  [{label}]")
        print(f"    genome:        {genome!r}")
        print(f"    dna.7.* read:  {dna_full!r}")
        print(f"    calm:    sys.sev={sev_after_calm}  sys.code={code_after_calm}  "
              f"psi.some={psi_some}  msgs={[m['payload'] for m in msgs_after_calm]}")
        print(f"    spike:   sys.sev={sev_after_spike}  sys.code={code_after_spike}  "
              f"msgs={[m['payload'] for m in msgs_after_spike]}")
        print(f"    steady:  msgs={len(msgs_steady)} (must be 0)")
        print(f"    return:  sys.sev={sev_after_return}  "
              f"msgs={[m['payload'] for m in msgs_after_return]}")

        checks = [
            ('calm: sys.sev = OK',            sev_after_calm == 'OK'),
            ('calm: initial OK:OK announced', len(msgs_after_calm) == 1
                                              and msgs_after_calm[0]['payload'] == 'OK:OK'),
            ('calm: psi.some written',        psi_some == '0.00'),
            ('calm: dna.7.* chain round-trips full genome',
                                              dna_full == genome),
            ('spike: sev = WARN',             sev_after_spike == 'WARN'),
            ('spike: code = MEM_PSI_WARN',    code_after_spike == 'MEM_PSI_WARN'),
            ('spike: ONE msg emitted',        len(msgs_after_spike) == 1),
            ('spike: msg type = 700',
                msgs_after_spike and msgs_after_spike[0]['type'] == 700),
            ('spike: msg payload right',
                msgs_after_spike and msgs_after_spike[0]['payload'] == 'WARN:MEM_PSI_WARN'),
            ('steady: no duplicate msg',      len(msgs_steady) == 0),
            ('return: sev = OK',              sev_after_return == 'OK'),
            ('return: edge msg emitted',      len(msgs_after_return) == 1
                                              and msgs_after_return[0]['payload'] == 'OK:OK'),
        ]
        for name, ok in checks:
            print(f"    [{'PASS' if ok else 'FAIL'}] {name}")
            if not ok:
                failures += 1
    finally:
        fabric.close()
        if os.path.exists(path): os.remove(path)
    return failures


# ── layer 3: sink role with persistence ───────────────────────────────────

def test_sink():
    failures = 0
    path = _fabric_path('sink')
    if os.path.exists(path): os.remove(path)
    fabric = Fabric(path=path, create=True)
    try:
        persist = os.path.join(tempfile.gettempdir(), 'codex_monk_test_alerts.jsonl')
        if os.path.exists(persist): os.remove(persist)

        sink = _make_agent(consume_types=[700], persist_path=persist)
        sink.attach(fabric)

        # send a 700 msg to it via the fabric, then deliver via on_message
        fabric.inbox_send(7, 1, 700, 'WARN:MEM_PSI_WARN')
        msg = fabric.inbox_recv(1)
        sink.on_message(msg)

        # a non-700 msg should be IGNORED (not counted, not persisted)
        fabric.inbox_send(7, 1, 100, 'IGNORED')
        msg2 = fabric.inbox_recv(1)
        sink.on_message(msg2)

        wrote = os.path.exists(persist) and open(persist).read().strip()

        # now test failure path: point at an un-writable path, send another
        # msg, assert VERB_ERROR landed in the fabric event log
        sink.persist_path = '/dev/null/cannot_write/alerts.jsonl'
        fabric.inbox_send(7, 1, 700, 'WARN:FAILTEST')
        msg3 = fabric.inbox_recv(1)
        sink.on_message(msg3)

        # capture sink state AFTER all messages have been processed
        sink_last, _ = fabric.state_get('sink.last')
        sink_n, _    = fabric.state_get('sink.n')

        verb_errors = []
        for seq in range(1, fabric.log_head() + 1):
            e = fabric.log_read(seq)
            if e and e['verb'] == VERB_ERROR and e['agent'] == 1:
                verb_errors.append(e)

        print(f"\n  [sink]")
        print(f"    sink.last:  {sink_last!r}")
        print(f"    sink.n:     {sink_n}")
        print(f"    persisted line: {wrote!r}")
        print(f"    VERB_ERROR entries from id=1: {len(verb_errors)}")
        for e in verb_errors:
            print(f"      key={e['key']!r}  val={e['value']!r}")

        rec = json.loads(wrote.splitlines()[0]) if wrote else {}

        checks = [
            ('persist file created',         bool(wrote)),
            ('persisted record has payload', rec.get('payload') == 'WARN:MEM_PSI_WARN'),
            ('persisted record has sender',  rec.get('from') == 7),
            ('sink.n = 2 (1 ok + 1 fail-but-counted)', sink_n == '2'),
            ('non-700 msg ignored',          sink_last != 'IGNORED'),
            ('OSError logged via VERB_ERROR', len(verb_errors) >= 1),
            ('VERB_ERROR key = sink.persist',
                any(e['key'] == 'sink.persist' for e in verb_errors)),
        ]
        for name, ok in checks:
            print(f"    [{'PASS' if ok else 'FAIL'}] {name}")
            if not ok:
                failures += 1
    finally:
        fabric.close()
        if os.path.exists(path): os.remove(path)
    return failures


# ── runner ────────────────────────────────────────────────────────────────

def main():
    f1 = test_probe(HANDCODED, 'hand-coded multi-rule')
    f2 = test_probe(EVOLVED,   'evolved 7-char')
    f3 = test_sink()
    total = f1 + f2 + f3
    print(f"\n{'ALL PASS' if total == 0 else f'{total} FAILURE(S)'}")
    sys.exit(1 if total else 0)


if __name__ == '__main__':
    main()
