"""
cgroup_pods.py — per-pod cgroup observability for codex_monk.

Walks /sys/fs/cgroup/kubepods.slice/ (cgroup v2) or /sys/fs/cgroup/memory/
kubepods/ (cgroup v1) and aggregates per-pod memory/CPU pressure into a
flat Frame dict. The genome gates on aggregates, not per-pod values — the
swarm pattern is one-emit-per-tick, so aggregates are the right shape.

Frame keys:
  pod.count                — total pods seen this tick
  pod.max.psi_some_avg10   — highest psi.some.avg10 across pods (memory.pressure)
  pod.max.psi_full_avg10   — highest psi.full.avg10 across pods
  pod.max.mem_pct          — highest memory.current / memory.max across pods
  pod.sum.oom_kills        — cumulative oom_kill counter sum across pods
  pod.delta.oom_kills_60s  — oom_kills observed in the last ~60s (ring window)
  pod.count_pressured      — count of pods with psi.some.avg10 > 5
  pod.cgroup_root          — 1 if /sys/fs/cgroup/kubepods.slice exists, else 0

Opcodes (Π — Greek capital pi):
  Πs → pod.max.psi_some_avg10
  Πf → pod.max.psi_full_avg10
  Πm → pod.max.mem_pct
  Πo → pod.delta.oom_kills_60s
  Πn → pod.count
  Πp → pod.count_pressured
  Π? → pod.cgroup_root   (probe-availability check, like κ?)

Synthetic-friendly: the probe's directory root is overridable via the
CODEX_CGROUP_ROOT env var, so tests can point at a tempfile tree.
"""

import os
import time

from swarm.probes import register


# Allow tests + non-k8s dev hosts to point this at a synthetic tree.
def _cgroup_root() -> str:
    env = os.environ.get('CODEX_CGROUP_ROOT')
    if env:
        return env
    # cgroup v2 unified path (modern k8s nodes)
    for p in ('/sys/fs/cgroup/kubepods.slice',
              '/sys/fs/cgroup/kubepods'):
        if os.path.isdir(p):
            return p
    return '/sys/fs/cgroup/kubepods.slice'   # default; absent → empty frame


def _read_int(path: str, default: int = 0) -> int:
    try:
        with open(path, 'r') as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return default


def _read_text(path: str) -> str:
    try:
        with open(path, 'r') as f:
            return f.read()
    except OSError:
        return ''


def _parse_psi_line(line: str) -> float:
    """Pull avg10 out of a `some ...` or `full ...` line from memory.pressure."""
    for tok in line.split()[1:]:
        if tok.startswith('avg10='):
            try:
                return float(tok.split('=', 1)[1])
            except ValueError:
                return 0.0
    return 0.0


def _read_psi(path: str) -> dict:
    """Read a cgroup-level memory.pressure or cpu.pressure file. Returns
    {'some_avg10': float, 'full_avg10': float}."""
    out = {'some_avg10': 0.0, 'full_avg10': 0.0}
    content = _read_text(path)
    for line in content.splitlines():
        if line.startswith('some '):
            out['some_avg10'] = _parse_psi_line(line)
        elif line.startswith('full '):
            out['full_avg10'] = _parse_psi_line(line)
    return out


def _read_oom_counts(path: str) -> int:
    """Parse memory.events for the oom_kill total. Each line is `key value`."""
    content = _read_text(path)
    for line in content.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == 'oom_kill':
            try:
                return int(parts[1])
            except ValueError:
                return 0
    return 0


def _iter_pod_dirs(root: str):
    """Yield each pod cgroup directory under the kubepods root. cgroup v2
    pod dirs are named `kubepods-pod<uid>.slice/`; we also accept anything
    that has a memory.current file (broad enough for v1 and synthetic trees)."""
    if not os.path.isdir(root):
        return
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if not os.path.isdir(path):
            continue
        # accept anything with a memory.current (matches both real pod dirs
        # and synthetic test fixtures)
        if os.path.isfile(os.path.join(path, 'memory.current')):
            yield path


# ── stateful: keep oom_kill totals from prior ticks for delta computation
#
# A small ring of (timestamp, sum_oom_kills) so we can compute
# "oom_kills seen in the last 60s." Bounded — only the last few entries kept.

_OOM_RING: list = []        # list of (ts, total_oom_kills)
_OOM_WINDOW_S = 60.0
_OOM_RING_CAP = 32


def _record_oom(now: float, total: int) -> int:
    _OOM_RING.append((now, total))
    if len(_OOM_RING) > _OOM_RING_CAP:
        del _OOM_RING[0]
    # find the earliest entry within the window
    cutoff = now - _OOM_WINDOW_S
    baseline = total
    for ts, val in _OOM_RING:
        if ts >= cutoff:
            baseline = val
            break
    return max(0, total - baseline)


def sample_all() -> dict:
    """One cheap tick: walk kubepods, aggregate, return Frame dict."""
    root = _cgroup_root()
    now = time.time()

    pods_seen = 0
    max_some = 0.0
    max_full = 0.0
    max_mem_pct = 0.0
    sum_oom_kills = 0
    pressured = 0

    for pod_path in _iter_pod_dirs(root):
        pods_seen += 1

        psi = _read_psi(os.path.join(pod_path, 'memory.pressure'))
        s = psi['some_avg10']
        if s > max_some: max_some = s
        if psi['full_avg10'] > max_full: max_full = psi['full_avg10']
        if s > 5.0: pressured += 1

        current = _read_int(os.path.join(pod_path, 'memory.current'))
        # memory.max may be 'max' (cgroup v2 sentinel) or numeric — handle both
        mem_max_raw = _read_text(os.path.join(pod_path, 'memory.max')).strip()
        try:
            mem_max = int(mem_max_raw)
        except ValueError:
            mem_max = 0
        if mem_max > 0 and current >= 0:
            pct = 100.0 * current / mem_max
            if pct > max_mem_pct: max_mem_pct = pct

        sum_oom_kills += _read_oom_counts(
            os.path.join(pod_path, 'memory.events'))

    delta_oom = _record_oom(now, sum_oom_kills)

    return {
        'ts':                       now,
        'pod.count':                pods_seen,
        'pod.max.psi_some_avg10':   max_some,
        'pod.max.psi_full_avg10':   max_full,
        'pod.max.mem_pct':          max_mem_pct,
        'pod.sum.oom_kills':        sum_oom_kills,
        'pod.delta.oom_kills_60s':  delta_oom,
        'pod.count_pressured':      pressured,
        'pod.cgroup_root':          1 if os.path.isdir(root) else 0,
    }


def describe() -> str:
    root = _cgroup_root()
    present = os.path.isdir(root)
    return f'cgroup_pods ({"live" if present else "absent"}: {root})'


OPCODES = {
    'Π': {
        's': 'pod.max.psi_some_avg10',
        'f': 'pod.max.psi_full_avg10',
        'm': 'pod.max.mem_pct',
        'o': 'pod.delta.oom_kills_60s',
        'n': 'pod.count',
        'p': 'pod.count_pressured',
        '?': 'pod.cgroup_root',
    },
}


register('cgroup_pods', sample_all, OPCODES, describe)


if __name__ == "__main__":
    import json
    print(json.dumps(sample_all(), indent=2))
