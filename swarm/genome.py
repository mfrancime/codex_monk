"""
genome.py — codex_monk's alien declarative DNA, domain-agnostic edition.

A genome is a short UTF-8 string of opcodes. Interpreted against a Frame
(any dict-shaped reading) PLUS an opcode table (the domain's load alphabet),
it produces a (sev, code) verdict. There is no Python agent class per
behavior — there is one interpreter, and behavior is data. New gates,
new severities, new routing become new genome strings, not new code.

Stack-based RPN. Designed for compactness (each rule fits in a 20-byte
fabric state slot) and for machine evolvability (random byte perturbations
either no-op or shift behavior — they never crash).

Vocabulary — the SPINE (universal, all probes share these):

  Literals:
    0-9        push that digit
    ‡DDDD...   push integer formed by following ASCII digits (stop at non-digit)

  Comparators (RPN: pop b, pop a, push (a OP b)):
    >  GT   <  LT   ≥ GE   ≤ LE   ≡ EQ   ≠ NE

  Boolean:
    ∧ AND   ∨ OR   ¬ NOT

  Emit + control:
    →XY  pop bool; if true, emit (sev tag X, code tag Y) and stop
    ;    rule separator (clears the stack between rules)

  Sev tags:  O I W C   → OK INFO WARN CRITICAL
  Code tags: o=OK a=SWAP_ABSENT w=MEM_PSI_WARN n=MEM_PSI_CRIT_NOSW
             c=MEM_PSI_CRIT l=MEM_LVL_WARN L=MEM_LVL_CRIT_NOSW

Vocabulary — LOAD opcodes (per-domain, registered by each probe):

  Loads are 2-char (op + sig) pairs supplied at interpret time as a nested
  dict: `{first_char: {sig_char: frame_key}}`. The kernel probe registers
  ψ/~/κ; the cgroup_pods probe registers Π; disk_net registers Δ/Ν; k8s_api
  registers K. Domain authors pick disjoint first-chars; the framework
  does not police collisions.

  Examples (kernel domain):
    ψs  → frame['psi.some.avg10']
    ψf  → frame['psi.full.avg10']
    ψ?  → frame['psi.available']
    ~u  → frame['mem.used_pct']
    ~a  → frame['mem.avail_pct']
    ~S  → frame['mem.swap_present']
    ~s  → frame['mem.swap_total_mb']
    κ?  → frame['cgroup.available']

Default emit when no rule fires: ('OK', 'OK'). Stack underflow / unknown
opcode are silent — they push 0 and move on. The worst a mutated genome can
do is score poorly; it cannot raise.
"""

SEV_TAGS = {'O': 'OK', 'I': 'INFO', 'W': 'WARN', 'C': 'CRITICAL'}

CODE_TAGS = {
    'o': 'OK',
    'a': 'SWAP_ABSENT',
    'w': 'MEM_PSI_WARN',
    'n': 'MEM_PSI_CRIT_NOSW',
    'c': 'MEM_PSI_CRIT',
    'l': 'MEM_LVL_WARN',
    'L': 'MEM_LVL_CRIT_NOSW',
    # k8s vertical adds:
    'd': 'CLUSTER_DEGRADED',
    'g': 'GATE_DOWN',
    'p': 'POD_PRESSURE',
    'x': 'NODE_NOT_READY',
}


def interpret(genome, frame, opcodes=None):
    """Walk `genome` (str) against `frame` (dict) using `opcodes` (load
    table). Return (sev, code). Never raises.

    `opcodes` shape: {first_char: {sig_char: frame_key_str}}. When a load
    opcode is encountered, looks up the key string and pushes
    `float(frame.get(key, 0.0))`. Unknown opcode chars are silently
    skipped — a mutated genome with a foreign load char no-ops, never
    crashes.

    `opcodes=None` is equivalent to `{}` — only the spine is available
    (literals, comparators, boolean, emit). Useful for testing the
    interpreter without any domain attached.
    """
    if opcodes is None:
        opcodes = {}

    stack = []

    def push(v):
        if len(stack) < 16:
            stack.append(float(v))

    def pop():
        return stack.pop() if stack else 0.0

    i, n = 0, len(genome)
    while i < n:
        ch = genome[i]

        if ch == ';':
            stack.clear()
            i += 1
            continue

        if ch == '→':
            cond = pop()
            if cond > 0 and i + 2 < n:
                sev = SEV_TAGS.get(genome[i + 1], 'OK')
                code = CODE_TAGS.get(genome[i + 2], 'OK')
                return sev, code
            i += 3
            continue

        # domain load: 2-char (op + sig). Registered per probe.
        if ch in opcodes and i + 1 < n:
            sig = genome[i + 1]
            key = opcodes[ch].get(sig)
            if key is not None:
                push(_frame_value(frame, key))
            i += 2
            continue

        if ch == '‡':
            i += 1
            buf = ''
            while i < n and genome[i].isdigit():
                buf += genome[i]
                i += 1
            push(int(buf) if buf else 0)
            continue

        if ch.isdigit():
            push(int(ch))
            i += 1
            continue

        if ch in '><≥≤≡≠':
            b, a = pop(), pop()
            if ch == '>':   push(1.0 if a > b  else 0.0)
            elif ch == '<': push(1.0 if a < b  else 0.0)
            elif ch == '≥': push(1.0 if a >= b else 0.0)
            elif ch == '≤': push(1.0 if a <= b else 0.0)
            elif ch == '≡': push(1.0 if a == b else 0.0)
            elif ch == '≠': push(1.0 if a != b else 0.0)
            i += 1
            continue

        if ch == '∧':
            b, a = pop(), pop()
            push(1.0 if (a > 0 and b > 0) else 0.0)
            i += 1
            continue
        if ch == '∨':
            b, a = pop(), pop()
            push(1.0 if (a > 0 or b > 0) else 0.0)
            i += 1
            continue
        if ch == '¬':
            a = pop()
            push(1.0 if a == 0 else 0.0)
            i += 1
            continue

        # unknown opcode: silent no-op (resilience to mutation)
        i += 1

    return 'OK', 'OK'


def _frame_value(frame, key):
    """Cast frame[key] to a float in a way that's lenient about boolean,
    None, and missing keys. Frames are dicts produced by probes; the
    interpreter doesn't trust them to all be float."""
    v = frame.get(key, 0.0) if isinstance(frame, dict) else 0.0
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if v is None:
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0
