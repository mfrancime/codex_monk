"""
kernel.py — Linux kernel-level telemetry probes for the WSL agentic monitor.

Design law (inherited from vajrayana): the gate lives in the crew, never in the
LLM. These probes return DUMB NUMBERS ONLY. No judgement, no thresholds, no
narration. Detection and explanation happen upstream. A probe's only job is to
read what the kernel already computes and hand back a struct.

Core idea (the whole reason this file exists):
  psutil's mem.percent answers "how full is memory?" — a LAGGING proxy that
  said "fine" right up until the host froze. PSI (/proc/pressure/memory) answers
  "how much time did tasks spend STALLED waiting for memory?" — a LEADING signal
  that climbs *before* the thrash/OOM. We read PSI when present and fall back
  cleanly when it isn't (older kernels, some WSL2 builds).

Everything here is read-only, allocation-light, and safe to call every tick.
Verified present on MARKFRA02 (kernel 6.6.114.1-microsoft-standard-WSL2):
/proc/pressure/{memory,cpu,io} and cgroup v2 unified are all available.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field, asdict
from typing import Optional


# ---------------------------------------------------------------------------
# Capability detection — done ONCE at import, cached. The agent must know which
# kernel interfaces exist on THIS host rather than assuming. WSL2 in particular
# may ship without PSI or without cgroup v2 unified hierarchy.
# ---------------------------------------------------------------------------

def _readable(path: str) -> bool:
    try:
        with open(path, "r"):
            return True
    except OSError:
        return False


@dataclass(frozen=True)
class Capabilities:
    psi_memory: bool
    psi_cpu: bool
    psi_io: bool
    cgroup_v2: bool
    swaps: bool

    @property
    def psi_any(self) -> bool:
        return self.psi_memory or self.psi_cpu or self.psi_io


def detect_capabilities() -> Capabilities:
    return Capabilities(
        psi_memory=_readable("/proc/pressure/memory"),
        psi_cpu=_readable("/proc/pressure/cpu"),
        psi_io=_readable("/proc/pressure/io"),
        cgroup_v2=_readable("/sys/fs/cgroup/cgroup.controllers"),
        swaps=_readable("/proc/swaps"),
    )


CAPS = detect_capabilities()


# ---------------------------------------------------------------------------
# PSI — Pressure Stall Information
#
# Format of /proc/pressure/memory:
#   some avg10=0.00 avg60=0.00 avg300=0.00 total=12345
#   full avg10=0.00 avg60=0.00 avg300=0.00 total=6789
#
# "some" = at least one task stalled on this resource.
# "full" = ALL non-idle tasks stalled (the dangerous one for memory — it means
#          everyone is waiting on the fridge and nothing is getting done).
# avgN   = % of wall-clock time stalled over the last N seconds.
# ---------------------------------------------------------------------------

@dataclass
class PSILine:
    avg10: float = 0.0
    avg60: float = 0.0
    avg300: float = 0.0
    total: int = 0


@dataclass
class PSISample:
    available: bool = False
    some: PSILine = field(default_factory=PSILine)
    full: PSILine = field(default_factory=PSILine)


def _parse_psi_line(line: str) -> PSILine:
    # line like: "some avg10=0.12 avg60=0.05 avg300=0.01 total=98765"
    out = PSILine()
    for tok in line.split()[1:]:  # skip the "some"/"full" label
        k, _, v = tok.partition("=")
        if k == "avg10":
            out.avg10 = float(v)
        elif k == "avg60":
            out.avg60 = float(v)
        elif k == "avg300":
            out.avg300 = float(v)
        elif k == "total":
            out.total = int(v)
    return out


def read_psi(resource: str = "memory") -> PSISample:
    """Read /proc/pressure/<resource>. Returns available=False if absent."""
    path = f"/proc/pressure/{resource}"
    try:
        with open(path, "r") as f:
            content = f.read()
    except OSError:
        return PSISample(available=False)

    sample = PSISample(available=True)
    for line in content.splitlines():
        if line.startswith("some"):
            sample.some = _parse_psi_line(line)
        elif line.startswith("full"):
            sample.full = _parse_psi_line(line)
    return sample


# ---------------------------------------------------------------------------
# Memory + swap from /proc/meminfo and /proc/swaps. This is the "fridge
# fullness" view — still useful, just NOT the early-warning signal. We pair it
# with PSI so we have both the lagging level and the leading pressure.
# ---------------------------------------------------------------------------

@dataclass
class MemSample:
    total_kb: int = 0
    available_kb: int = 0
    swap_total_kb: int = 0
    swap_free_kb: int = 0

    @property
    def used_pct(self) -> float:
        if self.total_kb == 0:
            return 0.0
        return 100.0 * (1.0 - self.available_kb / self.total_kb)

    @property
    def swap_total_mb(self) -> float:
        return self.swap_total_kb / 1024.0

    @property
    def swap_used_kb(self) -> int:
        return max(0, self.swap_total_kb - self.swap_free_kb)

    @property
    def has_swap(self) -> bool:
        return self.swap_total_kb > 0


def read_mem() -> MemSample:
    wanted = {
        "MemTotal:": "total_kb",
        "MemAvailable:": "available_kb",
        "SwapTotal:": "swap_total_kb",
        "SwapFree:": "swap_free_kb",
    }
    out = MemSample()
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                key = line.split(maxsplit=1)[0]
                attr = wanted.get(key)
                if attr is not None:
                    # value is the second field, in kB
                    setattr(out, attr, int(line.split()[1]))
    except OSError:
        pass
    return out


# ---------------------------------------------------------------------------
# cgroup v2 — per-workload attribution. When PSI says "memory pressure high",
# cgroup tells you WHICH cgroup is eating it. We expose memory.current +
# memory.events (oom counts). Present on MARKFRA02.
# ---------------------------------------------------------------------------

@dataclass
class CgroupSample:
    available: bool = False
    current_bytes: int = 0
    oom_events: int = 0
    oom_kill_events: int = 0


def read_cgroup(path: str = "/sys/fs/cgroup") -> CgroupSample:
    if not CAPS.cgroup_v2:
        return CgroupSample(available=False)
    s = CgroupSample(available=True)
    try:
        with open(os.path.join(path, "memory.current")) as f:
            s.current_bytes = int(f.read().strip())
    except OSError:
        pass
    try:
        with open(os.path.join(path, "memory.events")) as f:
            for line in f:
                k, _, v = line.partition(" ")
                if k == "oom":
                    s.oom_events = int(v)
                elif k == "oom_kill":
                    s.oom_kill_events = int(v)
    except OSError:
        pass
    return s


# ---------------------------------------------------------------------------
# The unified telemetry frame. One read of everything, timestamped. This is
# what the agent samples each tick (the agent itself writes compact discrete
# keys into the fabric state table; to_fabric() below is for standalone use).
# ---------------------------------------------------------------------------

@dataclass
class TelemetryFrame:
    ts: float
    caps: dict
    psi_mem: PSISample
    mem: MemSample
    cgroup: CgroupSample

    def to_fabric(self) -> dict:
        """Flat dict of the telemetry — handy for the __main__ demo and for any
        consumer that wants the whole frame at once. NOTE: the real fabric state
        table caps values at 20 bytes, so SensorAgent writes discrete short keys
        rather than this dict; this is a convenience/inspection view only."""
        return {
            "sys.ts": self.ts,
            "sys.mem.used_pct": round(self.mem.used_pct, 2),
            "sys.mem.available_mb": round(self.mem.available_kb / 1024.0, 1),
            "sys.swap.total_mb": round(self.mem.swap_total_mb, 1),
            "sys.swap.used_mb": round(self.mem.swap_used_kb / 1024.0, 1),
            "sys.swap.present": self.mem.has_swap,
            "sys.psi.available": self.psi_mem.available,
            "sys.psi.some_avg10": self.psi_mem.some.avg10,
            "sys.psi.some_avg60": self.psi_mem.some.avg60,
            "sys.psi.full_avg10": self.psi_mem.full.avg10,
            "sys.psi.full_avg60": self.psi_mem.full.avg60,
            "sys.cgroup.available": self.cgroup.available,
            "sys.cgroup.oom_kill": self.cgroup.oom_kill_events,
        }


def _frame_dict(tf: TelemetryFrame) -> dict:
    """Flatten a TelemetryFrame into the dict shape the probe registry
    contract requires. Keys match what the OPCODES table below references."""
    mem = tf.mem
    return {
        'ts':                  tf.ts,
        'psi.available':       tf.psi_mem.available,
        'psi.some.avg10':      tf.psi_mem.some.avg10,
        'psi.some.avg60':      tf.psi_mem.some.avg60,
        'psi.full.avg10':      tf.psi_mem.full.avg10,
        'psi.full.avg60':      tf.psi_mem.full.avg60,
        'mem.total_kb':        mem.total_kb,
        'mem.available_kb':    mem.available_kb,
        'mem.used_pct':        mem.used_pct,
        'mem.avail_pct':       100.0 - mem.used_pct,
        'mem.swap_total_kb':   mem.swap_total_kb,
        'mem.swap_present':    mem.has_swap,
        'mem.swap_total_mb':   mem.swap_total_mb,
        'cgroup.available':    tf.cgroup.available,
        'cgroup.current_bytes': tf.cgroup.current_bytes,
        'cgroup.oom_kills':    tf.cgroup.oom_kill_events,
    }


def sample_all() -> dict:
    """Single cheap read of every kernel signal, flattened into a dict
    Frame. Safe to call every tick. The TelemetryFrame dataclass is kept
    as an INTERNAL building block (so existing callers that introspect
    `.psi_mem.some.avg10` keep working) — but the registered contract
    returns the dict."""
    tf = TelemetryFrame(
        ts=time.time(),
        caps=asdict(CAPS),
        psi_mem=read_psi("memory"),
        mem=read_mem(),
        cgroup=read_cgroup(),
    )
    return _frame_dict(tf)


def describe() -> str:
    """Boot-banner one-liner. Picks the live mode based on CAPS."""
    mode = 'psi+swap+cgroup' if (CAPS.psi_memory and CAPS.cgroup_v2) else (
        'psi+swap' if CAPS.psi_memory else 'fallback_level')
    return f'kernel ({mode})'


# ── opcode alphabet (the kernel domain's load tokens) ────────────────────
#
# Disjoint first chars (ψ, ~, κ) so cgroup_pods / disk_net / k8s_api can
# coexist in the same agent's interpreter call by table-merging.

OPCODES = {
    'ψ': {
        's': 'psi.some.avg10',
        'f': 'psi.full.avg10',
        '?': 'psi.available',
    },
    '~': {
        'u': 'mem.used_pct',
        'a': 'mem.avail_pct',
        'S': 'mem.swap_present',
        's': 'mem.swap_total_mb',
    },
    'κ': {
        '?': 'cgroup.available',
    },
}


# Register with the plugin system at import time.
from swarm.probes import register as _register
_register('kernel', sample_all, OPCODES, describe)


if __name__ == "__main__":
    import json
    print("capabilities:", json.dumps(asdict(CAPS), indent=2))
    print("Frame:", json.dumps(sample_all(), indent=2, default=str))
