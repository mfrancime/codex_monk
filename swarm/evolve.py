"""
evolve.py — (1+λ) evolutionary loop over alien-RPN genomes.

Given the fitness oracle (swarm/fitness.py, an honest deterministic scorer),
this climbs it. Elitist (1+λ) ES: one parent, λ mutants per generation, the
highest-scoring becomes the next parent. Parent survives ties — elitism
prevents regression. Trajectory logged per generation; deterministic for a
fixed seed.

Mutation operators are domain-aware (insert a valid emit token, a frame
load, a literal, etc.) rather than blind byte flips. "Random byte flips
suffice" is an empirical claim worth testing separately; this loop's job is
to prove convergence at all on this DNA shape. Each child receives 1-3
mutation ops; a parsimony tiebreaker prefers shorter genomes among equals.

Usage:
    python -m swarm.evolve                              # defaults
    python -m swarm.evolve --gens 200 --lam 12 --seed 7
    python -m swarm.evolve --scenario scenarios/fast_spike.yaml
"""

import argparse
import os
import random

from swarm.fitness import score, load_scenario


# ── alphabet (valid opcode tokens by category) ────────────────────────────

LOADS     = ['ψs', 'ψf', 'ψ?', '~u', '~a', '~S', '~s', 'κ?']
COMPARES  = ['>', '<', '≥', '≤', '≡', '≠']
BOOLS     = ['∧', '∨', '¬']
SEV_TAGS  = ['O', 'I', 'W', 'C']
CODE_TAGS = ['o', 'a', 'w', 'n', 'c', 'l', 'L']

MAX_LEN = 40    # hard cap on genome length to keep search tractable


# ── mutation primitives ───────────────────────────────────────────────────

def _insert_at(genome, token, rng):
    pos = rng.randint(0, len(genome))
    return genome[:pos] + token + genome[pos:]


def _delete_one(genome, rng):
    if not genome:
        return genome
    pos = rng.randrange(len(genome))
    return genome[:pos] + genome[pos + 1:]


def mut_insert_emit(g, rng):
    return _insert_at(g, '→' + rng.choice(SEV_TAGS) + rng.choice(CODE_TAGS), rng)


def mut_insert_load(g, rng):
    return _insert_at(g, rng.choice(LOADS), rng)


def mut_insert_literal(g, rng):
    if rng.random() < 0.5:
        return _insert_at(g, str(rng.randint(0, 9)), rng)
    return _insert_at(g, '‡' + str(rng.randint(1, 99)), rng)


def mut_insert_op(g, rng):
    return _insert_at(g, rng.choice(COMPARES + BOOLS + [';']), rng)


def mut_delete(g, rng):
    return _delete_one(g, rng)


def mut_swap_sev(g, rng):
    idxs = [i for i, c in enumerate(g) if c in SEV_TAGS]
    if not idxs:
        return g
    pos = rng.choice(idxs)
    new = rng.choice([t for t in SEV_TAGS if t != g[pos]])
    return g[:pos] + new + g[pos + 1:]


def mut_swap_code(g, rng):
    idxs = [i for i, c in enumerate(g) if c in CODE_TAGS]
    if not idxs:
        return g
    pos = rng.choice(idxs)
    new = rng.choice([t for t in CODE_TAGS if t != g[pos]])
    return g[:pos] + new + g[pos + 1:]


def mut_perturb_literal(g, rng):
    """Find the first ‡N..N segment and bump it by a small gaussian step."""
    i = 0
    while i < len(g):
        if g[i] == '‡':
            j = i + 1
            buf = ''
            while j < len(g) and g[j].isdigit():
                buf += g[j]
                j += 1
            if buf:
                v = int(buf)
                step = int(round(rng.gauss(0, 3)))
                v = max(0, min(99, v + step))
                return g[:i] + '‡' + str(v) + g[j:]
        i += 1
    return g


MUTATIONS = [
    (mut_insert_emit,     0.20),
    (mut_insert_load,     0.20),
    (mut_insert_literal,  0.10),
    (mut_insert_op,       0.15),
    (mut_delete,          0.15),
    (mut_swap_sev,        0.05),
    (mut_swap_code,       0.05),
    (mut_perturb_literal, 0.10),
]


def _pick_mutation(rng):
    r, acc = rng.random(), 0.0
    for fn, w in MUTATIONS:
        acc += w
        if r < acc:
            return fn
    return MUTATIONS[-1][0]


def mutate(genome, rng, n_ops=None):
    if n_ops is None:
        n_ops = rng.choices([1, 2, 3], weights=[0.6, 0.3, 0.1])[0]
    g = genome
    for _ in range(n_ops):
        g = _pick_mutation(rng)(g, rng)
    return g[:MAX_LEN]


# ── (1+λ) elitist ES ──────────────────────────────────────────────────────

DRIFT_PROB = 0.5            # probability of accepting an equally-scoring child
STALL_LIMIT = 30            # gens without best-ever improvement → restart from archive


def _best_parent(pool, parent, parent_score, rng):
    """Select the next parent. On strict improvement, adopt (parsimony among
    ties). On tied score, drift onto a different tied child with DRIFT_PROB,
    BUT bias the drift toward parsimony — prefer shorter equally-scoring
    children so the genome doesn't bloat into noise that buries structure."""
    top_score = max(c[0]['score'] for c in pool)
    candidates = [c for c in pool if c[0]['score'] == top_score
                  and c[1] != parent]
    if not candidates:
        return parent, parent_score
    # both strict-improvement and drift use the same parsimony bias
    shortest = min(len(c[1]) for c in candidates)
    finalists = [c for c in candidates if len(c[1]) == shortest]
    pick = rng.choice(finalists)
    if top_score > parent_score['score']:
        return pick[1], pick[0]
    if top_score == parent_score['score'] and rng.random() < DRIFT_PROB:
        return pick[1], pick[0]
    return parent, parent_score


def evolve(scenarios, generations=100, lam=8, seed=0, initial=''):
    """One parent, λ mutants per generation. Tracks best-ever genome
    separately from the drifting parent (an archive), restarts the parent
    from the archive after STALL_LIMIT stagnant gens. Returns the archived
    best — drift can't lose ground that was already gained."""
    rng = random.Random(seed)
    parent = initial
    parent_score = score(parent, scenarios)
    best_ever = parent
    best_ever_score = parent_score
    stall = 0
    trajectory = [(0, parent_score['score'], parent_score['feasible'], parent)]

    for gen in range(1, generations + 1):
        pool = []
        for _ in range(lam):
            child = mutate(parent, rng)
            pool.append((score(child, scenarios), child))
        parent, parent_score = _best_parent(pool, parent, parent_score, rng)
        trajectory.append(
            (gen, parent_score['score'], parent_score['feasible'], parent))

        # archive update: best-ever wins on strict improvement OR on
        # equal score with shorter length (parsimony)
        improved = (parent_score['score'] > best_ever_score['score']
                    or (parent_score['score'] == best_ever_score['score']
                        and len(parent) < len(best_ever)))
        if improved:
            best_ever, best_ever_score = parent, parent_score
            stall = 0
        else:
            stall += 1
            if stall >= STALL_LIMIT:
                # restart drift from the archive — don't let the parent
                # wander forever in a bloated dead-end
                parent, parent_score = best_ever, best_ever_score
                stall = 0

    return {'best': best_ever,
            'best_score': best_ever_score,
            'trajectory': trajectory}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--gens', type=int, default=100)
    ap.add_argument('--lam', type=int, default=8)
    ap.add_argument('--seed', type=int, default=0)
    ap.add_argument('--initial', default='')
    ap.add_argument('--scenario', default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'scenarios', 'fast_spike.yaml'))
    args = ap.parse_args()

    scn = load_scenario(args.scenario)
    print(f'evolving on {scn["name"]} — gens={args.gens} λ={args.lam} '
          f'seed={args.seed} initial={args.initial!r}')
    print(f"{'gen':>4} {'score':>12} {'feas':>5}  genome")
    print('-' * 60)

    result = evolve(scn, args.gens, args.lam, args.seed, args.initial)
    last = None
    for gen, sc, feas, g in result['trajectory']:
        if last is None or sc > last:
            print(f"{gen:>4d} {sc:>12.2f} {feas!s:>5}  {g!r}")
            last = sc

    print('-' * 60)
    bs = result['best_score']
    print(f"best:  {result['best']!r}")
    print(f"       score={bs['score']:.2f}  feasible={bs['feasible']}  "
          f"latency={bs['latency_sum']}  fp={bs['fp_count']}  miss={bs['miss_count']}")


if __name__ == '__main__':
    main()
