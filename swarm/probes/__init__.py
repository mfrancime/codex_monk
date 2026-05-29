"""Kernel-level telemetry probes for the WSL agentic monitor.

Read-only, allocation-light readers for signals the kernel already computes:
PSI (/proc/pressure/*), /proc/meminfo + /proc/swaps, and cgroup v2. Probes
return dumb numbers only — detection lives in the gate, not here.
"""

from swarm.probes.kernel import (
    CAPS, Capabilities, detect_capabilities,
    PSISample, PSILine, read_psi,
    MemSample, read_mem,
    CgroupSample, read_cgroup,
    TelemetryFrame, sample_all,
)

__all__ = [
    'CAPS', 'Capabilities', 'detect_capabilities',
    'PSISample', 'PSILine', 'read_psi',
    'MemSample', 'read_mem',
    'CgroupSample', 'read_cgroup',
    'TelemetryFrame', 'sample_all',
]
