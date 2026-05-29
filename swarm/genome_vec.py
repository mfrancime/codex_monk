"""
genome_vec.py — the search side of codex_monk's middle-path DNA.

The agent executes Unicode-RPN (swarm/genome.py). But search runs HERE in
continuous vector space. Every token has a fixed point in an 8-d embedding;
a vector-DNA is a list of slot vectors; decoding picks the nearest token
per slot. Gaussian noise on a slot is a continuous mutation that, when
the embedding clusters tokens by function, usually keeps the same token
and occasionally crosses to a functionally adjacent one — a much smoother
landscape than discrete opcode insertion/deletion.

Embedding structure (the load-bearing design choice):
  Each token's 8-d vector = (4-d category) ⊕ (4-d role-within-category).

  Category dims encode WHAT KIND of opcode (load / literal / comparator /
  boolean / emit / sev tag / code tag / control).
  Role dims encode WHICH ONE within the category (psi.some vs psi.full,
  W vs C, w vs c, ...).

  Small gaussian noise stays in-category (cheap, near-neutral changes —
  bumping `ψs` to `ψf`); larger noise crosses categories (rare bigger
  jumps — turning `ψs` into a comparator). The hand-crafted layout is what
  gives the smooth-landscape claim teeth without learning anything.

Stored representation is always Unicode-RPN — the fabric, the dna.{id}.*
slots, the interpreter, the audit trail all stay readable. Vectors only
exist inside the optimizer.
"""

import math
import random


# ── alphabet (mirrors swarm/evolve.py) ───────────────────────────────────

LOAD_TOKENS  = ['ψs', 'ψf', 'ψ?', '~u', '~a', '~S', '~s', 'κ?']
DIGIT_TOKENS = ['0', '1', '2', '3', '4', '5', '6', '7', '8', '9']
# Pre-chosen multi-digit literals at thresholds the host actually cares
# about — keeps the vocabulary closed so encoding round-trips.
LIT_TOKENS   = ['‡5', '‡10', '‡20', '‡30', '‡50', '‡85', '‡90']
COMP_TOKENS  = ['>', '<', '≥', '≤', '≡', '≠']
BOOL_TOKENS  = ['∧', '∨', '¬']
SEV_TOKENS   = ['O', 'I', 'W', 'C']
CODE_TOKENS  = ['o', 'a', 'w', 'n', 'c', 'l', 'L']
EMIT_TOK     = '→'
SEP_TOK      = ';'
NOOP_TOK     = ''      # decodes to empty string


# ── category coordinates (first 4 dims of every token's embedding) ──────

CAT_LOAD = (1.0, 0.0, 0.0, 0.0)
CAT_LIT  = (0.0, 1.0, 0.0, 0.0)
CAT_COMP = (0.0, 0.0, 1.0, 0.0)
CAT_BOOL = (0.0, 0.0, 0.0, 1.0)
CAT_EMIT = (0.7, 0.0, 0.0, 0.0)   # neighbour of LOAD (both push/read)
CAT_SEV  = (0.7, 0.7, 0.0, 0.0)
CAT_CODE = (0.0, 0.7, 0.7, 0.0)
CAT_CTRL = (0.0, 0.0, 0.0, 0.0)   # SEP and NOOP — near origin


DIM = 8


def _role(i, n):
    """Place index i out of n on a unit circle (in role-dim 0/1); leave
    role-dim 2/3 at 0 — reserved for future sub-clustering."""
    if n <= 1:
        return (0.0, 0.0, 0.0, 0.0)
    theta = 2 * math.pi * i / n
    return (math.cos(theta), math.sin(theta), 0.0, 0.0)


def _build_table():
    table = {}
    for i, t in enumerate(LOAD_TOKENS):
        table[t] = CAT_LOAD + _role(i, len(LOAD_TOKENS))
    for i, t in enumerate(DIGIT_TOKENS):
        table[t] = CAT_LIT + _role(i, len(DIGIT_TOKENS))
    for i, t in enumerate(LIT_TOKENS):
        # slight offset so digits and ‡N don't collide in the same cluster
        cat = tuple(c + 0.2 for c in CAT_LIT)
        table[t] = cat + _role(i, len(LIT_TOKENS))
    for i, t in enumerate(COMP_TOKENS):
        table[t] = CAT_COMP + _role(i, len(COMP_TOKENS))
    for i, t in enumerate(BOOL_TOKENS):
        table[t] = CAT_BOOL + _role(i, len(BOOL_TOKENS))
    for i, t in enumerate(SEV_TOKENS):
        table[t] = CAT_SEV + _role(i, len(SEV_TOKENS))
    for i, t in enumerate(CODE_TOKENS):
        table[t] = CAT_CODE + _role(i, len(CODE_TOKENS))
    table[EMIT_TOK] = CAT_EMIT + _role(0, 1)
    table[SEP_TOK]  = CAT_CTRL + (1.0, 0.0, 0.0, 0.0)
    table[NOOP_TOK] = CAT_CTRL + (0.0, 0.0, 0.0, 0.0)
    return table


_TABLE = _build_table()
_TOKEN_LIST = list(_TABLE.keys())


# ── encode: RPN string → tokens ──────────────────────────────────────────

def encode(genome_str):
    """Tokenize an RPN string. Unknown bytes become NOOP. The multi-digit
    `‡NN` literal is quantized to the nearest LIT_TOKEN so the round-trip
    has a closed vocabulary."""
    tokens = []
    i, n = 0, len(genome_str)
    while i < n:
        # 2-char loads (ψs, ψf, ψ?, ~u, ~a, ~S, ~s, κ?)
        if i + 1 < n:
            two = genome_str[i:i + 2]
            if two in _TABLE:
                tokens.append(two)
                i += 2
                continue
        # ‡ + digits literal (quantize to nearest known LIT_TOKEN)
        if genome_str[i] == '‡':
            j = i + 1
            while j < n and genome_str[j].isdigit():
                j += 1
            if j > i + 1:
                lit = genome_str[i:j]
                if lit in _TABLE:
                    tokens.append(lit)
                else:
                    n_val = int(genome_str[i + 1:j])
                    nearest = min(LIT_TOKENS,
                                  key=lambda t: abs(int(t[1:]) - n_val))
                    tokens.append(nearest)
                i = j
                continue
        # single char
        one = genome_str[i]
        tokens.append(one if one in _TABLE else NOOP_TOK)
        i += 1
    return tokens


def tokens_to_vectors(tokens):
    return [_TABLE[t] if t in _TABLE else _TABLE[NOOP_TOK]
            for t in tokens]


# ── decode: slot vectors → tokens → RPN string ────────────────────────────

def _nearest_token(vec):
    """L2-nearest token in the embedding table."""
    best, best_d = NOOP_TOK, float('inf')
    for t, e in _TABLE.items():
        d = sum((a - b) * (a - b) for a, b in zip(vec, e))
        if d < best_d:
            best_d, best = d, t
    return best


def vectors_to_tokens(vectors):
    return [_nearest_token(v) for v in vectors]


def decode(vectors):
    return ''.join(vectors_to_tokens(vectors))


# ── mutation in vector space ─────────────────────────────────────────────

def random_vectors(rng, length=None):
    """Initialize a vector-DNA. Each slot is a random known token's
    embedding + small noise — keeps initial decodes meaningful (not stuck
    in the NOOP region) so the search has somewhere to climb from."""
    if length is None:
        length = rng.randint(4, 8)
    out = []
    for _ in range(length):
        tok = rng.choice(_TOKEN_LIST)
        emb = _TABLE[tok]
        noise = [rng.gauss(0, 0.1) for _ in range(DIM)]
        out.append(tuple(e + nz for e, nz in zip(emb, noise)))
    return out


def mutate_vector(vectors, rng, sigma=0.3,
                  insert_prob=0.30, delete_prob=0.10, max_len=20):
    """One mutation step. With prob insert_prob: insert a fresh anchored
    slot. With prob delete_prob: drop one slot. Else: perturb one slot
    with isotropic gaussian noise. Cap genome at max_len slots."""
    out = [list(v) for v in vectors]
    r = rng.random()

    if r < insert_prob and len(out) < max_len:
        i = rng.randrange(len(out) + 1)
        tok = rng.choice(_TOKEN_LIST)
        emb = _TABLE[tok]
        new_v = [e + rng.gauss(0, 0.1) for e in emb]
        out.insert(i, new_v)
    elif r < insert_prob + delete_prob and out:
        i = rng.randrange(len(out))
        out.pop(i)
    elif out:
        i = rng.randrange(len(out))
        out[i] = [x + rng.gauss(0, sigma) for x in out[i]]

    return [tuple(v) for v in out]
