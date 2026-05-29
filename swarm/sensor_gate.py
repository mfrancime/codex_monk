"""
sensor_gate.py — the alert gate for the WSL kernel monitor.

VAJ Law 2: the gate lives in the crew, never in the LLM. This file is where
"is this bad?" is answered, and it is answered in plain, auditable arithmetic —
NO model call. A downstream narrator only writes the human sentence and cites
the retrieved runbook line. If this file decides "quiet," no alert fires
regardless of what any model thinks.

Why deterministic: an alert system you cannot reason about is an alert system
you cannot trust at 3am. Thresholds are inspectable, testable, and cheap. A
model deciding severity is none of those.

NOTE: deliberately named `sensor_gate` (NOT `gate`) so it does not collide with
the existing RAG refusal gate at swarm/gate.py — a completely separate concern.

Reason codes are kept <= 20 chars so they fit a fabric state slot (SS_VALUE).
The distinction "PSI path vs level/fallback path" is carried by the prefix
(MEM_PSI_* vs MEM_LVL_*) and by the `psi_available` fact, not by a long suffix.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from swarm.probes.kernel import TelemetryFrame


class Severity(IntEnum):
    OK = 0
    INFO = 1
    WARN = 2
    CRITICAL = 3


@dataclass(frozen=True)
class Thresholds:
    # PSI "some" avg10: at least one task stalling on memory. Early tremor.
    psi_some_warn: float = 10.0
    psi_some_crit: float = 25.0
    # PSI "full" avg10: ALL non-idle tasks stalled — the freeze signature.
    psi_full_warn: float = 1.0
    psi_full_crit: float = 5.0
    # Fallback when PSI is absent (older kernels): use the lagging level.
    mem_used_warn: float = 80.0
    mem_used_crit: float = 92.0


@dataclass
class Verdict:
    severity: Severity
    # machine-readable reason code -> drives the runbook lookup (symptom -> SOP)
    code: str
    # the raw facts that tripped it, for the narrator to cite — NOT prose
    facts: dict


def evaluate(frame: TelemetryFrame, th: Thresholds = Thresholds()) -> Verdict:
    """Pure function: frame in, verdict out. No I/O, no model, no side effects."""
    mem = frame.mem
    psi = frame.psi_mem

    # --- Precondition alarm: no swap. This is the meltdown precondition we
    # found on the host. It is not itself a stall, but it removes the shock
    # absorber, so we surface it as INFO even when everything else is calm,
    # and we ESCALATE any memory verdict when swap is absent. ---
    swap_missing = not mem.has_swap

    # --- Primary path: PSI present. Watch the time wasted waiting, not the
    # fullness. "full" pressure is the freeze signature; weight it hardest. ---
    if psi.available:
        if psi.full.avg10 >= th.psi_full_crit or psi.some.avg10 >= th.psi_some_crit:
            sev, code = Severity.CRITICAL, "MEM_PSI_CRIT"
        elif psi.full.avg10 >= th.psi_full_warn or psi.some.avg10 >= th.psi_some_warn:
            sev, code = Severity.WARN, "MEM_PSI_WARN"
        elif swap_missing:
            sev, code = Severity.INFO, "SWAP_ABSENT"
        else:
            sev, code = Severity.OK, "OK"
    # --- Fallback path: PSI absent. We lose the leading signal and must lean
    # on the lagging fullness level. We compensate by tightening thresholds
    # when swap is missing, because without swap the fall is a cliff. ---
    else:
        crit = th.mem_used_crit - (7.0 if swap_missing else 0.0)
        warn = th.mem_used_warn - (7.0 if swap_missing else 0.0)
        if mem.used_pct >= crit:
            sev, code = Severity.CRITICAL, "MEM_LVL_CRIT"
        elif mem.used_pct >= warn:
            sev, code = Severity.WARN, "MEM_LVL_WARN"
        elif swap_missing:
            sev, code = Severity.INFO, "SWAP_ABSENT"
        else:
            sev, code = Severity.OK, "OK"

    # Escalate one notch if swap is missing AND we already have a WARN-level
    # memory concern: without the shock absorber, a warn is one spike from a
    # freeze. Replace (not append) to keep the code <= 20 chars.
    if swap_missing and sev == Severity.WARN:
        sev = Severity.CRITICAL
        code = code.replace("WARN", "CRIT") + "_NOSW"   # e.g. MEM_PSI_CRIT_NOSW

    facts = {
        "psi_available": psi.available,
        "psi_some_avg10": psi.some.avg10,
        "psi_full_avg10": psi.full.avg10,
        "mem_used_pct": round(mem.used_pct, 2),
        "swap_present": mem.has_swap,
        "swap_total_mb": round(mem.swap_total_mb, 1),
    }
    return Verdict(severity=sev, code=code, facts=facts)
