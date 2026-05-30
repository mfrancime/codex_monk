"""
wargame.py — Kubernetes co-evolution wargame driver (eval / orchestration tier).

  ┌─────────────────────────────────────────────────────────────────────┐
  │ DESIGN-LAW NOTE (read CLAUDE.md first).                              │
  │                                                                     │
  │ This file adds NO new agent class, NO new probe, NO new opcode, and │
  │ does NOT touch the swarm/ package. It is eval-tier tooling — a       │
  │ sibling of boot.py / viz.py / eval/test_*.py — that drives the       │
  │ ALREADY-EXISTING (1+λ) evolutionary engine (swarm.evolve) and the    │
  │ ALREADY-EXISTING k8s probe opcode tables (cgroup_pods Π, k8s_api K,  │
  │ quorum Γ) to evolve detector genomes against synthetic Kubernetes    │
  │ failure scenarios.                                                   │
  │                                                                     │
  │ The only reason this lives outside swarm/fitness.py: fitness.py's    │
  │ frame builder is kernel-domain (PSI/swap/mem) only. Rather than edit │
  │ the shipped oracle, the domain-general scorer is kept here. New      │
  │ behavior still = new genome strings (evolved, below) + new YAML      │
  │ (scenarios/k8s_*.yaml). No Python defines any new swarm behavior.    │
  └─────────────────────────────────────────────────────────────────────┘

THE BATTLEGROUND IS A KUBERNETES CLUSTER. Three fronts, each its own probe
domain and its own independently-evolving Blue champion genome:

  pods         — cgroup_pods (Π): per-pod memory pressure + OOMKills.
  controlplane — k8s_api (K):     apiserver / nodes / events health.
  cluster      — quorum (Γ):      cross-fabric cluster correlation. (added later)

Red Team is an escalating ladder of attack scenarios per front (cumulative:
rung R scores the champion against scenarios[0..R]). Blue Team is the genome,
evolved by the shipped elitist (1+λ) ES to stay FEASIBLE (zero false positive
on calm/decoy phases) AND catch every attack within its deadline. When Blue
masters a rung (feasible, zero miss/near/half), Red escalates to the next rung.

Each invocation runs ONE round per front (evolve the current rung, escalate on
mastery), appends to the lineage, and regenerates the web tab's data file.

Usage:
    python wargame.py                  # one round per front
    python wargame.py --rounds 6       # six rounds per front, back to back
    python wargame.py --front pods     # only the pods front
"""

import argparse
import json
import os
import time

from swarm import probes
from swarm.genome import interpret
from swarm.fitness import load_scenario, _make_frame
from swarm import evolve as ev


ROOT = os.path.dirname(os.path.abspath(__file__))
LINEAGE = os.path.join(ROOT, 'wargame', 'lineage.jsonl')
CHAMPIONS_MD = os.path.join(ROOT, 'wargame', 'CHAMPIONS.md')
WEB_JSON = os.path.join(ROOT, 'web', 'wargame.json')

# eager-import the probe modules so probes.get(domain) resolves their opcodes
import swarm.probes.cgroup_pods   # noqa: F401
import swarm.probes.k8s_api       # noqa: F401
import swarm.probes.quorum        # noqa: F401
import swarm.probes.kernel        # noqa: F401


# ── scoring constants (mirror swarm/fitness.py so verdicts are comparable) ──
FP_BUDGET = 0
MISS_CAP = 1000
NEAR_MISS = 500
HALF_MISS = 250
ALPHA = 0.01
INFEASIBLE = -1e9


# ── the fronts: probe domain + mutation LOAD alphabet + Red's attack ladder ─
#
# `ladder` is a list of rungs; each rung is the list of scenario files scored
# CUMULATIVELY at that rung (rung R = ladder[R] which already includes the
# prior rungs' scenarios). Authored so each rung has a known feasible genome,
# i.e. the arms race is winnable — Blue can always climb.

# Each front: probe `domain`, its `loads` (the load opcodes the genome may use),
# the emit `sev`/`codes` tags it may page (kept minimal per front so the search
# is focused AND so the evolver can actually express the right verdict — the
# shipped swarm.evolve emit alphabet is kernel-only and has no g/p/d/x), and the
# cumulative Red `ladder`. `flavor` is the fun label shown in the war-room tab.
FRONTS = {
    'pods': {
        'domain': 'cgroup_pods', 'flavor': '🫛 PODS — OOMKiller & memory pressure',
        'loads': ['Πs', 'Πf', 'Πm', 'Πo', 'Πn', 'Πp', 'Π?'],
        'sev': ['O', 'W', 'C'], 'codes': ['o', 'p'],
        'ladder': [
            ['scenarios/k8s_pod_creep.yaml'],
            ['scenarios/k8s_pod_creep.yaml', 'scenarios/k8s_pod_noisy_decoy.yaml'],
            ['scenarios/k8s_pod_creep.yaml', 'scenarios/k8s_pod_noisy_decoy.yaml',
             'scenarios/k8s_pod_oom.yaml'],
            ['scenarios/k8s_pod_creep.yaml', 'scenarios/k8s_pod_noisy_decoy.yaml',
             'scenarios/k8s_pod_oom.yaml', 'scenarios/k8s_pod_multi.yaml'],
        ],
    },
    'nodes': {
        'domain': 'k8s_api', 'flavor': '🖥️ NODES — kubelets going dark',
        'loads': ['Ka', 'Kl', 'Kn', 'Kx', 'Ke', 'Kd', 'K?'],
        'sev': ['O', 'W', 'C'], 'codes': ['o', 'x'],
        'ladder': [
            ['scenarios/k8s_node_notready.yaml'],
            ['scenarios/k8s_node_notready.yaml', 'scenarios/k8s_node_cascade.yaml'],
            ['scenarios/k8s_node_notready.yaml', 'scenarios/k8s_node_cascade.yaml',
             'scenarios/k8s_node_cordon_decoy.yaml'],
        ],
    },
    'apiserver': {
        'domain': 'k8s_api', 'flavor': '👑 APISERVER — control-plane decapitation',
        'loads': ['Ka', 'Kl', 'Kn', 'Kx', 'Ke', 'Kd', 'K?'],
        'sev': ['O', 'W', 'C'], 'codes': ['o', 'g'],
        'ladder': [
            ['scenarios/k8s_api_down.yaml'],
            ['scenarios/k8s_api_down.yaml', 'scenarios/k8s_api_warnstorm_decoy.yaml'],
            # Red escalation: empty-but-healthy cluster punishes the node-count
            # cheat (Kn≥→Cg), forcing a gate on api.healthy directly.
            ['scenarios/k8s_api_down.yaml', 'scenarios/k8s_api_warnstorm_decoy.yaml',
             'scenarios/k8s_api_empty_cluster_decoy.yaml'],
        ],
    },
    'etcd': {
        'domain': 'k8s_api', 'flavor': '🧠 ETCD — the cluster brain stalls',
        'loads': ['Ka', 'Kl', 'Kn', 'Kx', 'Ke', 'Kd', 'K?'],
        'sev': ['O', 'W', 'C'], 'codes': ['o', 'g'],
        'ladder': [
            ['scenarios/k8s_etcd_wobble.yaml'],
            ['scenarios/k8s_etcd_wobble.yaml', 'scenarios/k8s_etcd_splitbrain.yaml'],
            ['scenarios/k8s_etcd_wobble.yaml', 'scenarios/k8s_etcd_splitbrain.yaml',
             'scenarios/k8s_etcd_compaction_decoy.yaml'],
            # Red escalation: node-flap with healthy latency punishes the
            # not-ready/degraded cheat (KdKx→Cg→Wg), forcing real latency gating.
            ['scenarios/k8s_etcd_wobble.yaml', 'scenarios/k8s_etcd_splitbrain.yaml',
             'scenarios/k8s_etcd_compaction_decoy.yaml',
             'scenarios/k8s_etcd_node_flap_decoy.yaml'],
        ],
    },
    'scheduler': {
        'domain': 'k8s_api', 'flavor': '📋 SCHEDULER — pods stuck Pending',
        'loads': ['Ka', 'Kl', 'Kn', 'Kx', 'Ke', 'Kd', 'K?'],
        'sev': ['O', 'W', 'C'], 'codes': ['o', 'd'],
        'ladder': [
            ['scenarios/k8s_sched_pileup.yaml'],
            ['scenarios/k8s_sched_pileup.yaml', 'scenarios/k8s_sched_storm.yaml'],
            ['scenarios/k8s_sched_pileup.yaml', 'scenarios/k8s_sched_storm.yaml',
             'scenarios/k8s_sched_rollout_decoy.yaml'],
            # Red escalation: nodes lost but pods rescheduled (degraded stays
            # low) punishes the not-ready cheat (Kx→Cd), forcing pure degraded
            # thresholds.
            ['scenarios/k8s_sched_pileup.yaml', 'scenarios/k8s_sched_storm.yaml',
             'scenarios/k8s_sched_rollout_decoy.yaml',
             'scenarios/k8s_sched_nodeloss_decoy.yaml'],
        ],
    },
}


# ── domain-general frame replay + scoring ───────────────────────────────────

def _frame_for(scn, phase):
    """Build the Frame dict for a scenario phase. Kernel-domain scenarios use
    the PSI/swap shorthand (expanded via the shipped builder); k8s-domain
    scenarios carry the raw Frame dict straight through to the interpreter."""
    f = phase['frame']
    if scn.get('domain', 'kernel') == 'kernel':
        return _make_frame(f['psi_some'], f['psi_full'],
                           f['used_pct'], f['swap_present'])
    return dict(f)


def _phase_for(tick, scn):
    for ph in scn['phases']:
        lo, hi = ph['range']
        if lo <= tick <= hi:
            return ph
    return None


def replay(genome, scn, opcodes):
    """Edge-emit walk of `genome` over a scenario timeline. Returns the ordered
    [(tick, sev, code)] verdict changes, identical in spirit to fitness.replay."""
    edges = []
    last = ('OK', 'OK')
    for tick in range(scn['ticks_total']):
        ph = _phase_for(tick, scn)
        if ph is None:
            continue
        frame = _frame_for(scn, ph)
        sev, code = interpret(genome, frame, opcodes)
        if (sev, code) != last:
            edges.append((tick, sev, code))
            last = (sev, code)
    return edges


def score(genome, scenarios, opcodes):
    """Domain-general port of swarm.fitness.score. FP on any calm/decoy phase
    is a hard infeasibility; per truth phase the best-matching non-OK edge in
    the deadline window grades hit / half / near / miss."""
    latency_sum = fp_count = miss = near = half = 0
    hits = []
    per = []
    for scn in scenarios:
        edges = replay(genome, scn, opcodes)
        for ph in scn['phases']:
            truth = ph.get('truth')
            lo, hi = ph['range']
            if truth is None:
                fps = [e for e in edges if lo <= e[0] <= hi and e[1:] != ('OK', 'OK')]
                fp_count += len(fps)
                if fps:
                    per.append({'scn': scn['name'], 'phase': ph['name'],
                                'status': 'FP', 'detail': f'{fps[0][1]}:{fps[0][2]}'})
                continue
            deadline = ph.get('deadline_ticks', hi - lo + 1)
            wsev, wcode = truth['sev'], truth['code']
            whi = lo + deadline
            best_m, best_t, best_e = -1, None, None
            for (t, s, c) in edges:
                if not (lo <= t <= whi) or (s, c) == ('OK', 'OK'):
                    continue
                m = (1 if s == wsev else 0) + (1 if c == wcode else 0)
                if m > best_m:
                    best_m, best_t, best_e = m, t, (s, c)
            if best_m == 2:
                latency_sum += best_t - lo
                hits.append({'scn': scn['name'], 'latency': best_t - lo})
                per.append({'scn': scn['name'], 'phase': ph['name'],
                            'status': 'HIT', 'latency': best_t - lo})
            elif best_m == 1:
                half += 1
                per.append({'scn': scn['name'], 'phase': ph['name'],
                            'status': 'HALF', 'detail': f'{best_e[0]}:{best_e[1]}'})
            elif best_m == 0:
                near += 1
                per.append({'scn': scn['name'], 'phase': ph['name'],
                            'status': 'NEAR', 'detail': f'{best_e[0]}:{best_e[1]}'})
            else:
                miss += 1
                per.append({'scn': scn['name'], 'phase': ph['name'],
                            'status': 'MISS', 'want': f'{wsev}:{wcode}'})
    feasible = fp_count <= FP_BUDGET
    sc = (INFEASIBLE if not feasible else
          -(latency_sum + MISS_CAP * miss + NEAR_MISS * near
            + HALF_MISS * half + ALPHA * fp_count))
    return {'feasible': feasible, 'score': sc, 'latency_sum': latency_sum,
            'fp': fp_count, 'miss': miss, 'near': near, 'half': half,
            'hits': hits, 'per': per}


# ── HIGHER-DIMENSIONAL ALGORITHM: (1+λ) ES in continuous embedding space ────
#
# This is the vector path — the same idea as swarm/genome_vec.py + evolve_vec.py
# but domain-general (k8s token alphabets the shipped kernel-only table can't
# hold). Every opcode token is a point in R^8 = (4-d category) ⊕ (4-d role).
# A genome of L tokens is therefore ONE point in R^(8·L); the search moves
# through that high-dimensional space by isotropic gaussian noise and only
# DECODES (nearest token per slot) to the present-time executable genome at
# scoring time. Small noise stays in-category (neutral drift); larger noise
# crosses to a functionally adjacent opcode — a smoother landscape than 1-D
# character edits. (The "throw it into a higher dimension, let structure form,
# compress back to the present" path.)

import math

VEC_DIM = 8
_CAT = {
    'load': (1.0, 0.0, 0.0, 0.0), 'lit': (0.0, 1.0, 0.0, 0.0),
    'comp': (0.0, 0.0, 1.0, 0.0), 'bool': (0.0, 0.0, 0.0, 1.0),
    'emit': (0.7, 0.0, 0.0, 0.0), 'sev': (0.7, 0.7, 0.0, 0.0),
    'code': (0.0, 0.7, 0.7, 0.0), 'ctrl': (0.0, 0.0, 0.0, 0.0),
}
_VEC_LITS = ['‡2', '‡5', '‡10', '‡100', '‡1000', '‡2000', '‡4000']
# Literal choices for the STRING mutator: small digits + the k8s-meaningful
# thresholds. The shipped ev.mut_insert_literal draws ‡N with random N∈1..99,
# so hitting a needed threshold (‡2 to clear a degraded=2 decoy, ‡10, ‡1000 for
# latency) is ~1%. Drawing from this closed set makes thresholds findable.
_LIT_CHOICES = ['0', '1', '2', '3', '5'] + _VEC_LITS
_VEC_DIGITS = list('0123456789')
_VEC_COMPS = ['>', '<', '≥', '≤', '≡', '≠']
_VEC_BOOLS = ['∧', '∨', '¬']


def _role4(i, n):
    if n <= 1:
        return (0.0, 0.0, 0.0, 0.0)
    th = 2 * math.pi * i / n
    return (math.cos(th), math.sin(th), 0.0, 0.0)


def _build_embed(spec):
    """Per-front embedding table: {token: 8-tuple}. Tokens = the front's loads,
    literals, comparators, booleans, EMIT, its sev+code tags, sep, noop."""
    t = {}
    groups = [
        ('load', spec['loads']), ('lit', _VEC_DIGITS),
        ('comp', _VEC_COMPS), ('bool', _VEC_BOOLS),
        ('sev', spec['sev']), ('code', spec['codes']),
    ]
    for cat, toks in groups:
        for i, tok in enumerate(toks):
            t[tok] = _CAT[cat] + _role4(i, len(toks))
    for i, tok in enumerate(_VEC_LITS):       # ‡N offset so they don't collide w/ digits
        t[tok] = tuple(c + 0.2 for c in _CAT['lit']) + _role4(i, len(_VEC_LITS))
    t['→'] = _CAT['emit'] + _role4(0, 1)
    t[';'] = _CAT['ctrl'] + (1.0, 0.0, 0.0, 0.0)
    t[''] = _CAT['ctrl'] + (0.0, 0.0, 0.0, 0.0)
    return t


def _nearest(vec, table):
    best, bd = '', float('inf')
    for tok, e in table.items():
        d = sum((a - b) ** 2 for a, b in zip(vec, e))
        if d < bd:
            bd, best = d, tok
    return best


def _decode_vecs(vecs, table):
    return ''.join(_nearest(v, table) for v in vecs)


def _rand_vecs(rng, table, length=None):
    toks = list(table)
    length = length or rng.randint(4, 8)
    return [tuple(e + rng.gauss(0, 0.1) for e in table[rng.choice(toks)])
            for _ in range(length)]


def _mut_vecs(vecs, rng, table, sigma=0.3, ins_p=0.28, del_p=0.14, max_len=12):
    toks = list(table)
    out = [list(v) for v in vecs]
    r = rng.random()
    if r < ins_p and len(out) < max_len:
        out.insert(rng.randrange(len(out) + 1),
                   [e + rng.gauss(0, 0.1) for e in table[rng.choice(toks)]])
    elif r < ins_p + del_p and out:
        out.pop(rng.randrange(len(out)))
    elif out:
        i = rng.randrange(len(out))
        out[i] = [x + rng.gauss(0, sigma) for x in out[i]]
    return [tuple(v) for v in out]


def evolve_front_vec(scenarios, opcodes, spec, gens, lam, seed, initial):
    """(1+λ) ES in R^(8·L). Returns (decoded_genome, score_dict, dim)."""
    import random
    rng = random.Random(seed)
    table = _build_embed(spec)
    if initial:
        parent = [table.get(tok, table['']) for tok in _tokenize(initial, table)]
        if not parent:
            parent = _rand_vecs(rng, table)
    else:
        parent = _rand_vecs(rng, table)
    pdec = _decode_vecs(parent, table)
    pscore = score(pdec, scenarios, opcodes)
    best, bscore, bdec = parent, pscore, pdec
    stall = 0
    for _ in range(gens):
        pool = []
        for _ in range(lam):
            ch = _mut_vecs(parent, rng, table)
            cd = _decode_vecs(ch, table)
            pool.append((score(cd, scenarios, opcodes), ch, cd))
        top = max(s['score'] for s, _, _ in pool)
        cands = [(s, v, d) for s, v, d in pool if s['score'] == top and d != pdec]
        if cands:
            if top > pscore['score']:
                shortest = min(len(d) for _, _, d in cands)
                fin = [c for c in cands if len(c[2]) == shortest]
                ps, pv, pd = rng.choice(fin)
                parent, pscore, pdec = pv, ps, pd
            elif top == pscore['score'] and rng.random() < 0.5:
                ps, pv, pd = rng.choice(cands)
                parent, pscore, pdec = pv, ps, pd
        strict = pscore['score'] > bscore['score']
        tie_opt = (pscore['score'] == bscore['score']
                   and pscore['score'] >= -0.01 and len(pdec) < len(bdec))
        if strict or tie_opt:
            best, bscore, bdec, stall = parent, pscore, pdec, 0
        else:
            stall += 1
            if stall >= 30:
                parent, pscore, pdec, stall = best, bscore, bdec, 0
    return bdec, bscore, VEC_DIM * max(1, len(best))


def _tokenize(g, table):
    """Greedy tokenize a genome string against a front's token table (2-char
    loads, ‡N lits, single chars). Used to seed the vector parent from a string."""
    toks, i, n = [], 0, len(g)
    while i < n:
        if i + 1 < n and g[i:i + 2] in table:
            toks.append(g[i:i + 2]); i += 2; continue
        if g[i] == '‡':
            j = i + 1
            while j < n and g[j].isdigit():
                j += 1
            lit = g[i:j]
            toks.append(lit if lit in table else
                        min(_VEC_LITS, key=lambda t: abs(int(t[1:]) - int(g[i + 1:j] or 0))))
            i = j; continue
        toks.append(g[i] if g[i] in table else ''); i += 1
    return toks


# ── domain-aware STRING mutation (reuses every swarm.evolve operator; only the
#    LOAD + EMIT alphabets are swapped to the front's opcodes) ────────────────

def _mutate(genome, rng, loads, sevs, codes):
    """Domain-aware mutation. Reuses every swarm.evolve operator that is
    genome-string-generic (literal/op/delete/perturb), and supplies its own
    LOAD insertion (front's load opcodes) and EMIT insertion (front's sev+code
    tags) — because swarm.evolve's emit alphabet is kernel-only (no g/p/d/x)."""
    def ins_load(g):
        return ev._insert_at(g, rng.choice(loads), rng)

    def ins_emit(g):
        return ev._insert_at(g, '→' + rng.choice(sevs) + rng.choice(codes), rng)

    def ins_lit(g):
        return ev._insert_at(g, rng.choice(_LIT_CHOICES), rng)

    def ins_rule(g):
        # macro-mutation: insert a COMPLETE plausible rule in one step —
        # <load><literal><comparator>→<sev><code>. A multi-rule genome (e.g.
        # Kx→CdKd‡2>→Wd) needs a 4-token rule that only pays off when complete;
        # single-token mutation + elitism can never assemble it, but this lands
        # it atomically. The key to escaping deceptive 1-rule local optima.
        rule = (rng.choice(loads) + rng.choice(_LIT_CHOICES)
                + rng.choice(['>', '<', '≥', '≤'])
                + '→' + rng.choice(sevs) + rng.choice(codes))
        return ev._insert_at(g, rule, rng)

    ops = [
        (ins_rule,           0.18),
        (ins_emit,           0.16),
        (ins_load,           0.16),
        (ins_lit,            0.13),
        (ev.mut_insert_op,   0.13),
        (ev.mut_delete,      0.13),
        (ev.mut_swap_sev,    0.05),
        (ev.mut_swap_code,   0.04),
        (ev.mut_perturb_literal, 0.02),
    ]
    local = {ins_load, ins_emit, ins_lit, ins_rule}
    n = rng.choices([1, 2, 3], weights=[0.6, 0.3, 0.1])[0]
    g = genome
    for _ in range(n):
        r, acc = rng.random(), 0.0
        for fn, w in ops:
            acc += w
            if r < acc:
                g = fn(g) if fn in local else fn(g, rng)
                break
    return g[:ev.MAX_LEN]


def evolve_front(scenarios, opcodes, loads, sevs, codes, gens, lam, seed, initial):
    """Elitist (1+λ) with archive + stall-restart — the same shape as
    swarm.evolve.evolve, but parameterized by domain scorer/mutator."""
    import random
    rng = random.Random(seed)
    parent = initial
    pscore = score(parent, scenarios, opcodes)
    best, bscore = parent, pscore
    stall = 0
    for _ in range(gens):
        pool = [(score((c := _mutate(parent, rng, loads, sevs, codes)),
                       scenarios, opcodes), c)
                for _ in range(lam)]
        top = max(s['score'] for s, _ in pool)
        cands = [(s, g) for s, g in pool if s['score'] == top and g != parent]
        if cands:
            shortest = min(len(g) for _, g in cands)
            fin = [(s, g) for s, g in cands if len(g) == shortest]
            ps, pg = rng.choice(fin)
            if top > pscore['score'] or (top == pscore['score'] and rng.random() < 0.5):
                parent, pscore = pg, ps
        improved = (pscore['score'] > bscore['score'] or
                    (pscore['score'] == bscore['score'] and len(parent) < len(best)))
        if improved:
            best, bscore, stall = parent, pscore, 0
        else:
            stall += 1
            if stall >= 30:
                parent, pscore, stall = best, bscore, 0
    return best, bscore


# ── lineage I/O ─────────────────────────────────────────────────────────────

def _read_lineage():
    if not os.path.isfile(LINEAGE):
        return []
    out = []
    with open(LINEAGE) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


def _last_for(lineage, front):
    for rec in reversed(lineage):
        if rec.get('front') == front:
            return rec
    return None


def _append(rec):
    os.makedirs(os.path.dirname(LINEAGE), exist_ok=True)
    with open(LINEAGE, 'a') as f:
        f.write(json.dumps(rec) + '\n')


# ── run one round for one front ─────────────────────────────────────────────

def run_round(front, gens, lam):
    spec = FRONTS[front]
    domain = spec['domain']
    opcodes = probes.get(domain).opcodes
    ladder = spec['ladder']
    max_rung = len(ladder) - 1

    lineage = _read_lineage()
    last = _last_for(lineage, front)
    if last is None:
        rung, champion, champ_vec, attempt, rnum = 0, '', '', 0, 1
    else:
        rnum = last['round'] + 1
        cv = last.get('champion_vec', '')
        if last['mastered'] and last['rung'] < max_rung:
            rung, champion, champ_vec, attempt = last['rung'] + 1, last['champion'], cv, 0
        else:
            rung, champion, champ_vec, attempt = \
                last['rung'], last['champion'], cv, last['attempt'] + 1

    scns = [load_scenario(os.path.join(ROOT, p)) for p in ladder[rung]]
    for s, p in zip(scns, ladder[rung]):
        s['domain'] = domain
        s.setdefault('name', os.path.basename(p))
    seed = rung * 10_000 + attempt * 97 + 7
    # more attempts on a stuck rung → search harder
    g = gens + attempt * 200

    # DISCRETE (1-D) champion
    champ, cs = evolve_front(scns, opcodes, spec['loads'], spec['sev'],
                             spec['codes'], g, lam, seed, champion)
    mastered = cs['feasible'] and cs['miss'] == 0 and cs['near'] == 0 and cs['half'] == 0
    # Escape local optima: grinding FROM a trapped 1-rule champion is monotonic
    # but pinned (elitism can't take the several non-improving mutations a 2nd
    # rule needs). On a stuck grind, also run a FRESH search from empty with a
    # bigger lambda and keep whichever champion scores better — never regresses.
    if not mastered and attempt >= 1:
        fchamp, fcs = evolve_front(scns, opcodes, spec['loads'], spec['sev'],
                                   spec['codes'], g, lam * 2, seed + 50_000, '')
        if fcs['score'] > cs['score']:
            champ, cs = fchamp, fcs
            mastered = (cs['feasible'] and cs['miss'] == 0
                        and cs['near'] == 0 and cs['half'] == 0)
    # HIGHER-DIMENSIONAL (vector R^8/slot) champion — the same arms race run in
    # continuous embedding space, decoded back to a present-time genome.
    # Cross-representation gene flow: if last round the higher-D track fell
    # materially behind the discrete one, seed it from the discrete champion
    # (encode the "weights" into the vector representation) so it isn't stuck.
    vec_seed = champ_vec or champion
    if last is not None and last.get('score_vec') is not None \
            and last['score_vec'] < last['score'] - 200:
        vec_seed = champion or champ_vec
    vchamp, vcs, vdim = evolve_front_vec(scns, opcodes, spec, g, lam,
                                         seed + 1, vec_seed)
    vmastered = vcs['feasible'] and vcs['miss'] == 0 and vcs['near'] == 0 and vcs['half'] == 0

    prev = last['score'] if last else None
    rec = {
        'round': rnum, 'ts': time.time(), 'front': front, 'domain': domain,
        'rung': rung, 'rungs_total': max_rung + 1, 'attempt': attempt,
        'label': _rung_label(front, rung),
        'attacks': [os.path.basename(p) for p in ladder[rung]],
        'champion': champ, 'genome_len': len(champ),
        'score': cs['score'], 'feasible': cs['feasible'],
        'fp': cs['fp'], 'miss': cs['miss'], 'near': cs['near'], 'half': cs['half'],
        'latency_sum': cs['latency_sum'], 'hits': cs['hits'], 'per': cs['per'],
        'mastered': mastered, 'gens': g, 'lam': lam, 'seed': seed,
        'delta_score': (cs['score'] - prev) if prev is not None else None,
        # higher-dimensional track
        'champion_vec': vchamp, 'score_vec': vcs['score'],
        'feasible_vec': vcs['feasible'], 'vec_mastered': vmastered,
        'vec_dim': vdim, 'per_vec': vcs['per'],
    }
    _append(rec)
    return rec


def _rung_label(front, rung):
    return f'{front} · rung {rung + 1}/{len(FRONTS[front]["ladder"])}'


# ── regenerate the web tab data + human-readable champions ──────────────────

def regen_web():
    lineage = _read_lineage()
    fronts = {}
    for name, spec in FRONTS.items():
        hist = [r for r in lineage if r['front'] == name]
        last = hist[-1] if hist else None
        mastered_rung = -1
        for r in hist:
            if r['mastered']:
                mastered_rung = max(mastered_rung, r['rung'])
        fronts[name] = {
            'domain': spec['domain'],
            'flavor': spec.get('flavor', name),
            'rungs_total': len(spec['ladder']),
            'current_rung': last['rung'] if last else 0,
            'mastered_rung': mastered_rung,
            'champion': last['champion'] if last else '',
            'champion_score': last['score'] if last else None,
            'champion_feasible': last['feasible'] if last else None,
            'per': last['per'] if last else [],
            # higher-dimensional (vector-space) champion, decoded to present-time RPN
            'champion_vec': last.get('champion_vec', '') if last else '',
            'score_vec': last.get('score_vec') if last else None,
            'feasible_vec': last.get('feasible_vec') if last else None,
            'vec_dim': last.get('vec_dim') if last else None,
            'per_vec': last.get('per_vec', []) if last else [],
            'ladder': [{'rung': i, 'attacks': [os.path.basename(p) for p in rng]}
                       for i, rng in enumerate(spec['ladder'])],
            'history': hist[-60:],
        }
    doc = {
        'updated': time.time(),
        'title': 'KUBERNETES WARGAME — Red Team vs Blue genome co-evolution',
        'rounds_total': len(lineage),
        'fronts': fronts,
    }
    os.makedirs(os.path.dirname(WEB_JSON), exist_ok=True)
    with open(WEB_JSON, 'w') as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)

    # human-readable champions.md
    lines = ['# Kubernetes Wargame — reigning champions\n',
             f'_updated {time.strftime("%Y-%m-%d %H:%M:%S")} · '
             f'{len(lineage)} rounds total_\n']
    for name, fr in fronts.items():
        lines.append(f'\n## {name}  (`{fr["domain"]}`)\n')
        lines.append(f'- rung **{fr["current_rung"]+1}/{fr["rungs_total"]}** · '
                     f'mastered **{fr["mastered_rung"]+1}/{fr["rungs_total"]}**\n')
        lines.append(f'- champion genome: `{fr["champion"]}`  '
                     f'(score {fr["champion_score"]}, '
                     f'feasible {fr["champion_feasible"]})\n')
    os.makedirs(os.path.dirname(CHAMPIONS_MD), exist_ok=True)
    with open(CHAMPIONS_MD, 'w') as f:
        f.write(''.join(lines))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rounds', type=int, default=1)
    ap.add_argument('--front', default='all')
    ap.add_argument('--gens', type=int, default=500)
    ap.add_argument('--lam', type=int, default=16)
    args = ap.parse_args()

    fronts = list(FRONTS) if args.front == 'all' else [args.front]
    for _ in range(args.rounds):
        for fr in fronts:
            rec = run_round(fr, args.gens, args.lam)
            flag = 'MASTERED' if rec['mastered'] else f"grind#{rec['attempt']}"
            print(f"[{fr:>10}] r{rec['round']:>2} rung "
                  f"{rec['rung']+1}/{rec['rungs_total']} {flag:>10}  "
                  f"1D score={rec['score']:>8.1f} feas={rec['feasible']!s:>5}  «{rec['champion']}»")
            print(f"{'':>13}        ℝ^{rec['vec_dim']:<4}  "
                  f"vec score={rec['score_vec']:>8.1f} feas={rec['feasible_vec']!s:>5}  «{rec['champion_vec']}»")
    regen_web()
    print(f'\nweb/wargame.json + wargame/CHAMPIONS.md regenerated.')


if __name__ == '__main__':
    main()
