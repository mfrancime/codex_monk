"""
test_live_mutation.py — prove the Borg dynamic: the swarm rewrites its
own DNA in shared memory while running.

Setup: one fabric, two DeclarativeAgents on it. The TARGET (id=7) is
seeded with a half-miss genome `ψs→Cw;` — emits CRITICAL MEM_PSI_WARN
during the spike, scoring -250 (sev W is wrong, code w is right). The
MUTATOR (id=9) reads the target's `dna.7.*` chain from the fabric,
generates λ candidates per cycle, scores them against fast_spike, and
writes the best back to `dna.7.*` IF it strictly improves.

If, after N mutator cycles, the chain contains a feasible HIT genome,
in-fabric Borg mutation is real. The PROBE half is verified separately by
re-interpreting the post-mutation chain against synthetic frames and
asserting it now emits (WARN, MEM_PSI_WARN) on a spike — i.e., the target
agent's next tick would actually pick up the new DNA and behave
correctly.

Same-process here for determinism. Cross-process is the same code path
(fabric is shared-memory mmap, multiprocessing children share it
verbatim) — the next step is the same test in two boot.py children.

Run:  cd /home/k8s/git/codex_monk && python -m eval.test_live_mutation
"""

import os
import sys
import tempfile

from swarm.fabric import Fabric
from swarm.fitness import score, load_scenario
from swarm.genome import interpret
from swarm.probes.kernel import (
    PSISample, PSILine, MemSample, CgroupSample, TelemetryFrame,
)
from swarm import template, dna_storage


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCENARIO_PATH = os.path.join(ROOT, 'scenarios', 'fast_spike.yaml')

SEED_GENOME = 'ψs→Cw;'                 # half-miss (sev wrong) — score -250
MUTATOR_SEED = 7                       # deterministic RNG for the test
LAMBDA = 12
CYCLES = 80


def _spike_frame():
    """The frame the target would see during the spike phase of fast_spike."""
    total = 100_000
    avail = int(total * 0.5)             # used_pct=50
    swap = 1_048_576                     # swap present
    return TelemetryFrame(
        ts=0.0, caps={},
        psi_mem=PSISample(available=True,
                          some=PSILine(avg10=12.0),
                          full=PSILine(avg10=0.0)),
        mem=MemSample(total_kb=total, available_kb=avail,
                      swap_total_kb=swap, swap_free_kb=swap),
        cgroup=CgroupSample(available=False))


def main():
    failures = 0
    path = os.path.join(tempfile.gettempdir(), 'codex_monk_live_mut.fabric')
    if os.path.exists(path): os.remove(path)
    fabric = Fabric(path=path, create=True)

    try:
        # Target — id=7, seeded with the half-miss genome. We DON'T drive its
        # on_tick here; the only thing we need from it is that its DNA chunks
        # exist in the fabric. We seed them directly so the test does not
        # depend on the kernel sampler.
        dna_storage.write(fabric, 7, SEED_GENOME)

        # Mutator — id=9, points at target id=7, reads scenarios/fast_spike.yaml
        mutator = template.create_agent(
            'declarative', 9, 9, 1,
            {
                'mutate_target':     7,
                'mutation_interval': 0,       # no cadence sleep in test
                'mutation_lambda':   LAMBDA,
                'fitness_scenario':  SCENARIO_PATH,
                'mutation_seed':     MUTATOR_SEED,
            })
        mutator.attach(fabric)

        scenario = load_scenario(SCENARIO_PATH)
        seeded_score = score(SEED_GENOME, scenario)['score']

        print(f"\n  setup:")
        print(f"    seed genome:    {SEED_GENOME!r}")
        print(f"    seed score:     {seeded_score:.2f}  (target = 0.00 for hit)")
        print(f"    cycles:         {CYCLES}  λ={LAMBDA}  seed={MUTATOR_SEED}")
        print(f"    scenario:       {scenario['name']}")

        # Drive the mutator
        trajectory = []
        for c in range(CYCLES):
            mutator._next_due = 0.0       # bypass cadence
            mutator.on_tick()
            live = dna_storage.read(fabric, 7)
            live_score = score(live, scenario)['score']
            if not trajectory or live_score > trajectory[-1][1]:
                trajectory.append((c + 1, live_score, live))

        final = dna_storage.read(fabric, 7)
        final_score = score(final, scenario)
        mut_cycles, _ = fabric.state_get('mut.cycles')
        mut_best, _   = fabric.state_get('mut.best')

        # PROBE half: re-interpret the post-mutation DNA against a spike
        # frame. This is what the target agent's next on_tick would compute.
        spike_sev, spike_code = interpret(final, _spike_frame())

        print(f"\n  mutator cycles ran:   {mut_cycles}")
        print(f"  mut.best (fabric):    {mut_best}")
        print(f"  final genome:         {final!r}  (len={len(final)})")
        print(f"  final score:          {final_score['score']:.2f}")
        print(f"  feasible:             {final_score['feasible']}")
        print(f"  fp / miss / near / half: "
              f"{final_score['fp_count']} / {final_score['miss_count']} / "
              f"{final_score['near_miss_count']} / {final_score['half_miss_count']}")
        print(f"  interpret on spike:   ({spike_sev}, {spike_code})")

        print(f"\n  improvement trajectory (in-fabric):")
        for cyc, sc, g in trajectory:
            print(f"    cycle={cyc:>3d}  score={sc:>10.2f}  {g!r}")

        checks = [
            ('final dna.7.* feasible',           final_score['feasible']),
            ('final dna.7.* is a HIT (score>=0)', final_score['score'] >= -0.01),
            ('final dna.7.* has zero misses',    final_score['miss_count'] == 0),
            ('final dna.7.* has zero near-misses', final_score['near_miss_count'] == 0),
            ('final dna.7.* has zero half-misses', final_score['half_miss_count'] == 0),
            ('final dna.7.* differs from seed',  final != SEED_GENOME),
            ('mutator cycles state written',     mut_cycles == str(CYCLES)),
            ('interpret(post-mutation, spike) = WARN', spike_sev == 'WARN'),
            ('interpret(post-mutation, spike) = MEM_PSI_WARN',
                                                 spike_code == 'MEM_PSI_WARN'),
        ]

        print()
        for name, ok in checks:
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
            if not ok:
                failures += 1

    finally:
        fabric.close()
        if os.path.exists(path): os.remove(path)

    print(f"\n{'ALL PASS' if failures == 0 else f'{failures} FAILURE(S)'}")
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    main()
