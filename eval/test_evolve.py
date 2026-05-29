"""
test_evolve.py — prove the (1+λ) loop converges on the fast_spike scenario.

Starting from the empty genome (NEVER-EMIT, score -1000), the loop must
climb to a feasible high-score genome within a bounded search budget. If
this holds, codex_monk is genuinely an evolvable agentic OS — new behavior
emerges from search over DNA, not from a human writing Python.

Run:  cd /home/k8s/git/codex_monk && python -m eval.test_evolve
"""

import os
import sys

from swarm.fitness import score, load_scenario
from swarm.evolve import evolve


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCENARIO_PATH = os.path.join(ROOT, 'scenarios', 'fast_spike.yaml')


def main():
    scn = load_scenario(SCENARIO_PATH)

    print(f"\n== convergence on {scn['name']} (200 gens × λ=12, seed=7) ==\n")
    result = evolve(scn, generations=200, lam=12, seed=7, initial='')
    best = result['best']
    bs = result['best_score']

    print(f"  best genome:  {best!r}  (len={len(best)})")
    print(f"  score:        {bs['score']:.2f}")
    print(f"  feasible:     {bs['feasible']}")
    print(f"  latency_sum:  {bs['latency_sum']}")
    print(f"  fp / miss:    {bs['fp_count']} / {bs['miss_count']}")

    print("\n  improvement trajectory:")
    last = None
    for gen, sc, feas, g in result['trajectory']:
        if last is None or sc > last:
            print(f"    gen={gen:>3d}  score={sc:>10.2f}  feasible={feas!s:5s}  {g!r}")
            last = sc

    # determinism: same seed must reproduce
    r2 = evolve(scn, generations=200, lam=12, seed=7, initial='')
    deterministic = (r2['best'] == best
                     and r2['best_score']['score'] == bs['score'])

    checks = [
        ('best genome is feasible',          bs['feasible']),
        ('best is a HIT (score >= -0.01)',   bs['score'] >= -0.01),
        ('best has zero misses',             bs['miss_count'] == 0),
        ('best has zero near-misses',        bs.get('near_miss_count', 0) == 0),
        ('best has zero half-misses',        bs.get('half_miss_count', 0) == 0),
        ('best has at least one hit',        len(bs['hits']) >= 1),
        ('best latency_sum <= 5',            bs['latency_sum'] <= 5),
        ('deterministic for seed=7',         deterministic),
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
