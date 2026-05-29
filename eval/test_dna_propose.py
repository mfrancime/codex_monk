"""
test_dna_propose.py — prove cross-swarm DNA propose actually rewrites the
live target's genome chain.

This closes the multiswarm arc. The evolver sub-swarm doesn't just observe
kernel alerts — it can SHIP a better genome to the kernel sensor, which
adopts it on its very next tick. The mechanism:

  evolver fabric                  kernel fabric
  ───────────────                 ────────────────
  mutator id=2 ── on_tick
   (1+λ) over scenarios
   discovers a better genome
   send_msg(propose_to=9,
            type=701, payload=G)
                          │
                          ▼
  inbox of gateway(9) ─── on_message
                          gateway packs VJR, ships ─►
                                                       │
                                                       ▼
                                            gateway(9) server thread
                                            inbox_send(sender=9, dst=7,
                                                       type=701, payload=G)
                                                       │
                                                       ▼
                                            sensor id=7 ── on_message
                                            type==MSG_DNA_PROPOSE ──►
                                            dna_storage.write(self.id, G)
                                            self.genome = G

Then the sensor's next on_tick reads its own DNA chain, gets the new G,
and the verdicts shift accordingly.

Two layers verified here:

  1. SAME-FABRIC PROPOSE — a single fabric with mutator (id=2) sending
     directly to probe (id=7). No gateway. Proves the on_message handler.

  2. CROSS-FABRIC PROPOSE — two fabrics, two gateways bridging. Proves
     the whole arc.

Run:  cd /home/k8s/git/codex_monk && python -m eval.test_dna_propose
"""

import os
import sys
import tempfile
import time

from swarm import dna_storage, template
from swarm.fabric import Fabric


_FAILS = 0
def _check(label, cond):
    global _FAILS
    if cond:
        print(f'    [PASS] {label}')
    else:
        print(f'    [FAIL] {label}')
        _FAILS += 1


def _fabric_path(suffix):
    return os.path.join(tempfile.gettempdir(),
                        f'codex_monk_test_dna_propose_{suffix}.fabric')


def _fresh(suffix):
    p = _fabric_path(suffix)
    if os.path.exists(p):
        os.remove(p)
    return Fabric(path=p, create=True), p


def _drain_inbox(agent):
    n = 0
    while True:
        m = agent.fabric.inbox_recv(agent.id)
        if m is None:
            break
        agent.on_message(m)
        n += 1
    return n


def _wait_for_inbox(fabric, aid, timeout_s=3.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        m = fabric.inbox_recv(aid)
        if m is not None:
            return m
        time.sleep(0.02)
    return None


# Small genomes (must fit in 48 UTF-8 bytes — the inbox cap). Both happen
# to be valid programs against fast_spike.yaml.
SEED_GENOME = 'ψs‡5>→Ww'      # 13 bytes UTF-8 — wrong threshold, half-miss
GOOD_GENOME = 'ψs‡10>→Ww'     # 14 bytes UTF-8 — correct, score 0
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCENARIO = os.path.join(ROOT, 'scenarios', 'fast_spike.yaml')


# ── layer 1: same-fabric propose ──────────────────────────────────────────

def layer1_same_fabric():
    print()
    print('  [same-fabric propose]')

    fabric, path = _fresh('same')
    try:
        # probe: starts with the seed genome
        probe = template.create_agent('declarative', 7, 7, 1, {
            'genome': SEED_GENOME,
            'calm_interval': 0,
            'alert_interval': 0,
        })
        probe.attach(fabric)

        # seed the probe's DNA chain by calling on_tick once (or write
        # directly — the probe's first tick would seed from constructor,
        # but we want the chain populated before the propose lands)
        dna_storage.write(fabric, 7, SEED_GENOME)

        # mutator: NO local target — pure propose_to mode pointed at the probe
        mutator = template.create_agent('declarative', 2, 2, 1, {
            'propose_to':       7,
            'initial_genome':   SEED_GENOME,
            'fitness_scenario': SCENARIO,
            'mutation_interval': 0,
            'mutation_lambda':   80,    # enough to find GOOD_GENOME in seed
            'mutation_seed':     7,
        })
        mutator.attach(fabric)

        # drive mutator cycles until either it proposes or budget runs out
        proposed = None
        for _ in range(50):
            mutator._next_due = 0.0
            mutator.on_tick()
            # peek the probe's inbox without consuming — we'll drain below
            head = fabric.r32(fabric.acb(7) + 64)   # ACB_INBOX_HEAD
            tail = fabric.r32(fabric.acb(7) + 68)   # ACB_INBOX_TAIL
            if tail > head:
                proposed = True
                break

        _check('mutator sent at least one propose', proposed is True)

        before = dna_storage.read(fabric, 7)

        # drain probe's inbox → triggers on_message → applies via dna_storage
        n_handled = _drain_inbox(probe)
        _check('probe processed >=1 message', n_handled >= 1)

        after = dna_storage.read(fabric, 7)
        print(f'    seed genome  : {SEED_GENOME!r}')
        print(f'    before drain : {before!r}')
        print(f'    after drain  : {after!r}')

        _check('probe DNA chain is now non-empty', bool(after))
        _check('probe DNA chain changed from seed', after != SEED_GENOME)
        _check('probe in-memory genome cache updated',
               probe.genome == after)

    finally:
        fabric.close()
        if os.path.exists(path):
            os.remove(path)


# ── layer 2: cross-fabric propose via gateway pair ────────────────────────

def layer2_cross_fabric():
    print()
    print('  [cross-fabric propose via VJR]')

    PSK = 'dna-propose-test'

    fab_k, path_k = _fresh('kernel')
    fab_e, path_e = _fresh('evolver')
    try:
        # — kernel side: probe (id=7) + gateway (id=9). Gateway has no
        # outgoing routes; it just receives 701 frames and posts to id=7.
        probe = template.create_agent('declarative', 7, 7, 1, {
            'genome': SEED_GENOME,
            'calm_interval': 0,
            'alert_interval': 0,
        })
        probe.attach(fab_k)
        dna_storage.write(fab_k, 7, SEED_GENOME)

        gw_k = template.create_agent('gateway', 9, 9, 1, {
            'swarm_name': 'kernel',
            'bind':       '127.0.0.1:0',
            'peers':      [{'name': 'evolver', 'addr': '127.0.0.1:0'}],
            'routes':     [],
            'psk':        PSK,
        })
        gw_k.attach(fab_k)
        gw_k.on_tick()                       # start server
        _, k_port = gw_k._actual_bind

        # — evolver side: gateway (id=9) configured to route 701 → kernel/7
        gw_e = template.create_agent('gateway', 9, 9, 1, {
            'swarm_name': 'evolver',
            'bind':       '127.0.0.1:0',
            'peers':      [{'name': 'kernel',
                            'addr': f'127.0.0.1:{k_port}'}],
            'routes':     [{'type': 701, 'peer': 'kernel', 'agent': 7}],
            'psk':        PSK,
        })
        gw_e.attach(fab_e)
        gw_e.on_tick()                       # start server + client

        # evolver mutator (id=2) — proposes to LOCAL gateway id=9
        mutator = template.create_agent('declarative', 2, 2, 1, {
            'propose_to':       9,
            'initial_genome':   SEED_GENOME,
            'fitness_scenario': SCENARIO,
            'mutation_interval': 0,
            'mutation_lambda':   80,
            'mutation_seed':     7,
        })
        mutator.attach(fab_e)

        # tick mutator until it proposes; drain evolver-gateway inbox
        # manually to dispatch to VJR send queue.
        proposed_at = None
        for cycle in range(50):
            mutator._next_due = 0.0
            mutator.on_tick()
            # drain evolver gateway's inbox so its on_message runs and
            # enqueues a VJR send
            while True:
                m = fab_e.inbox_recv(9)
                if m is None:
                    break
                gw_e.on_message(m)
                proposed_at = cycle

        _check('mutator emitted >=1 propose to local gateway',
               proposed_at is not None)

        # wait briefly for VJR send + receive + apply
        time.sleep(1.0)

        # drain kernel gateway: its server thread already posted to fab_k
        # inbox 7. Drive the probe's on_message to apply.
        n_applied = _drain_inbox(probe)

        before = SEED_GENOME
        after = dna_storage.read(fab_k, 7)

        print(f'    seed genome     : {SEED_GENOME!r}')
        print(f'    after VJR propose: {after!r}')
        print(f'    propose cycles   : {proposed_at}')
        print(f'    probe handled    : {n_applied} messages')

        _check('cross-fabric: probe DNA chain changed', after != before)
        _check('cross-fabric: probe in-memory genome updated',
               probe.genome == after)
        # the evolver mutator should have improved the score; the
        # adopted genome must be SHORTER or score-better than seed.
        from swarm.fitness import load_scenario, score
        scn = load_scenario(SCENARIO)
        before_score = score(SEED_GENOME, scn)['score']
        after_score = score(after, scn)['score']
        print(f'    score: seed={before_score:.0f}  adopted={after_score:.0f}')
        _check('cross-fabric: adopted genome scores >= seed',
               after_score >= before_score)

        gw_k.shutdown()
        gw_e.shutdown()
        time.sleep(0.5)

    finally:
        fab_k.close()
        fab_e.close()
        for p in (path_k, path_e):
            if os.path.exists(p):
                os.remove(p)


def main():
    print()
    print('== DNA propose ==')

    layer1_same_fabric()
    layer2_cross_fabric()

    print()
    if _FAILS:
        print(f'  {_FAILS} FAIL(s)')
        sys.exit(1)
    print('ALL PASS')


if __name__ == '__main__':
    main()
