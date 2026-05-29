"""
test_vec_evolve.py — prove the middle path: search in continuous vector
space, execute as Unicode-RPN.

This v1 proof is REFINEMENT, not from-scratch: seed from a half-miss RPN
genome (`ψs→Cw;` — score -250, sev wrong), encode to vectors, run vector
(1+λ) ES against fast_spike. Assert the BEST-EVER decoded RPN is a
feasible HIT and that the EXISTING executor (swarm/genome.py) accepts it.

Why refinement and not from-scratch: the naive isotropic ES used here is
not equipped to assemble atomic structures (`→Ww`) from random vectors
in vector space — that requires either (a) a smart continuous optimizer
(CMA-ES, gradient methods on a learned surrogate) or (b) richer
mutation primitives (multi-slot bursts). Both are real follow-ups. The
refinement proof is honest about what works now: vector mutation can
take an existing RPN seed and improve it into a HIT, with the executor
unchanged — that closes the middle-path claim end-to-end.

Run:  cd /home/k8s/git/codex_monk && python -m eval.test_vec_evolve
"""

import os
import sys

from swarm.fitness import load_scenario, score as fitness_score
from swarm.genome import interpret
from swarm.evolve_vec import evolve
from swarm.probes.kernel import (
    PSISample, PSILine, MemSample, CgroupSample, TelemetryFrame,
)


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCENARIO_PATH = os.path.join(ROOT, 'scenarios', 'fast_spike.yaml')


def _spike_frame():
    total = 100_000
    avail = int(total * 0.5)
    swap = 1_048_576
    return TelemetryFrame(
        ts=0.0, caps={},
        psi_mem=PSISample(available=True,
                          some=PSILine(avg10=12.0),
                          full=PSILine(avg10=0.0)),
        mem=MemSample(total_kb=total, available_kb=avail,
                      swap_total_kb=swap, swap_free_kb=swap),
        cgroup=CgroupSample(available=False))


SEED_GENOME = 'ψs→Cw;'           # half-miss: sev wrong, score -250


def main():
    scn = load_scenario(SCENARIO_PATH)

    print("\n== vector-space REFINEMENT on fast_spike ==")
    print("(seed an RPN, encode to vectors, mutate in vector space, decode back)")
    print(f"seed RPN: {SEED_GENOME!r}  (score -250, half-miss)\n")

    result = evolve(scn, generations=200, lam=12, seed=7,
                    initial_genome=SEED_GENOME)
    bg = result['best_genome']
    bs = result['best_score']

    # Independent re-execution: feed the decoded RPN to the existing
    # interpreter against a real synthetic spike frame.
    spike_sev, spike_code = interpret(bg, _spike_frame())

    print(f"  best decoded RPN:  {bg!r}  (len={len(bg)})")
    print(f"  score:             {bs['score']:.2f}")
    print(f"  feasible:          {bs['feasible']}")
    print(f"  latency_sum:       {bs['latency_sum']}")
    print(f"  hit / miss / near / half: "
          f"{len(bs['hits'])} / {bs['miss_count']} / "
          f"{bs['near_miss_count']} / {bs['half_miss_count']}")
    print(f"  interpret on spike: ({spike_sev}, {spike_code})")

    print("\n  improvement trajectory (decoded RPN):")
    last = None
    for gen, sc, gen_str in result['trajectory']:
        if last is None or sc > last:
            print(f"    gen={gen:>3d}  score={sc:>10.2f}  {gen_str!r}")
            last = sc

    r2 = evolve(scn, generations=200, lam=12, seed=7,
                initial_genome=SEED_GENOME)
    deterministic = (r2['best_genome'] == bg
                     and r2['best_score']['score'] == bs['score'])

    checks = [
        ('best is feasible',                bs['feasible']),
        ('best is HIT (score >= -0.01)',    bs['score'] >= -0.01),
        ('best has zero misses',            bs['miss_count'] == 0),
        ('best has zero near-misses',       bs['near_miss_count'] == 0),
        ('best has zero half-misses',       bs['half_miss_count'] == 0),
        ('best has at least one hit',       len(bs['hits']) >= 1),
        ('decoded RPN executes on spike → WARN', spike_sev == 'WARN'),
        ('decoded RPN executes on spike → MEM_PSI_WARN',
                                            spike_code == 'MEM_PSI_WARN'),
        ('deterministic for seed=7',        deterministic),
    ]
    print()
    failures = 0
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            failures += 1
    print(f"\n{'ALL PASS' if failures == 0 else f'{failures} FAILURE(S)'}")
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    main()
