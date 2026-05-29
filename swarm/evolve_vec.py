"""
evolve_vec.py — (1+λ) ES in continuous vector space.

The optimizer mutates vector-DNA; each candidate is decoded to RPN by
swarm/genome_vec and scored by swarm/fitness. The optimizer never sees
the RPN directly — search happens entirely in the embedding space, the
fabric stores only the decoded RPN, and the executor (swarm/genome.py)
runs RPN. That is the middle path: alien substrate where it matters
(search), readable substrate where it matters (audit + execution).

Carries the lessons from swarm/evolve.py: drift + best-ever archive +
stall-restart, because the fitness landscape still has flat regions in
vector space (multiple vector configurations decode to the same string).
Selection's parsimony tiebreaker compares the DECODED string length, not
the vector list length — what we ship is the RPN.
"""

import random

from swarm.fitness import score
from swarm.genome_vec import (
    decode, encode, mutate_vector, random_vectors, tokens_to_vectors,
)


DRIFT_PROB = 0.5
STALL_LIMIT = 30


def _select(pool, parent_vecs, parent_score, parent_decoded, rng):
    """pool: list of (score_dict, vectors, decoded_str). Pick the next
    parent. On strict improvement, parsimony tiebreak by decoded length.
    On tied score (plateau), drift to ANY different tied child — NO
    parsimony, because pulling toward shorter on a flat plateau strangles
    the structure-accumulation phase."""
    top_score = max(c[0]['score'] for c in pool)
    candidates = [c for c in pool
                  if c[0]['score'] == top_score
                  and c[2] != parent_decoded]
    if not candidates:
        return parent_vecs, parent_score, parent_decoded

    if top_score > parent_score['score']:
        # strict improvement: shortest wins
        shortest = min(len(c[2]) for c in candidates)
        finalists = [c for c in candidates if len(c[2]) == shortest]
        pick_score, pick_vecs, pick_decoded = rng.choice(finalists)
        return pick_vecs, pick_score, pick_decoded

    if top_score == parent_score['score'] and rng.random() < DRIFT_PROB:
        # plateau drift: any tied child, length-blind
        pick_score, pick_vecs, pick_decoded = rng.choice(candidates)
        return pick_vecs, pick_score, pick_decoded

    return parent_vecs, parent_score, parent_decoded


def evolve(scenarios, generations=200, lam=12, seed=0,
           initial_vecs=None, initial_genome=''):
    """Run (1+λ) ES in vector space. Returns the BEST-EVER decoded RPN
    genome plus its fitness + the full trajectory."""
    rng = random.Random(seed)

    if initial_vecs is not None:
        parent = initial_vecs
    elif initial_genome:
        parent = tokens_to_vectors(encode(initial_genome))
    else:
        parent = random_vectors(rng)

    parent_decoded = decode(parent)
    parent_score = score(parent_decoded, scenarios)
    best_vecs, best_score, best_decoded = parent, parent_score, parent_decoded
    stall = 0
    trajectory = [(0, parent_score['score'], parent_decoded)]

    for gen in range(1, generations + 1):
        pool = []
        for _ in range(lam):
            child = mutate_vector(parent, rng)
            cd = decode(child)
            pool.append((score(cd, scenarios), child, cd))

        parent, parent_score, parent_decoded = _select(
            pool, parent, parent_score, parent_decoded, rng)
        trajectory.append((gen, parent_score['score'], parent_decoded))

        # Archive parsimony ONLY at strict improvement, NOT on tied scores —
        # otherwise the empty/short genome on the -1000 plateau permanently
        # outranks longer drift candidates that have actual structure for
        # future mutations to climb from.
        strict_improvement = parent_score['score'] > best_score['score']
        # At equal score, parsimony tiebreaker ONLY kicks in once the parent
        # is at the apparent optimum (score >= -0.01 → it found a HIT).
        # Before that, longer is better — it gives mutation more substrate.
        at_optimum_tied = (
            parent_score['score'] == best_score['score']
            and parent_score['score'] >= -0.01
            and len(parent_decoded) < len(best_decoded))
        if strict_improvement or at_optimum_tied:
            best_vecs, best_score, best_decoded = (
                parent, parent_score, parent_decoded)
            stall = 0
        else:
            stall += 1
            if stall >= STALL_LIMIT:
                parent = best_vecs
                parent_score = best_score
                parent_decoded = best_decoded
                stall = 0

    return {'best_vecs': best_vecs,
            'best_genome': best_decoded,
            'best_score': best_score,
            'trajectory': trajectory}
