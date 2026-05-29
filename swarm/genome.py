"""
genome.py — codex_monk's alien declarative DNA.

A genome is a short UTF-8 string of opcodes. Interpreted against a
TelemetryFrame, it produces a (sev, code) verdict. There is no Python agent
class per behavior — there is one interpreter, and behavior is data. New
gates, new severities, new routing become new genome strings, not new code.

Stack-based RPN. Designed for compactness (each rule fits in a 20-byte
fabric state slot) and for machine evolvability (random byte perturbations
either no-op or shift behavior — they never crash).

Vocabulary (the current minimal core):

  Frame loads (2 chars: opcode + sigil):
    ψs  push psi.some.avg10           ~u  push used_pct
    ψf  push psi.full.avg10           ~a  push avail_pct
    ψ?  push 1 if psi.available       ~S  push 1 if swap_present
                                      ~s  push swap_total_mb
                                      κ?  push 1 if cgroup_v2

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
}


def _frame_value(frame, op, sig):
    if op == 'ψ':
        if sig == 's': return float(getattr(frame.psi_mem.some, 'avg10', 0.0))
        if sig == 'f': return float(getattr(frame.psi_mem.full, 'avg10', 0.0))
        if sig == '?': return 1.0 if getattr(frame.psi_mem, 'available', False) else 0.0
    if op == '~':
        mem = frame.mem
        total = max(getattr(mem, 'total_kb', 1), 1)
        avail = getattr(mem, 'available_kb', 0)
        used_pct = 100.0 * (1.0 - avail / total)
        swap_kb = getattr(mem, 'swap_total_kb', 0)
        if sig == 'u': return used_pct
        if sig == 'a': return 100.0 - used_pct
        if sig == 'S': return 1.0 if swap_kb > 0 else 0.0
        if sig == 's': return swap_kb / 1024.0
    if op == 'κ' and sig == '?':
        return 1.0 if getattr(frame.cgroup, 'available', False) else 0.0
    return 0.0


def interpret(genome, frame):
    """Walk `genome` (str) against `frame` (TelemetryFrame). Return (sev,
    code). Never raises."""
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

        if ch in ('ψ', '~', 'κ') and i + 1 < n:
            push(_frame_value(frame, ch, genome[i + 1]))
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
