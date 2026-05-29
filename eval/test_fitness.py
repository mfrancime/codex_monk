"""
test_fitness.py — prove the fitness oracle is honest.

Three genomes that frame the design space:

  SENSOR  — `ψs‡10>→Ww;`  "psi.some > 10 → WARN MEM_PSI_WARN"
            The minimal correct genome for the fast_spike scenario.
            Expected: feasible, zero FP, zero miss, ~zero latency, top score.

  ALWAYS  — `1→Cc;`       "push 1, emit CRITICAL MEM_PSI_CRIT unconditionally"
            Pages for nothing. Expected: INFEASIBLE (FP > budget).

  NEVER   — ``            no rules; default (OK, OK) every tick.
            Stays silent forever. Expected: feasible, all misses, worst
            feasible score.

If these three orderings hold (SENSOR > NEVER > ALWAYS), the oracle is
honest enough to evolve against. If they don't, no evolutionary loop built
on top will mean anything.

Run:  cd /home/k8s/git/codex_monk && python -m eval.test_fitness
"""

import os
import sys

from swarm.fitness import score, load_scenario


HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCENARIO_PATH = os.path.join(ROOT, 'scenarios', 'fast_spike.yaml')

GENOME_SENSOR = 'ψs‡10>→Ww;'
GENOME_ALWAYS = '1→Cc;'
GENOME_NEVER  = ''


def _row(label, r):
    print(f"  {label:14s} score={r['score']:>12.2f}  feasible={r['feasible']!s:5s}  "
          f"latency={r['latency_sum']:>4d}  fp={r['fp_count']:>3d}  miss={r['miss_count']:>3d}")
    if r['hits']:
        for h in r['hits']:
            print(f"      hit:  {h}")
    if r['fps']:
        for fp in r['fps'][:3]:
            print(f"      fp:   {fp}")
        if len(r['fps']) > 3:
            print(f"      ... +{len(r['fps'])-3} more FPs")
    if r['misses']:
        for m in r['misses']:
            print(f"      miss: {m}")


def main():
    scn = load_scenario(SCENARIO_PATH)
    r_good  = score(GENOME_SENSOR, scn)
    r_alarm = score(GENOME_ALWAYS, scn)
    r_dead  = score(GENOME_NEVER,  scn)

    print(f"\n== scenario: {scn['name']} ({scn['ticks_total']} ticks) ==\n")
    _row('SENSOR', r_good)
    _row('ALWAYS-CRIT', r_alarm)
    _row('NEVER-EMIT', r_dead)

    checks = [
        ('SENSOR is feasible',                 r_good['feasible']),
        ('SENSOR has zero FPs',                r_good['fp_count'] == 0),
        ('SENSOR has zero misses',             r_good['miss_count'] == 0),
        ('SENSOR latency to spike is <= 1',    r_good['latency_sum'] <= 1),
        ('ALWAYS-CRIT is INFEASIBLE',          not r_alarm['feasible']),
        ('ALWAYS-CRIT has at least one FP',    r_alarm['fp_count'] >= 1),
        ('NEVER-EMIT is feasible',             r_dead['feasible']),
        ('NEVER-EMIT misses the event',        r_dead['miss_count'] >= 1),
        ('ordering: SENSOR > NEVER > ALWAYS',
         r_good['score'] > r_dead['score'] > r_alarm['score']),
    ]

    failures = 0
    print()
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            failures += 1

    print(f"\n{'ALL PASS' if failures == 0 else f'{failures} FAILURE(S)'}")
    sys.exit(1 if failures else 0)


if __name__ == '__main__':
    main()
