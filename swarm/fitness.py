"""
fitness.py — score a genome against a labeled scenario set.

Detection latency is the oracle: a genome is good if it emits the right
edge soon after a synthetic event begins, and stays silent during calm.

False positives are a HARD CONSTRAINT (FP_BUDGET = 0 by default). A genome
that pages for nothing is infeasible no matter how fast it is on real
events — it gets a single sentinel score, no further comparison. Misses are
a fixed large penalty so they dominate latency in the comparable region.

This module is the only place "right" is defined for codex_monk. Code never
invents ground truth — it loads it from scenarios/*.yaml.
"""

import yaml

from swarm.genome import interpret
from swarm.probes.kernel import OPCODES as KERNEL_OPCODES


FP_BUDGET = 0           # calm-phase FPs — hard constraint, no exceptions
MISS_CAP = 1000         # truth phase, no non-OK emit at all
NEAR_MISS = 500         # truth phase, emitted but 0 tags match
HALF_MISS = 250         # truth phase, emitted with 1 of 2 tags matching
ALPHA = 0.01            # tiny tiebreaker on calm-fp count (only matters if budget > 0)
INFEASIBLE_SCORE = -1e9


def _make_frame(psi_some, psi_full, used_pct, swap_present):
    """Build a synthetic kernel-probe Frame (dict) for scenario replay.
    Keys must match swarm/probes/kernel.py's _frame_dict shape — that's
    the contract the OPCODES table is wired against."""
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


def load_scenario(path):
    with open(path) as f:
        return yaml.safe_load(f)


def _phase_for(tick, scenario):
    for ph in scenario['phases']:
        lo, hi = ph['range']
        if lo <= tick <= hi:
            return ph
    return None


def replay(genome, scenario):
    """Return the ordered list of edges [(tick, sev, code), ...] the genome
    emits as it walks the scenario, with implicit baseline (OK, OK)."""
    edges = []
    last = ('OK', 'OK')
    for tick in range(scenario['ticks_total']):
        ph = _phase_for(tick, scenario)
        if ph is None:
            continue
        f = ph['frame']
        frame = _make_frame(
            f['psi_some'], f['psi_full'], f['used_pct'], f['swap_present'])
        sev, code = interpret(genome, frame, KERNEL_OPCODES)
        if (sev, code) != last:
            edges.append((tick, sev, code))
            last = (sev, code)
    return edges


def score(genome, scenarios):
    """Score `genome` against one or more scenario dicts.

    Per truth phase: take the BEST non-OK edge in the deadline window — best =
    most tags matching the expected (sev, code). 2-match = hit; 1-match =
    half-near; 0-match = near-miss; no non-OK edge at all = full miss. This
    builds a true gradient between "silent" and "correct" so mutation has
    something to climb.

    Calm-phase non-OK edges remain a HARD constraint (any FP -> infeasible)."""
    if isinstance(scenarios, dict):
        scenarios = [scenarios]

    latency_sum = 0
    fp_count = 0
    miss_count = 0
    near_miss_count = 0
    half_miss_count = 0
    hits, fps, misses, near_misses, half_misses = [], [], [], [], []

    for scn in scenarios:
        edges = replay(genome, scn)
        for ph in scn['phases']:
            truth = ph.get('truth')
            lo, hi = ph['range']

            if truth is None:
                for (t, s, c) in edges:
                    if lo <= t <= hi and (s, c) != ('OK', 'OK'):
                        fp_count += 1
                        fps.append((scn['name'], ph['name'], t, s, c))
                continue

            deadline = ph.get('deadline_ticks', hi - lo + 1)
            want_sev, want_code = truth['sev'], truth['code']
            window_hi = lo + deadline

            best_match = -1
            best_t = None
            best_edge = None
            for (t, s, c) in edges:
                if not (lo <= t <= window_hi):
                    continue
                if (s, c) == ('OK', 'OK'):
                    continue
                m = (1 if s == want_sev else 0) + (1 if c == want_code else 0)
                if m > best_match:
                    best_match = m
                    best_t = t
                    best_edge = (s, c)

            if best_match == 2:
                latency_sum += (best_t - lo)
                hits.append((scn['name'], ph['name'], best_t - lo))
            elif best_match == 1:
                half_miss_count += 1
                half_misses.append((scn['name'], ph['name'], best_edge))
            elif best_match == 0:
                near_miss_count += 1
                near_misses.append((scn['name'], ph['name'], best_edge))
            else:
                miss_count += 1
                misses.append((scn['name'], ph['name'], (want_sev, want_code)))

    feasible = fp_count <= FP_BUDGET
    if not feasible:
        result_score = INFEASIBLE_SCORE
    else:
        result_score = -(latency_sum
                         + MISS_CAP * miss_count
                         + NEAR_MISS * near_miss_count
                         + HALF_MISS * half_miss_count
                         + ALPHA * fp_count)

    return {
        'feasible': feasible,
        'score': result_score,
        'latency_sum': latency_sum,
        'fp_count': fp_count,
        'miss_count': miss_count,
        'near_miss_count': near_miss_count,
        'half_miss_count': half_miss_count,
        'hits': hits,
        'fps': fps,
        'misses': misses,
        'near_misses': near_misses,
        'half_misses': half_misses,
    }
